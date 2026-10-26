from __future__ import annotations

import csv
from dataclasses import dataclass
from decimal import Decimal
from io import StringIO
from typing import Optional

from django.contrib.auth.models import User
from django.core.cache import cache
from django.db import transaction
from django.db.models import Case, F, IntegerField, Q, Value, When, Sum
from django.db.models.functions import TruncDate
from django.utils import timezone
from datetime import timedelta

from .context_processors import ALERT_COUNT_CACHE_KEY
from .models import Category, Product, StockMovement, Supplier
from django.core.exceptions import ValidationError


# service para listar los productos con filtros.
#filtros de suplier y category opcionales, por defecto muestra los productos activos.
def products_for_list(
    *,
    supplier: Optional[Supplier] = None,
    category: Optional[Category] = None,
    search: str = '',
    show_inactive: bool = False,
):
    qs = Product.objects.select_related('category', 'supplier')
    if not show_inactive:
        qs = qs.filter(is_active=True)
    if supplier:
        qs = qs.filter(supplier=supplier)
    if category:
        qs = qs.filter(category=category)
    if search:
        # Buscar por nombre o por SKU ambos son visibles para el usuario en la UI.
        qs = qs.filter(Q(name__icontains=search) | Q(sku__icontains=search))
    return qs


def low_stock_products():
    #Productos en o por debajo de su umbral mínimo de stock.
    return (
        Product.objects.filter(is_active=True, quantity__lte=F('minimum_stock'))
        .annotate(
            # 0 = "agotado" (más urgente), 1 = "bajo pero > 0".
            # Ordenar por esto deja los badges rojos arriba.
            urgency=Case(
                When(quantity=0, then=Value(0)),
                default=Value(1),
                output_field=IntegerField(),
            ),
        )
        .select_related('category', 'supplier')
        .order_by('urgency', 'quantity', 'name')
    )

#devuelve los datos que necesitan los graficos del dashboard
def movement_chart_data(days: int = 7):

    start = timezone.now().date() - timedelta(days=days - 1)
    qs = (
        StockMovement.objects
        .filter(created_at__date__gte=start, movement_type__in=('IN', 'OUT'))
        .annotate(day=TruncDate('created_at'))
        # Agrupar por (día, tipo) y sumar la cantidad una fila por (día, tipo).
        .values('day', 'movement_type')
        .annotate(total=Sum('quantity'))
        .order_by('day')
    )
    by_day: dict = {}
    for row in qs:
        by_day.setdefault(row['day'], {'IN': 0, 'OUT': 0})
        by_day[row['day']][row['movement_type']] = row['total']

    labels, in_series, out_series = [], [], []
    cursor = start
    for _ in range(days):
        labels.append(cursor.isoformat())
        in_series.append(by_day.get(cursor, {}).get('IN', 0))
        out_series.append(by_day.get(cursor, {}).get('OUT', 0))
        cursor = cursor + timedelta(days=1)
    return labels, in_series, out_series


# ---------------------------------------------------------------------------
# Generación de SKU
# ---------------------------------------------------------------------------

CATEGORY_PREFIXES = {
    'ELECTRONICS': 'EL',
    'ELECTRÓNICOS': 'EL',
    'OFFICE SUPPLIES': 'OF',
    'FURNITURE': 'FU',
    'MUEBLES': 'FU',
}

DEFAULT_SKU_PREFIX = 'GEN'

SKU_NUMBER_FORMAT = '{prefix}-{number:04d}'

INITIAL_STOCK_REASON = 'Stock inicial'

PRODUCT_DELETED_REASON = 'Producto eliminado'


