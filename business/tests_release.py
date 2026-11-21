from io import StringIO
from unittest import mock

from django.contrib.auth.models import Group
from django.core.cache import cache
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test import TestCase
from post_office.models import EmailTemplate

from business.models import BusinessCategory, Currency


def _counts():
    return (
        Group.objects.count(),
        Currency.objects.count(),
        BusinessCategory.objects.count(),
        EmailTemplate.objects.count(),
    )


class ReleaseCommandTests(TestCase):
    def _release(self):
        out = StringIO()
        call_command('release', stdout=out)
        return out.getvalue()

    def test_creates_cache_table_and_seeds(self):
        # El runner de tests ya crea la tabla; la borramos (DDL transaccional, se revierte) para probar el comando.
        with connection.cursor() as cursor:
            cursor.execute('DROP TABLE django_cache')
        self._release()
        self.assertIn('django_cache', connection.introspection.table_names())
        cache.set('release-test', 'ok', 30)
        self.assertEqual(cache.get('release-test'), 'ok')
        self.assertTrue(Group.objects.filter(name='business').exists())
        self.assertTrue(Currency.objects.filter(code='PYG').exists())
        self.assertTrue(BusinessCategory.objects.exists())
        self.assertTrue(EmailTemplate.objects.filter(name='verify_email').exists())

    def test_prints_one_line_per_step(self):
        output = self._release()
        for step in ('migrate', 'createcachetable', 'seed'):
            self.assertIn(f'[release] {step}', output)

    def test_idempotent(self):
        self._release()
        before = _counts()
        self._release()
        self.assertEqual(_counts(), before)

    def test_fails_loudly_when_a_step_fails(self):
        real = call_command

        def boom(name, *args, **kwargs):
            if name == 'createcachetable':
                raise RuntimeError('db down')
            return real(name, *args, **kwargs)

        with mock.patch('business.management.commands.release.call_command', side_effect=boom):
            with self.assertRaises(CommandError):
                call_command('release', stdout=StringIO())
