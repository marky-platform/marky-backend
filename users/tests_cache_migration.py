import importlib

from django.core.cache import cache
from django.db import connection
from django.test import TestCase, override_settings

migration = importlib.import_module('users.migrations.0006_create_cache_table')


class _Editor:
    connection = connection


def _forward():
    migration.create_cache_table(None, _Editor())


class CacheTableMigrationTests(TestCase):
    databases = {'default'}

    def _drop(self):
        with connection.cursor() as cursor:
            cursor.execute('DROP TABLE django_cache')

    def test_creates_missing_table(self):
        # DDL transaccional: el DROP se revierte al terminar el test.
        self._drop()
        _forward()
        self.assertIn('django_cache', connection.introspection.table_names())
        cache.set('mig-test', 'ok', 30)
        self.assertEqual(cache.get('mig-test'), 'ok')

    def test_idempotent(self):
        self._drop()
        _forward()
        _forward()
        self.assertIn('django_cache', connection.introspection.table_names())

    def test_noop_with_existing_table(self):
        _forward()
        self.assertIn('django_cache', connection.introspection.table_names())

    def test_noop_with_non_db_cache(self):
        self._drop()
        locmem = {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}}
        with override_settings(CACHES=locmem):
            _forward()
        self.assertNotIn('django_cache', connection.introspection.table_names())
