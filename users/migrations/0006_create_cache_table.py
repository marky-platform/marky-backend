from django.conf import settings
from django.core.management import call_command
from django.db import migrations

DB_CACHE_BACKEND = 'django.core.cache.backends.db.DatabaseCache'


def create_cache_table(apps, schema_editor):
    """Crea la tabla del cache en BD (throttling de DRF) si se usa DatabaseCache.

    createcachetable es idempotente: omite las tablas que ya existen. Con otro
    backend (p. ej. Redis vía CACHE_URL) no hace nada.
    """
    for config in settings.CACHES.values():
        if config.get('BACKEND') == DB_CACHE_BACKEND:
            call_command(
                'createcachetable',
                config['LOCATION'],
                database=schema_editor.connection.alias,
                verbosity=0,
            )


class Migration(migrations.Migration):

    dependencies = [
        ('users', '0005_user_verification_attempts'),
    ]

    operations = [
        migrations.RunPython(create_cache_table, migrations.RunPython.noop),
    ]
