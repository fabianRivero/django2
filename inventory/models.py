from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import models
from django.urls import reverse
from django.utils.translation import gettext_lazy as _

class Category(models.Model):
    #es el modelo de la categoría del porducto del inventario
    name = models.CharField(max_length=120, unique=True)
    description = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ('name',)
        verbose_name = _('Categoria')
        verbose_name_plural = _('Categorias')

    def __str__(self) -> str:
        return self.name

    def get_absolute_url(self) -> str:
        return reverse('inventory:category_detail', args=[self.pk])


class Supplier(models.Model):
    #es el modelo del proveedor del producto del inventario
    name = models.CharField(max_length=150)
    notes = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ('name',)
        verbose_name = _('Proveedor')
        verbose_name_plural = _('Proveedores')

    def __str__(self) -> str:
        return self.name

    def get_absolute_url(self) -> str:
        return reverse('inventory:supplier_detail', args=[self.pk])


class Product(models.Model):
    #es el modelo del producto del inventario
    UNIT_CHOICES = (
        ('unidad', _('Unidad')),
        ('caja', _('Caja')),
        ('kg', _('Kilogramo')),
        ('g', _('Gramo')),
        ('L', _('Litro')),
        ('m', _('Metro')),
    )

    sku = models.CharField(
        max_length=32,
        unique=True,
        help_text=_('Código único del producto'),
    )
    name = models.CharField(max_length=180)
    description = models.TextField(blank=True)
    category = models.ForeignKey(
        Category,
        on_delete=models.PROTECT,
        related_name='productos',
    )
    supplier = models.ForeignKey(
        Supplier,
        on_delete=models.SET_NULL,
        related_name='proveedores',
        null=True,
        blank=True,
    )
    unit = models.CharField(max_length=8, choices=UNIT_CHOICES, default='unidad')
    unit_price = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal('0.00'),
    )
    quantity = models.PositiveIntegerField(
        default=0,
        help_text=_('Stock actual'),
    )
    minimum_stock = models.PositiveIntegerField(
        default=5,
        help_text=_('Stock minimo'),
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ('name',)
        verbose_name = _('Producto')
        verbose_name_plural = _('Productos')
        indexes = [
            models.Index(fields=['sku']),
            models.Index(fields=['name']),
            models.Index(fields=['category']),
            models.Index(fields=['supplier']),
        ]

    def __str__(self) -> str:
        return f'{self.name} ({self.sku})'

    def get_absolute_url(self) -> str:
        return reverse('inventory:product_detail', args=[self.pk])

    def save(self, *args, **kwargs):
        is_creating = self._state.adding

        if not self.sku and self.category_id:
            from .services import generate_next_sku
            self.sku = generate_next_sku(self.category)

        super().save(*args, **kwargs)

        if is_creating and self.quantity > 0:
            from .services import INITIAL_STOCK_REASON
            from .models import StockMovement
            StockMovement.objects.create(
                product=self,
                movement_type='IN',
                quantity=self.quantity,
                reason=INITIAL_STOCK_REASON,
            )

    def delete(self, *args, **kwargs):
        from .services import PRODUCT_DELETED_REASON

        if self.quantity > 0:
            StockMovement.objects.create(
                product=self,
                movement_type='OUT',
                quantity=self.quantity,
                reason=PRODUCT_DELETED_REASON,
            )

        return super().delete(*args, **kwargs)

    @property
    def is_low_stock(self) -> bool:
        #True cuando el stock está en el umbral mínimo o por debajo.
        return self.quantity <= self.minimum_stock

    @property
    def is_out_of_stock(self) -> bool:
        #True cuando el stock es 0.
        return self.quantity == 0

    @property
    def stock_status(self) -> str:
        if self.is_out_of_stock:
            return 'out'
        if self.is_low_stock:
            return 'low'
        return 'ok'

class StockMovement(models.Model):
    #es el modelo del movimiento de stock del producto del inventario
    MOVEMENT_TYPE_CHOICES = (
        ('IN', _('Entrada (compra / devolución)')),
        ('OUT', _('Salida (venta / consumo)')),
        ('ADJ', _('Ajuste (corrección manual)')),
    )

    product = models.ForeignKey(
        Product,
        on_delete=models.SET_NULL,
        related_name='movimientos',
        null=True,
        blank=True,
    )
    product_sku = models.CharField(max_length=32, blank=True)
    product_name = models.CharField(max_length=180, blank=True)
    movement_type = models.CharField(
        max_length=3,
        choices=MOVEMENT_TYPE_CHOICES,
    )
    quantity = models.PositiveIntegerField(
        help_text=_('Cantidad movida'),
    )
    reason = models.CharField(max_length=255, blank=True)
    reference = models.CharField(
        max_length=80,
        blank=True,
        help_text=_('Ej: factura, orden de venta, etc.'),
    )
    supplier = models.ForeignKey(
        Supplier,
        on_delete=models.SET_NULL,
        related_name='movimientos_proveedor',
        null=True,
        blank=True,
        help_text=_('Útil para entradas (compras)'),
    )
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(
        'auth.User',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='movimientos_registrados',
    )

    class Meta:
        ordering = ('-created_at',)
        verbose_name = _('Movimiento de Stock')
        verbose_name_plural = _('Movimientos de Stock')
        indexes = [
            models.Index(fields=['-created_at']),
            models.Index(fields=['movement_type']),
            models.Index(fields=['product', '-created_at']),
        ]

    def __str__(self) -> str:
        sign = '+' if self.movement_type == 'IN' else '-' if self.movement_type == 'OUT' else '~'
        return f'{sign}{self.quantity} {self.display_sku} @ {self.created_at:%Y-%m-%d %H:%M}'

    def save(self, *args, **kwargs):
        if self.product_id and not self.product_sku:
            self.product_sku = self.product.sku
        if self.product_id and not self.product_name:
            self.product_name = self.product.name
        super().save(*args, **kwargs)

    @property
    def display_sku(self) -> str:
        if self.product_id and self.product:
            return self.product.sku
        return self.product_sku or '—'

    @property
    def display_name(self) -> str:
        if self.product_id and self.product:
            return self.product.name
        return self.product_name or '—'

    @property
    def is_orphan(self) -> bool:
        return self.product_id is None

    @property
    def signed_quantity(self) -> int:
        if self.movement_type == 'IN':
            return self.quantity
        if self.movement_type == 'OUT':
            return -self.quantity
        return 0

    def clean(self) -> None:

        super().clean()
        if self.pk is None and self.movement_type == 'OUT':
            if self.product_id and self.product.quantity < self.quantity:
                raise ValidationError({
                    'quantity': _(
                        'No se pueden retirar %(qty)d unidades. Solo %(stock)d disponibles para %(sku)s.'
                    ) % {
                        'qty': self.quantity,
                        'stock': self.product.quantity,
                        'sku': self.product.sku,
                    },
                })