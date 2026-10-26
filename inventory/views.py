from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import LoginView, LogoutView
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from .forms import (
    CategoryForm,
    CsvImportForm,
    ProductFilterForm,
    ProductForm,
    StockMovementForm,
    SupplierForm,
)
from .models import Category, Product, StockMovement, Supplier
from .services import (
    import_products_from_csv,
    low_stock_products,
    movement_chart_data,
    products_for_list,
    record_movement,
)


# ---------------------------------------------------------------------------
# Autenticación
# ---------------------------------------------------------------------------

class AppLoginView(LoginView):
    template_name = 'inventory/login.html'
    redirect_authenticated_user = True

    def get_success_url(self):

        return self.get_redirect_url() or reverse('inventory:dashboard')

    def form_invalid(self, form):
        messages.error(
            self.request,
            'Credenciales inválidas. Revisá tu usuario y contraseña.',
        )
        return super().form_invalid(form)


class AppLogoutView(LogoutView):
    # Tras cerrar sesión redirigimos a /login/ y no al dashboard
    
    next_page = 'inventory:login'

    def post(self, request, *args, **kwargs):
        messages.success(request, 'Sesión cerrada correctamente.')
        return super().post(request, *args, **kwargs)


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

def dashboard(request):

    context = {
        'total_products': Product.objects.filter(is_active=True).count(),
        'total_categories': Category.objects.count(),
        'total_suppliers': Supplier.objects.filter(is_active=True).count(),
        'low_stock': low_stock_products()[:10],
        'recent_movements': (
            StockMovement.objects
            .select_related('product', 'created_by')
            .order_by('-created_at')[:8]
        ),
    }
    chart_days = int(request.GET.get('chart_days', '7'))
    chart_days = max(7, min(chart_days, 90))
    labels, in_series, out_series = movement_chart_data(days=chart_days)
    context.update({
        'chart_days': chart_days,
        'chart_labels': labels,
        'chart_in': in_series,
        'chart_out': out_series,
    })
    return render(request, 'inventory/dashboard.html', context)


# ---------------------------------------------------------------------------
# Products
# ---------------------------------------------------------------------------

def product_list(request):
    #Lista productos con filtros (proveedor, categoría, búsqueda).
    form = ProductFilterForm(request.GET or None)
    if form.is_valid():
        cd = form.cleaned_data
        qs = products_for_list(
            supplier=cd.get('supplier'),
            category=cd.get('category'),
            search=cd.get('search') or '',
            show_inactive=cd.get('show_inactive', False),
        )
    else:
        qs = products_for_list()

    #Paginador de los productos
    paginator = Paginator(qs, 25)
    page = paginator.get_page(request.GET.get('page'))

    #lo de low stock count es solo para el encabezado de la tabla
    return render(request, 'inventory/product_list.html', {
        'page': page,
        'filter_form': form,
        'low_stock_count': sum(1 for p in page.object_list if p.is_low_stock),
    })

#view para crear un nuevo producto o renderizar el formulario
@login_required
def product_create(request):
    if request.method == 'POST':
        form = ProductForm(request.POST)
        if form.is_valid():
            product = form.save()
            messages.success(
                request,
                f'Producto "{product.name}" creado con SKU {product.sku}.',
            )
            return redirect('inventory:product_detail', pk=product.pk)
    else:
        form = ProductForm()
    return render(request, 'inventory/product_form.html', {
        'form': form,
        'title': 'Nuevo producto',
    })

#view para actualizar un producto o renderizar el formulario
@login_required
def product_update(request, pk: int):
    product = get_object_or_404(Product, pk=pk)
    if request.method == 'POST':
        form = ProductForm(request.POST, instance=product)
        if form.is_valid():
            form.save()
            messages.success(request, f'Producto "{product.name}" actualizado.')
            return redirect('inventory:product_detail', pk=product.pk)
    else:
        form = ProductForm(instance=product)
    return render(request, 'inventory/product_form.html', {
        'form': form,
        'title': f'Editar {product.name}',
        'product': product,
    })

#view para mostrar los detalles de un producto y sus movimientos
def product_detail(request, pk: int):
    product = get_object_or_404(Product.objects.select_related('category', 'supplier'), pk=pk)
    movements = product.movimientos.select_related('supplier', 'created_by').order_by('-created_at')[:25]
    return render(request, 'inventory/product_detail.html', {
        'product': product,
        'movements': movements,
    })

