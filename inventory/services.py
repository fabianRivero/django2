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

    # Los agotados van primero (así las alertas más urgentes siempre aparecen en el top-10 del panel).

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
        # Agrupar por (día, tipo) y sumar la cantidad — una fila por (día, tipo).
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

#: Mapeo de nombre de categoría → prefijo de SKU. Si la categoría no
#: está acá, se usa el primer fallback (``'GEN'``) — un usuario puede
#: agregar nuevas entradas acá sin tocar nada más.
#:
#: Las claves están en MAYÚSCULAS a propósito: el lookup normaliza a
#: uppercase antes de consultar el dict, así ``"Electronics"`` y
#: ``"electronics"`` caen en el mismo prefijo.
CATEGORY_PREFIXES = {
    'ELECTRONICS': 'EL',
    'ELECTRÓNICOS': 'EL',
    'OFFICE SUPPLIES': 'OF',
    'FURNITURE': 'FU',
    'MUEBLES': 'FU',
}

#: Prefijo por defecto cuando la categoría no está mapeada. ``GEN`` por
#: "general" — significa que nadie se tomó el trabajo de mapear esta
#: categoría. Editar CATEGORY_PREFIXES para evitar caer acá.
DEFAULT_SKU_PREFIX = 'GEN'

#: Formato del correlativo: 4 dígitos con ceros a la izquierda.
#: 9999 productos por categoría alcanza para catálogos chicos; si
#: pasás ese número el SKU se vuelve ``EL-10000`` y el sistema sigue
#: funcionando (Django no valida el formato.
SKU_NUMBER_FORMAT = '{prefix}-{number:04d}'

#: Motivo que se usa cuando ``Product.save()`` genera un movimiento
#: inicial de stock al crear un producto con ``quantity > 0``.
#: Lo dejamos como constante para que sea fácil cambiarlo (o
#: internacionalizarlo) en un solo lugar.
INITIAL_STOCK_REASON = 'Stock inicial'


@transaction.atomic
def generate_next_sku(category: Category) -> str:
    """Genera el siguiente SKU disponible para una categoría.

    Estrategia: prefijo de la categoría + correlativo de 4 dígitos con
    ceros a la izquierda (ej. ``EL-0043``).

    Thread-safety:
        La función corre dentro de ``@transaction.atomic`` y usa
        ``select_for_update()`` sobre las filas que matchean el prefijo.
        En Postgres esto toma un row-level lock hasta el commit; en
        SQLite es un no-op pero las escrituras ya están serializadas
        por el motor. Resultado: dos requests concurrentes nunca
        generan el mismo SKU.

    Si la categoría no tiene un prefijo mapeado en ``CATEGORY_PREFIXES``
    se usa ``DEFAULT_SKU_PREFIX`` y se loguea un warning. La idea es
    no bloquear la creación de un producto porque alguien olvidó
    mapear una categoría nueva.

    Args:
        category: la instancia de ``Category`` para la cual generar SKU.

    Returns:
        El SKU generado, en formato ``"<PREFIX>-<NNNN>"``.

    Raises:
        ``ValueError``: si la categoría es ``None``.
    """
    if category is None:
        raise ValueError('Se requiere una categoría para generar el SKU.')

    # 1. Determinar el prefijo.
    normalized = category.name.strip().upper()
    prefix = CATEGORY_PREFIXES.get(normalized, DEFAULT_SKU_PREFIX)

    if prefix == DEFAULT_SKU_PREFIX:
        # La categoría no está mapeada. Logueamos para que sea visible
        # en el log del servidor — preferible a fallar la creación.
        import logging
        logger = logging.getLogger(__name__)
        logger.warning(
            "Categoría '%s' sin prefijo de SKU mapeado; usando '%s'. "
            'Editar CATEGORY_PREFIXES en services.py para mapearla.',
            category.name, DEFAULT_SKU_PREFIX,
        )

    # 2. Encontrar el correlativo actual dentro de la transacción.
    #    ``select_for_update`` toma un lock sobre las filas
    #    coincidentes. ``prefix-%`` matchea cualquier SKU que arranque
    #    con ``<prefix>-``.
    pattern = f'{prefix}-%'
    existing = (
        Product.objects
        .select_for_update()
        .filter(sku__regex=rf'^{prefix}-[0-9]+$')
        .order_by('-sku')
        .values_list('sku', flat=True)
    )
    # Materializar la lista ANTES de hacer cualquier otra cosa: el
    # ``select_for_update`` solo tiene efecto dentro de la transacción
    # y queremos asegurar que vimos el estado más reciente.
    existing = list(existing)

    # 3. Calcular el siguiente número.
    next_number = 1
    if existing:
        # Tomamos el primero (mayor SKU) y parseamos la parte numérica.
        # Como ordenamos por ``-sku`` lexicográfico, "EL-9999" > "EL-1000"
        # lexicográficamente — pero solo si tienen la misma cantidad de
        # dígitos. Por eso parseamos el número, no usamos la string.
        try:
            latest_number = int(existing[0].split('-', 1)[1])
            next_number = latest_number + 1
        except (IndexError, ValueError):
            # El SKU no respetó el formato — empezamos de nuevo desde 1.
            # (No debería pasar si todos los SKUs vienen de acá, pero
            # los datos importados manualmente pueden romperlo.)
            next_number = 1

    # 4. Verificar que no exista (defensa adicional, en caso de que
    #    alguien haya metido un SKU manualmente que coincida).
    candidate = SKU_NUMBER_FORMAT.format(prefix=prefix, number=next_number)
    while Product.objects.filter(sku=candidate).exists():
        next_number += 1
        candidate = SKU_NUMBER_FORMAT.format(prefix=prefix, number=next_number)

    return candidate


