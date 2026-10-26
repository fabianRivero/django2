from django import forms

from .models import Category, Product, StockMovement, Supplier


# Nombres de clases de Bootstrap. Reusados por cada form para no
# repetirlos en cada widget.
INPUT = {'class': 'form-control'}
SELECT = {'class': 'form-select'}


class ProductForm(forms.ModelForm):

    class Meta:
        model = Product
        fields = (
            'sku', 'name', 'description', 'category', 'supplier',
            'unit', 'unit_price', 'quantity', 'minimum_stock', 'is_active',
        )
        labels = {
            'sku': 'SKU',
            'name': 'Nombre',
            'description': 'Descripción',
            'category': 'Categoría',
            'supplier': 'Proveedor',
            'unit': 'Unidad',
            'unit_price': 'Precio unitario',
            'quantity': 'Stock actual',
            'minimum_stock': 'Stock mínimo',
            'is_active': 'Activo',
        }
        widgets = {
            'sku': forms.TextInput(attrs=INPUT),
            'name': forms.TextInput(attrs=INPUT),
            'description': forms.Textarea(attrs={**INPUT, 'rows': 3}),
            'category': forms.Select(attrs=SELECT),
            'supplier': forms.Select(attrs=SELECT),
            'unit': forms.Select(attrs=SELECT),
            'unit_price': forms.NumberInput(attrs={**INPUT, 'step': '0.01', 'min': '0'}),
            'quantity': forms.NumberInput(attrs={**INPUT, 'min': '0'}),
            'minimum_stock': forms.NumberInput(attrs={**INPUT, 'min': '0'}),
            'is_active': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        is_editing = bool(self.instance and self.instance.pk)

        if is_editing:
            # Edición: SKU visible pero no editable. ``disabled`` hace
            # que el valor final venga siempre del ``initial`` (el SKU
            # actual), ignorando lo que venga en el POST.
            self.fields['sku'].disabled = True
            self.fields['sku'].help_text = (
                'Identificador único del producto — no editable desde este formulario.'
            )
            # El stock se edita desde acá
            self.fields['quantity'].disabled = True
            self.fields['quantity'].help_text = (
                'El stock se modifica registrando entradas y salidas. '
                'Usá el botón "+ Entrada" / "− Salida" del producto.'
            )
        else:
            del self.fields['sku']
            # En creación el stock si es editable, es el stock inicial
            self.fields['quantity'].help_text = (
                'Stock con el que arranca el producto. Se registrará '
                'automáticamente como una entrada de "Stock inicial".'
            )


class CategoryForm(forms.ModelForm):
    class Meta:
        model = Category
        fields = ('name', 'description')
        labels = {
            'name': 'Nombre',
            'description': 'Descripción',
        }
        widgets = {
            'name': forms.TextInput(attrs=INPUT),
            'description': forms.Textarea(attrs={**INPUT, 'rows': 3}),
        }


class SupplierForm(forms.ModelForm):
    class Meta:
        model = Supplier
        fields = ('name', 'notes', 'is_active')
        labels = {
            'name': 'Nombre',
            'notes': 'Notas',
            'is_active': 'Activo',
        }
        widgets = {
            'name': forms.TextInput(attrs=INPUT),
            'notes': forms.Textarea(attrs={**INPUT, 'rows': 3}),
            'is_active': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
        }


class StockMovementForm(forms.ModelForm):
    """Form usado tanto para entradas (IN) como para salidas (OUT).

    El tipo pre-seleccionado viene de ``?type=IN|OUT`` en la URL (ver
    vista ``movement_create``), así el mismo form se reusa para los
    dos flujos sin necesidad de un campo hidden.
    """

    class Meta:
        model = StockMovement
        fields = ('movement_type', 'quantity', 'reason', 'reference', 'supplier')
        labels = {
            'movement_type': 'Tipo de movimiento',
            'quantity': 'Cantidad',
            'reason': 'Motivo',
            'reference': 'Referencia',
            'supplier': 'Proveedor',
        }
        widgets = {
            'movement_type': forms.Select(attrs=SELECT),
            'quantity': forms.NumberInput(attrs={**INPUT, 'min': '1'}),
            'reason': forms.TextInput(attrs=INPUT),
            'reference': forms.TextInput(attrs=INPUT),
            'supplier': forms.Select(attrs=SELECT),
        }


class ProductFilterForm(forms.Form):

    supplier = forms.ModelChoiceField(
        queryset=Supplier.objects.filter(is_active=True),
        required=False,
        widget=forms.Select(attrs=SELECT),
        label='Proveedor',
    )
    category = forms.ModelChoiceField(
        queryset=Category.objects.all(),
        required=False,
        widget=forms.Select(attrs=SELECT),
        label='Categoría',
    )
    search = forms.CharField(
        required=False,
        widget=forms.TextInput(attrs={**INPUT, 'placeholder': 'Buscar por nombre o SKU'}),
        label='Buscar',
    )
    show_inactive = forms.BooleanField(
        required=False,
        widget=forms.CheckboxInput(attrs={'class': 'form-check-input'}),
        label='Mostrar inactivos',
    )


class CsvImportForm(forms.Form):

    csv_file = forms.FileField(
        label='Archivo CSV',
        help_text=(
            'Columnas obligatorias: sku, name, category, quantity, minimum_stock. '
            'Opcionales: supplier, description, unit, unit_price, is_active.'
        ),
        widget=forms.ClearableFileInput(attrs={'class': 'form-control', 'accept': '.csv'}),
    )
