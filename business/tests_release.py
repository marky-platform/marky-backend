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


class StartScriptTests(TestCase):
    """Ejecuta scripts/start.sh con `python`/`gunicorn` falsos en el PATH."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        import pathlib
        import tempfile
        cls.script = pathlib.Path(__file__).resolve().parent.parent / 'scripts' / 'start.sh'
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)
        cls.bin = pathlib.Path(cls.tmp.name)
        cls.log = cls.bin / 'calls.log'

    def _fake(self, name, body):
        path = self.bin / name
        path.write_text('#!/bin/sh\n' + body)
        path.chmod(0o755)

    def _run(self, release_exit=0, skip=False):
        import subprocess
        self.log.write_text('')
        self._fake('python', f'echo "python $*" >> "{self.log}"\nexit {release_exit}\n')
        self._fake('gunicorn', f'echo "gunicorn $*" >> "{self.log}"\n')
        env = {'PATH': f'{self.bin}:/usr/bin:/bin', 'PORT': '8123'}
        if skip:
            env['SKIP_RELEASE'] = '1'
        proc = subprocess.run(['sh', str(self.script)], env=env, capture_output=True, text=True)
        return proc, self.log.read_text().splitlines()

    def test_syntax(self):
        import subprocess
        subprocess.run(['sh', '-n', str(self.script)], check=True)

    def test_release_runs_before_gunicorn(self):
        proc, calls = self._run()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0], 'python manage.py release')
        self.assertTrue(calls[1].startswith('gunicorn marky_backend.wsgi:application'))
        self.assertIn('--bind 0.0.0.0:8123', calls[1])
        self.assertIn('[start] release OK', proc.stdout)

    def test_skip_release(self):
        proc, calls = self._run(skip=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0].startswith('gunicorn'))
        self.assertIn('SKIP_RELEASE=1', proc.stdout)

    def test_failing_release_blocks_gunicorn(self):
        proc, calls = self._run(release_exit=1)
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(calls, ['python manage.py release'])
