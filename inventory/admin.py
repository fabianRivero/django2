"""
Registros del admin de Django para el dominio de inventario (en español).

Cada ModelAdmin es una declaración fina: list_display, filtros, búsqueda,
readonly. Lo no trivial es ``ProductAdmin.stock_indicator``, que
renderiza un badge coloreado de Bootstrap para que operaciones pueda
escanear un listado de 200 productos y ver al instante cuáles necesitan
atención.

Nota: el admin es una superficie *secundaria* para esta app — los
templates públicos (panel, listado de productos, listado de proveedores)
son el flujo principal. El admin es para correcciones de datos y
ediciones en bulk.
"""
from django.contrib import admin
from django.utils.html import format_html

from .models import Category, Product, StockMovement, Supplier


@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    """Las categorías son simples — sin acciones extra ni inlines."""
    list_display = ('name', 'product_count', 'description', 'updated_at')
    list_display_links = ('name',)
    search_fields = ('name',)
    readonly_fields = ('created_at', 'updated_at')

    @admin.display(description='Productos')
    def product_count(self, obj):
        # Usa la relación inversa en español desde Product.category.
        return obj.productos.count()


@admin.register(Supplier)
class SupplierAdmin(admin.ModelAdmin):
    """Los proveedores se filtran por ``is_active`` para poder ocultar archivados."""
    list_display = ('name', 'is_active', 'product_count')
    list_filter = ('is_active',)
    search_fields = ('name',)
    readonly_fields = ('created_at', 'updated_at')

    @admin.display(description='Productos')
    def product_count(self, obj):
        return obj.proveedores.count()


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    """Admin de productos — la pantalla más usada de este admin.

    ``stock_indicator`` es una columna basada en método que devuelve
    HTML seguro (vía ``format_html``) para que podamos renderizar el
    badge coloreado sin tener que marcar el campo como ``allow_tags``.
    """
    list_display = (
        'sku', 'name', 'category', 'supplier', 'quantity', 'minimum_stock',
        'stock_indicator', 'unit_price', 'is_active',
    )
    list_filter = ('category', 'supplier', 'unit', 'is_active')
    search_fields = ('sku', 'name')
    readonly_fields = ('created_at', 'updated_at')
    # ``autocomplete_fields`` es clave cuando el catálogo crece —
    # reemplaza el <select> del FK por un typeahead. Cae con gracia
    # a un select para conjuntos de datos chicos.
    autocomplete_fields = ('category', 'supplier')

    @admin.display(description='Estado de stock')
    def stock_indicator(self, obj):
        if obj.is_out_of_stock:
            badge, text = 'danger', 'AGOTADO'
        elif obj.is_low_stock:
            badge, text = 'warning', 'BAJO'
        else:
            badge, text = 'success', 'OK'
        return format_html(
            '<span class="badge bg-{}">{}</span> {}',
            badge, text, f'{obj.quantity}/{obj.minimum_stock}',
        )


@admin.register(StockMovement)
class StockMovementAdmin(admin.ModelAdmin):
    """Admin mayormente de lectura para el log de auditoría.

    Nota: este admin NO expone ``delete_selected``. Los movimientos son
    append-only por diseño — para "deshacer" uno, se registra un
    movimiento ADJ.
    """
    list_display = (
        'created_at', 'product', 'movement_type', 'quantity',
        'supplier', 'reason', 'created_by',
    )
    list_filter = ('movement_type', 'supplier', 'created_at')
    search_fields = ('product__sku', 'product__name', 'reference', 'reason')
    readonly_fields = ('created_at',)
    autocomplete_fields = ('product', 'supplier')
    date_hierarchy = 'created_at'
