"""
Formularios para la aplicación de inventario (labels en español).

Casi todos son ``ModelForm``s con dos customizaciones:
  - ``labels`` explícitos para que el texto renderizado esté en
    español, y
  - ``widgets`` con sabor a Bootstrap para que los inputs ya se vean
    bien cuando el template renderiza ``{{ field }}``.

El único que no es ModelForm es ``ProductFilterForm`` — es un
``Form`` plano porque es solo un bundle de parámetros GET, no
está atado a un modelo.
"""
from django import forms

from .models import Category, Product, StockMovement, Supplier


# Nombres de clases de Bootstrap. Reusados por cada form para no
# repetirlos en cada widget.
INPUT = {'class': 'form-control'}
SELECT = {'class': 'form-select'}


class ProductForm(forms.ModelForm):
    """Form para crear y editar productos.

    Manejo del SKU:
        - Creación: el campo NO se muestra. El SKU se genera
          automáticamente en ``Product.save()`` según la categoría
          (ver ``services.generate_next_sku``). El mensaje de éxito
          de la vista le muestra al usuario el SKU asignado.
        - Edición: el campo se muestra read-only (``disabled``). El
          SKU es un identificador, no un dato — cambiarlo rompe
          etiquetas impresas, referencias externas y la búsqueda
          mental de los usuarios. Para corregir un SKU mal cargado,
          el admin de Django sigue permitiéndolo (escape hatch).

    Por qué ``disabled`` y no sacar el campo en edición:
        Un campo ``disabled`` viaja en el POST pero Django ignora su
        valor y usa el ``initial`` (el SKU actual de la instancia) —
        ni siquiera un usuario malicioso forjando el POST puede
        cambiarlo. Además el usuario VE el SKU en contexto, lo cual
        es útil al editar.

    ``unit`` se renderiza a propósito como Select (no TextInput) para
    que el usuario elija de una lista fija — los nombres de unidad
    libres eran fuente de typos y reportes inconsistentes.
    """

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

        if self.instance and self.instance.pk:
            # Edición: SKU visible pero no editable. ``disabled`` hace
            # que el valor final venga siempre del ``initial`` (el SKU
            # actual), ignorando lo que venga en el POST.
            self.fields['sku'].disabled = True
            self.fields['sku'].help_text = (
                'Identificador único del producto — no editable desde este formulario.'
            )
        else:
            # Creación: el campo ni siquiera existe. El SKU se asigna
            # en ``Product.save()`` a partir de la categoría elegida.
            # ``del`` (y no ``exclude`` en Meta) para que la lógica
            # quede visible acá, junto a su justificación.
            del self.fields['sku']


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
            # ``min=1`` porque ``quantity`` es PositiveIntegerField —
            # un valor de 0 crearía un movimiento no-op en el log.
            'quantity': forms.NumberInput(attrs={**INPUT, 'min': '1'}),
            'reason': forms.TextInput(attrs=INPUT),
            'reference': forms.TextInput(attrs=INPUT),
            'supplier': forms.Select(attrs=SELECT),
        }


class ProductFilterForm(forms.Form):
    """Controles de filtrado para la vista de listado de productos.

    A diferencia de los otros forms, este no está atado a un modelo —
    su único trabajo es parsear los parámetros GET de la URL.
    Usamos ``required=False`` en todo así un form vacío (sin params)
    significa "mostrar todo".
    """

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
    """Form mínimo de un solo campo, solo para el widget de upload.

    Toda la validación real (columnas, tipos, errores por fila) vive
    en ``services.import_products_from_csv`` — este form existe
    únicamente para obtener el file input con estilo Bootstrap y un
    upload protegido por CSRF.
    """
    csv_file = forms.FileField(
        label='Archivo CSV',
        help_text=(
            'Columnas obligatorias: sku, name, category, quantity, minimum_stock. '
            'Opcionales: supplier, description, unit, unit_price, is_active.'
        ),
        widget=forms.ClearableFileInput(attrs={'class': 'form-control', 'accept': '.csv'}),
    )