#view para eliminar un producto
@login_required
@require_POST
def product_delete(request, pk: int):
    #Si tenía stock, ``Product.delete()`` registra automáticamente una salida con esa cantidad 
    

    product = get_object_or_404(Product, pk=pk)
    # Guardamos el stock antes de borrar para poder mencionarlo en el mensaje de confirmación.
    stock = product.quantity
    name = product.name
    product.delete()
    if stock > 0:
        messages.success(
            request,
            f'Producto "{name}" eliminado. Se registró una salida de '
            f'{stock} unidades.',
        )
    else:
        messages.success(request, f'Producto "{name}" eliminado.')
    return redirect('inventory:product_list')


# ---------------------------------------------------------------------------
# Stock movements
# ---------------------------------------------------------------------------

#view para registrar un movimiento de stock o renderizar el formulario
@login_required
def movement_create(request, pk: int):

    product = get_object_or_404(Product, pk=pk)
    if request.method == 'POST':
        form = StockMovementForm(request.POST)
        if form.is_valid():
            try:
                result = record_movement(
                    product=product,
                    movement_type=form.cleaned_data['movement_type'],
                    quantity=form.cleaned_data['quantity'],
                    reason=form.cleaned_data.get('reason', ''),
                    reference=form.cleaned_data.get('reference', ''),
                    supplier=form.cleaned_data.get('supplier'),
                    user=request.user if request.user.is_authenticated else None,
                )
            except ValidationError as exc:
                error_text = '; '.join(
                    f'{field}: {", ".join(msgs)}' for field, msgs in exc.message_dict.items()
                ) or str(exc)
                messages.error(request, f'No se pudo registrar el movimiento: {error_text}')
                return render(request, 'inventory/movement_form.html', {
                    'form': form,
                    'product': product,
                })
            # Si el movimiento provocó que el stock caiga en o por debajo del mínimo, mostramos un mensaje de alerta
            if result.triggered_low_stock and result.movement.movement_type == 'OUT':
                messages.warning(
                    request,
                    f'⚠️ "{product.name}" quedó en {product.quantity} unidades '
                    f'(mínimo {product.minimum_stock}).',
                )
            else:
                messages.success(request, 'Movimiento de stock registrado.')
            return redirect('inventory:product_detail', pk=product.pk)
    else:
        form = StockMovementForm(initial={'movement_type': request.GET.get('type', 'IN')})
    return render(request, 'inventory/movement_form.html', {
        'form': form,
        'product': product,
    })

#view para listar los movimientos de stock.
def movement_list(request):
    qs = (
        StockMovement.objects
        .select_related('product', 'supplier', 'created_by')
        .order_by('-created_at')
    )

    movement_type = request.GET.get('type')
    if movement_type in ('IN', 'OUT', 'ADJ'):
        qs = qs.filter(movement_type=movement_type)

    product_id = request.GET.get('product')
    if product_id and product_id.isdigit():
        qs = qs.filter(product_id=int(product_id))

    supplier_id = request.GET.get('supplier')
    if supplier_id and supplier_id.isdigit():
        qs = qs.filter(supplier_id=int(supplier_id))

    paginator = Paginator(qs, 50)
    page = paginator.get_page(request.GET.get('page'))
    return render(request, 'inventory/movement_list.html', {
        'page': page,
        'suppliers': Supplier.objects.filter(is_active=True),
        'products': Product.objects.filter(is_active=True).order_by('name'),
        'selected_type': movement_type or '',
        'selected_supplier': int(supplier_id) if (supplier_id and supplier_id.isdigit()) else None,
        'selected_product': int(product_id) if (product_id and product_id.isdigit()) else None,
    })


# ---------------------------------------------------------------------------
# Categories
# ---------------------------------------------------------------------------

#view para listar las categorías
def category_list(request):
    categories = Category.objects.annotate(product_count=models_count('productos'))
    return render(request, 'inventory/category_list.html', {
        'categories': categories.order_by('name'),
    })

#view para crear una categoría
@login_required
def category_create(request):
    if request.method == 'POST':
        form = CategoryForm(request.POST)
        if form.is_valid():
            category = form.save()
            messages.success(request, f'Categoría "{category.name}" creada.')
            return redirect('inventory:category_list')
    else:
        form = CategoryForm()
    return render(request, 'inventory/category_form.html', {'form': form, 'title': 'Nueva categoría'})

