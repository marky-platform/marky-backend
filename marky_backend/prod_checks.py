"""Validación de entorno para producción (DEBUG=False): falla al arrancar en vez de degradarse en silencio."""
from django.core.exceptions import ImproperlyConfigured

# Sin estas variables la app caería en SQLite efímero, media en disco local,
# ALLOWED_HOSTS=* o enlaces de email apuntando a localhost.
REQUIRED_PRODUCTION_ENV = (
    'DATABASE_URL',
    'HOST',
    'FRONTEND_URL',
    'RESEND_API_KEY',
    # Remitente de todos los correos; debe ser un remitente verificado en Resend
    'DEFAULT_FROM_EMAIL',
    # Cloudflare R2 (API compatible con S3)
    'AWS_ACCESS_KEY_ID',
    'AWS_SECRET_ACCESS_KEY',
    'AWS_STORAGE_BUCKET_NAME',
    'AWS_S3_ENDPOINT_URL',
)


def validate_production_env(environ):
    """Lanza ImproperlyConfigured si falta alguna variable obligatoria (vacía cuenta como faltante)."""
    missing = [name for name in REQUIRED_PRODUCTION_ENV if not (environ.get(name) or '').strip()]
    if missing:
        raise ImproperlyConfigured(
            'Con DEBUG=False faltan variables de entorno obligatorias: ' + ', '.join(missing)
        )
    hosts = [h.strip() for h in environ['HOST'].split(',') if h.strip()]
    if '*' in hosts:
        raise ImproperlyConfigured('HOST no puede ser "*" con DEBUG=False; listá los dominios reales.')