def preview_next_sku(category: Category) -> str:
    """Devuelve el siguiente SKU que ``generate_next_sku`` produciría,
    sin asignarlo. Útil para mostrar un placeholder en el form.

    Esta función NO toma locks ni abre transacciones — solo calcula
    lo que *probablemente* será el próximo SKU. Si entre el momento
    de mostrarlo y el de guardar el SKU otro producto se inserta, el
    SKU final será distinto al mostrado. Eso está bien: la vista lo
    verá como un campo pre-rellenado y el usuario puede editarlo.
    """
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
    """Resumen por corrida que se devuelve a la UI."""
    created: int = 0
    updated: int = 0
    skipped: int = 0
    errors: list = None
    #: Lista de ``(row_number, requested_sku, actual_sku)`` para filas
    #: donde el SKU del CSV ya existía y se le asignó uno nuevo. Sirve
    #: para mostrarle al usuario qué pasó con cada fila.
    sku_changes: list = None

    def __post_init__(self):
        if self.errors is None:
            self.errors = []
        if self.sku_changes is None:
            self.sku_changes = []


@transaction.atomic
def import_products_from_csv(file_obj) -> CsvImportReport:
    """Crea o actualiza productos en bulk desde un objeto CSV-like.

    Encabezado esperado (case-insensitive): sku, name, category, quantity,
    minimum_stock, más opcionales: supplier, description, unit, unit_price,
    is_active.

    Reglas de SKU durante la importación:
        - Si la columna ``sku`` viene VACÍA en el CSV → se genera un
          SKU correlativo según la categoría (mismo flujo que
          ``ProductForm``).
        - Si la columna ``sku`` viene LLENA y NO existe en la BD → se
          usa el SKU del CSV.
        - Si la columna ``sku`` viene LLENA y YA EXISTE en la BD → se
          ignora el SKU del CSV y se genera un nuevo correlativo. El
          reporte incluye ``sku_changes`` con el original y el final
          para que el usuario pueda revisar.

    Cada fila corre dentro de su propio bloque atómico para que una
    fila malformada no aborte toda la importación — las filas buenas
    commitean, las malas se reportan.
    """
    #se crea un objeto CsvImportReport para guardar el resultado de la importación
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
            # Si el SKU del CSV difiere del SKU final, registrarlo
            # para que la UI pueda mostrarlo.
            if result.requested_sku and result.requested_sku != result.actual_sku:
                report.sku_changes.append((
                    index, result.requested_sku, result.actual_sku,
                ))
        except Exception as exc:  # noqa: BLE001 — surface the message to the UI
            # Atrapamos todo a propósito: una sola fila mala no debe
            # frenar la importación. El error se reporta por fila en
            # el resultado para que el usuario pueda arreglar el archivo
            # y reintentar.
            report.errors.append((index, str(exc)))
            #si hay error incrementamos el contador de errores y saltamos a la siguiente fila
            report.skipped += 1
    #se borra el cache de alertas para que se actualice
    cache.delete(ALERT_COUNT_CACHE_KEY)

    return report