@transaction.atomic
def generate_next_sku(category: Category) -> str:
    if category is None:
        raise ValueError('Se requiere una categoría para generar el SKU.')

    normalized = category.name.strip().upper()
    prefix = CATEGORY_PREFIXES.get(normalized, DEFAULT_SKU_PREFIX)

    if prefix == DEFAULT_SKU_PREFIX:
        import logging
        logger = logging.getLogger(__name__)
        logger.warning(
            "Categoría '%s' sin prefijo de SKU mapeado; usando '%s'. "
            'Editar CATEGORY_PREFIXES en services.py para mapearla.',
            category.name, DEFAULT_SKU_PREFIX,
        )
    pattern = f'{prefix}-%'
    existing = (
        Product.objects
        .select_for_update()
        .filter(sku__regex=rf'^{prefix}-[0-9]+$')
        .order_by('-sku')
        .values_list('sku', flat=True)
    )
    existing = list(existing)
    next_number = 1
    if existing:
        try:
            latest_number = int(existing[0].split('-', 1)[1])
            next_number = latest_number + 1
        except (IndexError, ValueError):
            next_number = 1

    candidate = SKU_NUMBER_FORMAT.format(prefix=prefix, number=next_number)
    while Product.objects.filter(sku=candidate).exists():
        next_number += 1
        candidate = SKU_NUMBER_FORMAT.format(prefix=prefix, number=next_number)

    return candidate


def preview_next_sku(category: Category) -> str:
    if category is None:
        return ''
    normalized = category.name.strip().upper()
    prefix = CATEGORY_PREFIXES.get(normalized, DEFAULT_SKU_PREFIX)
    latest = (
        Product.objects
        .filter(sku__regex=rf'^{prefix}-[0-9]+$')
        .order_by('-sku')
        .values_list('sku', flat=True)
        .first()
    )
    next_number = 1
    if latest:
        try:
            next_number = int(latest.split('-', 1)[1]) + 1
        except (IndexError, ValueError):
            next_number = 1
    return SKU_NUMBER_FORMAT.format(prefix=prefix, number=next_number)


# ---------------------------------------------------------------------------
# Helpers de escritura
# ---------------------------------------------------------------------------

# tipado para los resultados de la funcion record_movement
@dataclass
class MovementResult:
    movement: StockMovement
    triggered_low_stock: bool


#funcion para registrar un movimiento de stock asegurando la integridad de la transaccion.
@transaction.atomic
def record_movement(
    *,
    product: Product,
    movement_type: str,
    quantity: int,
    reason: str = '',
    reference: str = '',
    supplier: Optional[Supplier] = None,
    user: Optional[User] = None,
    unit_price: Optional[Decimal] = None,
) -> MovementResult:

    if quantity <= 0:
        raise ValueError('La cantidad debe ser positiva.')

    movement = StockMovement(
        product=product,
        movement_type=movement_type,
        quantity=quantity,
        reason=reason,
        reference=reference,
        supplier=supplier,
        created_by=user,
    )

    movement.full_clean()

    if movement_type == 'IN':
        product.quantity = product.quantity + quantity
        # Si el producto no tenía proveedor y el usuario eligió uno para esta compra, lo guardamos
        if supplier and not product.supplier_id:
            product.supplier = supplier
        # El precio unitario se puede actualizar al ingresar (el precio de compra puede cambiar).
        if unit_price is not None:
            product.unit_price = unit_price
    elif movement_type == 'OUT':
        if product.quantity < quantity:
            raise ValidationError(
                f'No hay suficiente stock para el producto {product.sku} '
                f'(solicitado {quantity}, disponible {product.quantity}).'
            )
        product.quantity = product.quantity - quantity

    product.save()
    movement.save()

    #se elimina cache del conteo de alertas para que se actualice
    cache.delete(ALERT_COUNT_CACHE_KEY)

    return MovementResult(movement=movement, triggered_low_stock=product.is_low_stock)


# ---------------------------------------------------------------------------
# Importación CSV
# ---------------------------------------------------------------------------

#Encabezados requeridos en toda importación
CSV_REQUIRED_HEADERS = ('sku', 'name', 'category', 'quantity', 'minimum_stock')

#tipado del resultado de la importación csv
@dataclass
class CsvImportReport:
    created: int = 0
    updated: int = 0
    skipped: int = 0
    errors: list = None
    #: Lista de (row_number, sku) para las filas que actualizaron un
    #: producto ya existente. Sirve para que el usuario vea qué se
    #: sobrescribió en la importación.
    updated_rows: list = None

    def __post_init__(self):
        if self.errors is None:
            self.errors = []
        if self.updated_rows is None:
            self.updated_rows = []


