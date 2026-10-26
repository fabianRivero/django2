from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction

from inventory.models import Category, Product, StockMovement, Supplier
from inventory.services import record_movement


# ---------------------------------------------------------------------------
# Fixtures — mantenerlas chicas pero diversas: 1-2 OK, 1 BAJO, 1 AGOTADO
# así el panel tiene algo interesante para mostrar apenas se siembra.
# ---------------------------------------------------------------------------

CATEGORIES = [
    ('Electronics', 'Phones, laptops, peripherals'),
    ('Office Supplies', 'Paper, pens, organizers'),
    ('Furniture', 'Chairs, desks, storage'),
]

SUPPLIERS = [
    {'name': 'TechSource', 'contact_name': 'Anna Lee', 'email': 'anna@techsource.example'},
    {'name': 'Office Depot', 'contact_name': 'Mark Chen', 'email': 'mark@officedepot.example'},
    {'name': 'Comfort Living', 'contact_name': 'Júlia Pérez', 'email': 'julia@comfortliving.example'},
]

# Tuplas de (sku, name, category, supplier, qty, min_stock, unit, unit_price).
# Notar que ``OF-2002`` (qty 4, min 10) y ``EL-1003`` (qty 3, min 5) están
# por debajo de su mínimo — es intencional, le da al panel de alertas
# algo para mostrar.
PRODUCTS = [
    # (sku, name, category, supplier_key, qty, min_stock, unit, unit_price)
    ('EL-1001', 'Wireless Mouse', 'Electronics', 'TechSource', 45, 10, 'unidad', '12.50'),
    ('EL-1002', '27" Monitor', 'Electronics', 'TechSource', 8, 5, 'unidad', '189.00'),
    ('EL-1003', 'Mechanical Keyboard', 'Electronics', 'TechSource', 3, 5, 'unidad', '79.99'),
    ('OF-2001', 'A4 Paper Ream', 'Office Supplies', 'Office Depot', 60, 20, 'unidad', '4.75'),
    ('OF-2002', 'Blue Ink Pen (box)', 'Office Supplies', 'Office Depot', 4, 10, 'caja', '6.20'),
    ('FU-3001', 'Office Chair', 'Furniture', 'Comfort Living', 12, 4, 'unidad', '129.00'),
    ('FU-3002', 'Standing Desk', 'Furniture', 'Comfort Living', 0, 2, 'unidad', '399.00'),
]


class Command(BaseCommand):
    help = 'Carga la base con categorías, proveedores, productos y algunos movimientos de stock.'

    @transaction.atomic
    def handle(self, *args, **options):
        # --- Categorías ---
        for name, description in CATEGORIES:
            Category.objects.get_or_create(name=name, defaults={'description': description})
            self.stdout.write(self.style.SUCCESS(f'Category: {name}'))

        # --- Proveedores ---
        for data in SUPPLIERS:
            Supplier.objects.get_or_create(name=data['name'], defaults=data)
            self.stdout.write(self.style.SUCCESS(f'Supplier: {data["name"]}'))

        # Cachear proveedores y categorías en dicts para que el loop de
        # abajo no dispare un SELECT por fila.
        cats = {c.name: c for c in Category.objects.all()}
        sups = {s.name: s for s in Supplier.objects.all()}

        # --- Productos (+ un par de movimientos semilla cada uno) ---
        for sku, name, cat_key, sup_key, qty, min_stock, unit, unit_price in PRODUCTS:
            product, _ = Product.objects.update_or_create(
                sku=sku,
                defaults={
                    'name': name,
                    'category': cats[cat_key],
                    'supplier': sups[sup_key],
                    'quantity': qty,
                    'minimum_stock': min_stock,
                    'unit': unit,
                    'unit_price': Decimal(unit_price),
                    'is_active': True,
                },
            )
            self.stdout.write(self.style.SUCCESS(f'Product: {product.sku} ({product.name})'))

            # Si es un producto nuevo, loguear un par de movimientos
            # también — así el gráfico del panel y la página de detalle
            # tienen algo que mostrar. Usamos ``record_movement`` (el
            # servicio) en lugar de crear filas de StockMovement
            # directamente, para que el Product.quantity cacheado se
            # mantenga en sincronía.
            if not product.movimientos.exists():
                record_movement(
                    product=product,
                    movement_type='IN',
                    quantity=qty + 10,
                    reason='Initial stock',
                    supplier=sups[sup_key],
                )
                if qty < (qty + 10):
                    record_movement(
                        product=product,
                        movement_type='OUT',
                        quantity=10,
                        reason='Demo sale',
                    )

        self.stdout.write(self.style.SUCCESS('Demo data loaded successfully.'))