@dataclass
class _ImportRowResult:
    """Resultado de importar una sola fila del CSV.

    Por qué una dataclass y no una tupla:
        Las tuplas con más de 2 elementos son difíciles de leer en el
        call site (``r[2]`` no dice nada). Una dataclass con nombres
        explícitos hace el flujo del bucle mucho más claro.
    """
    created: bool
    requested_sku: str  # SKU que vino en el CSV ('' si no venía)
    actual_sku: str     # SKU final que tiene el producto en la BD


def _import_one_row(row: dict) -> _ImportRowResult:
    """Crea o actualiza un solo producto — envuelto en su propio savepoint.

    Por qué cada fila tiene su propio savepoint:
        ``update_or_create`` hace dos escrituras (un SELECT, después
        INSERT o UPDATE). Una carrera por unique-constraint o un error
        de BD a mitad de la escritura dejaría la transacción en estado
        roto y abortaría el resto del archivo. El ``with
        transaction.atomic()`` interior acota la falla a esta única fila.

    Manejo del SKU en cada fila:
        1. Si el CSV no trae SKU → se genera uno nuevo.
        2. Si el CSV trae un SKU que NO existe → se usa tal cual.
        3. Si el CSV trae un SKU que SÍ existe → se genera uno nuevo
           (no se pisa el existente, para no perder datos).
    """
    requested_sku = row.get('sku', '').strip()

    category_name = row.get('category')
    if not category_name:
        raise ValueError('category es obligatoria')

    # Auto-crear la categoría/proveedor al vuelo. Es práctico para
    # importaciones chicas pero un poco riesgoso en importaciones
    # grandes — si la columna category tiene un typo terminás con
    # una categoría nueva cada vez.
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

    # Resolver el SKU final ANTES de update_or_create para no entrar
    # en la rama de "update" cuando en realidad queríamos "create".
    final_sku = _resolve_sku_for_import(requested_sku, category)

    defaults = {
        'name': row.get('name') or final_sku,
        'description': row.get('description', ''),
        'category': category,
        'supplier': supplier_obj,
        'unit': row.get('unit', 'unidad') or 'unidad',
        'unit_price': unit_price,
        'quantity': quantity,
        'minimum_stock': minimum_stock,
        'is_active': _parse_bool(row.get('is_active'), default=True),
    }

    # Cada fila en su propio savepoint — una fila mala no hace
    # rollback del resto.
    with transaction.atomic():
        _, created = Product.objects.update_or_create(sku=final_sku, defaults=defaults)
        return _ImportRowResult(
            created=created,
            requested_sku=requested_sku,
            actual_sku=final_sku,
        )


def _resolve_sku_for_import(requested_sku: str, category: Category) -> str:
    """Decide qué SKU usar para una fila del CSV.

    Reglas:
        1. ``requested_sku`` vacío → generar correlativo.
        2. ``requested_sku`` lleno y NO existe en la BD → usar tal cual.
        3. ``requested_sku`` lleno y SÍ existe → generar correlativo.

    Por qué este helper existe separado de ``generate_next_sku``:
        ``generate_next_sku`` siempre genera. Este necesita el
        branching de "usar el del CSV o generar", que es específico
        del importador. Si en el futuro querés que otras partes
        tengan la misma lógica, la extraés.
    """
    if not requested_sku:
        # Caso 1: CSV sin SKU.
        return generate_next_sku(category)

    if not Product.objects.filter(sku=requested_sku).exists():
        # Caso 2: CSV con SKU único. Usar tal cual.
        return requested_sku

    # Caso 3: CSV con SKU que ya existe. Generar uno nuevo.
    return generate_next_sku(category)

#funcion para parsear booleanos que vienen como string 
def _parse_bool(value: str, default: bool = False) -> bool:

    if value is None or value == '':
        return default
    return value.strip().lower() in ('1', 'true', 'yes', 'y', 't')