@transaction.atomic
def import_products_from_csv(file_obj) -> CsvImportReport:
    """Importa productos en bulk desde un archivo CSV.

    Reglas de SKU:
        - Si la columna sku viene vacia se genera un SKU
          correlativo según la categoría y se crea el producto.
        - Si la columna sku viene llena y no existe se crea el
          producto con ese sku.
        - Si la columna sku viene llena y ya existe se actualiza
          ese producto se actualiza con la info del csv,excepto el stock.

    """
    report = CsvImportReport()
    #se decodifica el archivo CSV
    decoded = file_obj.read().decode('utf-8-sig')
    #se crea un lector de CSV
    reader = csv.DictReader(StringIO(decoded))
    if reader.fieldnames is None:
        raise ValueError('El archivo CSV parece estar vacío.')

    # Normalizar encabezados a minúsculas + strip, para que "SKU" y "sku" sean intercambiables.
    headers = {h.lower().strip() for h in reader.fieldnames}
    missing = [h for h in CSV_REQUIRED_HEADERS if h not in headers]
    if missing:
        raise ValueError(
            f'Faltan columnas obligatorias: {", ".join(missing)}. '
            f'Se requiere al menos: {", ".join(CSV_REQUIRED_HEADERS)}.'
        )

    for index, raw_row in enumerate(reader, start=2):  # start=2: el encabezado es fila 1
        row = {k.lower().strip(): (v or '').strip() for k, v in raw_row.items() if k}
        try:
            result = _import_one_row(row)
            if result.created:
                report.created += 1
            else:
                report.updated += 1
                report.updated_rows.append((index, result.actual_sku))
        except Exception as exc:
            report.errors.append((index, str(exc)))
            report.skipped += 1
    cache.delete(ALERT_COUNT_CACHE_KEY)

    return report


@dataclass
class _ImportRowResult:
    created: bool
    requested_sku: str
    actual_sku: str


def _import_one_row(row: dict) -> _ImportRowResult:
    requested_sku = row.get('sku', '').strip()

    category_name = row.get('category')
    if not category_name:
        raise ValueError('category es obligatoria')

    category, _ = Category.objects.get_or_create(name=category_name)

    supplier_obj = None
    supplier_name = row.get('supplier', '')
    if supplier_name:
        supplier_obj, _ = Supplier.objects.get_or_create(name=supplier_name)

    try:
        unit_price_raw = row.get('unit_price') or ''
        unit_price = Decimal(unit_price_raw) if unit_price_raw else Decimal('0')
        quantity_raw = row.get('quantity') or ''
        quantity = int(quantity_raw) if quantity_raw else 0
        minimum_raw = row.get('minimum_stock') or ''
        minimum_stock = int(minimum_raw) if minimum_raw else 0
    except (ValueError, ArithmeticError, TypeError) as exc:
        raise ValueError(f'Valor numérico inválido: {exc}')

    final_sku = _resolve_sku_for_import(requested_sku, row, category)

    defaults = {
        'name': row.get('name') or final_sku,
        'description': row.get('description', ''),
        'category': category,
        'supplier': supplier_obj,
        'unit': row.get('unit', 'unidad') or 'unidad',
        'unit_price': unit_price,
        'minimum_stock': minimum_stock,
        'is_active': _parse_bool(row.get('is_active'), default=True),
    }

    with transaction.atomic():
        existing = Product.objects.filter(sku=final_sku).first()
        if existing is None:
            # Producto nuevo: el stock del CSV es el stock inicial.
            defaults['quantity'] = quantity
        # Producto existente: NO se toca el stock. El stock real se
        # cambia registrando movimientos, no importando un catálogo.

        _, created = Product.objects.update_or_create(sku=final_sku, defaults=defaults)
        return _ImportRowResult(
            created=created,
            requested_sku=requested_sku,
            actual_sku=final_sku,
        )


def _resolve_sku_for_import(requested_sku: str, row: dict, category: Category) -> str:
    if requested_sku:
        return requested_sku

    # Sin SKU en el CSV se busca un producto con el mismo nombre en la misma categoría.
    name = row.get('name') or ''
    if name:
        existing = Product.objects.filter(name=name, category=category).first()
        if existing:
            return existing.sku

    return generate_next_sku(category)

#funcion para parsear booleanos que vienen como string 
def _parse_bool(value: str, default: bool = False) -> bool:

    if value is None or value == '':
        return default
    return value.strip().lower() in ('1', 'true', 'yes', 'y', 't')
