"""
Context processors para templates que necesitan mostrar datos en cada request.

Exponer un pedacito de estado (el conteo de alertas de stock bajo) vía
un context processor mantiene las vistas finas y asegura que el badge
de la navbar sea consistente en todo el sitio — incluso en páginas
que por lo demás no necesitan saber de inventario.

Por qué un context processor y no una variable por vista:
    Un middleware tendría que setear ``request.low_stock_count`` y
    después cada vista tendría que acordarse de pasarlo. Un context
    processor corre en cada render automáticamente. La documentación
    de Django recomienda este patrón para datos transversales como
    el usuario actual o, en nuestro caso, el conteo de alertas.
"""
from django.core.cache import cache

#: Clave de caché para el conteo de alertas de stock bajo.
#: Se exporta para que otros módulos (services.record_movement,
#: services.import_products_from_csv) puedan invalidar la misma clave
#: que usa el lector. Mantenerlas en sincronía — un typo acá causa
#: bugs de "badge stale" difíciles de detectar.
ALERT_COUNT_CACHE_KEY = 'inventory:low_stock_count'

#: TTL de caché en segundos. Dos minutos es un default sano: absorbe
#: picos de requests sin mostrar datos stale por mucho tiempo.
#: Invalidamos con entusiasmo en los cambios de stock para que el
#: usuario sienta "tiempo real".
ALERT_COUNT_CACHE_TTL = 120  # segundos


def low_stock_alerts(request):
    """Expone el conteo de alertas de stock bajo y su desglose a todos los templates.

    Retorna:
        dict con:
          - low_stock_count: total de productos en alerta (int)
          - low_stock_out:   cantidad de esos que están agotados (int)
          - low_stock_low:   cantidad de los que están en mínimo pero > 0 (int)

    Estrategia de caché:
        - Cache hit  → return inmediato (sin queries a la BD).
        - Cache miss → 2 consultas count(), después cachear por
          ``ALERT_COUNT_CACHE_TTL`` segundos.

        Puntos de invalidación (ver services.py):
        - ``record_movement`` (cada IN/OUT/ADJ)
        - ``import_products_from_csv`` (fin de la importación bulk)
    """
    cached = cache.get(ALERT_COUNT_CACHE_KEY)
    if cached is not None:
        return cached

    # Imports locales: evitamos cargar modelos al momento de init de la
    # app para que este processor sea seguro incluso si 'inventory' no
    # está en INSTALLED_APPS.
    from django.db.models import F
    from .models import Product

    qs = Product.objects.filter(is_active=True, quantity__lte=F('minimum_stock'))
    low_count = qs.filter(quantity__gt=0).count()
    out_count = qs.filter(quantity=0).count()
    payload = {
        'low_stock_count': low_count + out_count,
        'low_stock_out': out_count,
        'low_stock_low': low_count,
    }
    cache.set(ALERT_COUNT_CACHE_KEY, payload, ALERT_COUNT_CACHE_TTL)
    return payload