#view para mostrar los detalles de una categoría
def category_detail(request, pk: int):
    category = get_object_or_404(Category, pk=pk)
    products = category.productos.select_related('supplier').order_by('name')
    return render(request, 'inventory/category_detail.html', {
        'category': category,
        'products': products,
    })

#view para eliminar una categoría
@login_required
@require_POST
def category_delete(request, pk: int):
    category = get_object_or_404(Category, pk=pk)
    try:
        category.delete()
        messages.success(request, 'Categoría eliminada.')
    except Exception as exc:
        messages.error(request, f'No se pudo eliminar la categoría: {exc}')
    return redirect('inventory:category_list')


# ---------------------------------------------------------------------------
# Suppliers
# ---------------------------------------------------------------------------

#view para listar los proveedores
def supplier_list(request):
    suppliers = Supplier.objects.annotate(product_count=models_count('proveedores'))
    return render(request, 'inventory/supplier_list.html', {
        'suppliers': suppliers.order_by('name'),
    })

#view para crear un proveedor
@login_required
def supplier_create(request):
    if request.method == 'POST':
        form = SupplierForm(request.POST)
        if form.is_valid():
            supplier = form.save()
            messages.success(request, f'Proveedor "{supplier.name}" creado.')
            return redirect('inventory:supplier_list')
    else:
        form = SupplierForm()
    return render(request, 'inventory/supplier_form.html', {'form': form, 'title': 'Nuevo proveedor'})

#view para actualizar un proveedor
@login_required
def supplier_update(request, pk: int):
    supplier = get_object_or_404(Supplier, pk=pk)
    if request.method == 'POST':
        form = SupplierForm(request.POST, instance=supplier)
        if form.is_valid():
            form.save()
            messages.success(request, 'Proveedor actualizado.')
            return redirect('inventory:supplier_list')
    else:
        form = SupplierForm(instance=supplier)
    return render(request, 'inventory/supplier_form.html', {
        'form': form,
        'title': f'Editar {supplier.name}',
        'supplier': supplier,
    })

#view para mostrar los detalles de un proveedor
def supplier_detail(request, pk: int):
    supplier = get_object_or_404(Supplier, pk=pk)
    products = supplier.proveedores.select_related('category').order_by('name')
    movements = supplier.movimientos_proveedor.select_related('product').order_by('-created_at')[:25]
    return render(request, 'inventory/supplier_detail.html', {
        'supplier': supplier,
        'products': products,
        'movements': movements,
    })


# ---------------------------------------------------------------------------
# CSV import
# ---------------------------------------------------------------------------

#view para importar productos desde un archivo CSV
@login_required
def csv_import(request):
    report = None
    if request.method == 'POST':
        #CsvImportForm se une en la importacion de productos porque permite la subida de un archivo csv
        form = CsvImportForm(request.POST, request.FILES)
        if form.is_valid():
            try:
                report = import_products_from_csv(request.FILES['csv_file'])
            except ValueError as exc:
                # Formato de CSV malo (vacío, columnas faltantes, etc.)
                messages.error(request, f'Error al importar: {exc}')
                return render(request, 'inventory/csv_import.html', {'form': form})
            except Exception as exc:  
                messages.error(request, f'Error inesperado al importar: {exc}')
                return render(request, 'inventory/csv_import.html', {'form': form})

            # Elegir el nivel del mensaje según lo que pasó:
            #   success: se creó o actualizó algo, y no hay errores por fila
            #   warning: al menos una fila se salteó (errores por fila visibles)
            #   info: no pasó nada (probablemente archivo vacío)
            level = (
                messages.success if (report.created or report.updated) and not report.errors
                else messages.warning if report.errors
                else messages.info
            )
            level(
                request,
                f'Importación finalizada — creados: {report.created}, '
                f'actualizados: {report.updated}, omitidos: {report.skipped}.',
            )
            # Resetear el form para la próxima carga pero mantener el reporte visible.
            return render(request, 'inventory/csv_import.html', {
                'form': CsvImportForm(),
                'report': report,
            })
    else:
        form = CsvImportForm()
    return render(request, 'inventory/csv_import.html', {'form': form, 'report': report})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

#helper para agregar el conteo de elementos de modelos relacionados
def models_count(related_name: str):
    from django.db.models import Count
    return Count(related_name)
