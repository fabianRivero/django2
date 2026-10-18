"""
Settings de Django para el proyecto inventory_system.

Configuración por entorno (12-factor):
    Todo valor sensible o dependiente del entorno se lee de variables
    de entorno con defaults seguros para desarrollo local. En Render
    (u otro PaaS) las vars las define ``render.yaml``.

    Variables soportadas:
      - ``DJANGO_SECRET_KEY``  → SECRET_KEY (en dev, un valor inseguro)
      - ``DJANGO_DEBUG``       → DEBUG ("False" en producción)
      - ``ALLOWED_HOSTS``      → lista separada por comas (ej. ".onrender.com")
      - ``DATABASE_URL``       → si está, se usa Postgres; si no, SQLite

Local:
    ``python manage.py runserver`` funciona sin setear nada: cae a
    SQLite + DEBUG=True + hosts de localhost.

Producción (Render):
    El blueprint ``render.yaml`` setea todas las vars automáticamente,
    genera un SECRET_KEY real, apunta DATABASE_URL al Postgres interno
    y pone DJANGO_DEBUG=False.
"""

import os
from pathlib import Path

# Construir paths dentro del proyecto como BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent

# `dj_database_url` solo se importa si hay DATABASE_URL. Esto permite
# que un dev sin el paquete instalado (y sin DATABASE_URL) pueda correr
# ``makemigrations`` o ``runserver`` sin que ``settings.py`` explote al
# cargarse. En producción (DATABASE_URL presente) sí lo necesitamos
# y el ``requirements.txt`` lo garantiza.
DATABASE_URL_PRESENT = bool(os.environ.get('DATABASE_URL'))
if DATABASE_URL_PRESENT:
    import dj_database_url  # noqa: E402  (import condicional intencional)

# ---------------------------------------------------------------------------
# Núcleo de seguridad / entorno
# ---------------------------------------------------------------------------

# En producción viene de la env var (render.yaml usa generateValue).
# El default solo sirve para desarrollo local.
SECRET_KEY = os.environ.get(
    'DJANGO_SECRET_KEY',
    'django-insecure-change-me-in-production-9k3j4h5g6f7d8s9a',
)

# Cualquier valor de ("1", "true", "yes", "on") enciende DEBUG.
# Cualquier otra cosa (incluido "False") lo apaga.
DEBUG = os.environ.get('DJANGO_DEBUG', 'True').strip().lower() in ('1', 'true', 'yes', 'on')

# Hosts permitidos, separados por coma. El punto inicial (ej.
# ".onrender.com") cubre cualquier subdominio — es la forma que
# recomienda Render porque el hostname exacto no se sabe antes del
# primer deploy.
ALLOWED_HOSTS = [
    h.strip()
    for h in os.environ.get(
        'ALLOWED_HOSTS', 'localhost,127.0.0.1,[::1],testserver'
    ).split(',')
    if h.strip()
]


# Definición de la aplicación

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    # Apps locales
    'inventory',
]

# El stack estándar de middleware, con dos adiciones para producción:
# - whitenoise inmediatamente después de SecurityMiddleware: sirve
#   /static/ con DEBUG=False sin necesitar nginx.
MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'inventory_system.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                # Built-ins de Django:
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                # Del proyecto: badge de alerta de stock bajo para la navbar.
                'inventory.context_processors.low_stock_alerts',
            ],
        },
    },
]

WSGI_APPLICATION = 'inventory_system.wsgi.application'


# ---------------------------------------------------------------------------
# Base de datos
# ---------------------------------------------------------------------------
# Si viene DATABASE_URL (Render/Heroku/cualquier PaaS), se usa esa
# conexión — típicamente Postgres. Si no, cae a SQLite para desarrollo.
#
# ``conn_max_age=600`` reutiliza conexiones TCP entre requests en vez
# de abrir/cerrar por cada uno (recomendación directa de la docs de
# Render para Postgres).
if DATABASE_URL_PRESENT:
    # El import está arriba (línea condicional). Esta rama solo se
    # ejecuta si el paquete está instalado, lo cual está garantizado
    # por requirements.txt en cualquier deploy real.
    DATABASES = {
        'default': dj_database_url.config(conn_max_age=600),
    }
else:
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.sqlite3',
            'NAME': BASE_DIR / 'db.sqlite3',
        }
    }


# Validación de passwords — los defaults de Django son razonables para
# una herramienta interna. Si necesitás reglas más estrictas (largo >
# 8, sin patrones comunes, etc.), agregalas acá.
AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]


# Internacionalización
#
# Seteamos LANGUAGE_CODE = 'es' para que cualquier llamada a
# gettext_lazy() en los modelos se resuelva en español. Los labels
# estáticos en templates y forms ya están en español — no pasan por
# gettext.
LANGUAGE_CODE = 'es'
TIME_ZONE = 'UTC'
USE_I18N = True
USE_TZ = True


# ---------------------------------------------------------------------------
# Archivos estáticos (CSS, JavaScript, Imágenes)
# ---------------------------------------------------------------------------
# En dev, ``runserver`` sirve los estáticos directamente desde cada app
# (inventory/static/...). En producción, ``collectstatic`` los copia
# todos a STATIC_ROOT y whitenoise los sirve desde ahí.
STATIC_URL = 'static/'

# Destino de ``python manage.py collectstatic``. Está en .gitignore —
# es un artefacto de build, no código fuente.
STATIC_ROOT = BASE_DIR / 'staticfiles'

# ---------------------------------------------------------------------------
# Endurecimiento solo-producción
# ---------------------------------------------------------------------------
# Render termina TLS en su proxy y reenvía la request con el header
# ``X-Forwarded-Proto: https``. Con SECURE_PROXY_SSL_HEADER Django
# entiende que la conexión original fue HTTPS aunque entre al app como
# HTTP plano — sin esto, SECURE_SSL_REDIRECT causaría un loop infinito.
if not DEBUG:
    SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
    # Redirigir todo HTTP → HTTPS.
    SECURE_SSL_REDIRECT = True
    # Cookies solo por HTTPS.
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    # Django 4+ valida el header Origin de los POST contra
    # CSRF_TRUSTED_ORIGINS. Sin esto, el login del admin falla con 403
    # en producción. Se deriva de ALLOWED_HOSTS: un host que arranca
    # con "." (ej. ".onrender.com") se vuelve wildcard "https://*.onrender.com".
    CSRF_TRUSTED_ORIGINS = [
        f'https://*{host}' if host.startswith('.') else f'https://{host}'
        for host in ALLOWED_HOSTS
    ]
    # HSTS: fuerza al browser a usar HTTPS siempre para este dominio.
    # Off por default (0) porque activarlo mal es irreversible — el
    # browser recuerda el dominio por el tiempo seteado aunque después
    # lo apagues. Activá con SECURE_HSTS_SECONDS=31536000 (1 año)
    # cuando el dominio sea estable.
    SECURE_HSTS_SECONDS = int(os.environ.get('SECURE_HSTS_SECONDS', '0'))
    if SECURE_HSTS_SECONDS:
        SECURE_HSTS_INCLUDE_SUBDOMAINS = True

# Logout: a dónde redirigir después de /admin/logout/.
# Si no se setea, Django 4.1+ renderiza una página intermedia "Logged
# out" en vez de redirigir. Lo apuntamos al dashboard para una mejor UX.
LOGOUT_REDIRECT_URL = '/'

# Tipo de primary key por defecto para todos los modelos
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# Mapea los tags del framework de messages de Django a las clases CSS
# de alertas de Bootstrap. Sin este mapa, ``messages.warning`` se
# renderizaría como una caja gris genérica en vez de una amarilla.
from django.contrib.messages import constants as message_constants
MESSAGE_TAGS = {
    message_constants.DEBUG: 'secondary',
    message_constants.INFO: 'info',
    message_constants.SUCCESS: 'success',
    message_constants.WARNING: 'warning',
    message_constants.ERROR: 'danger',
}
