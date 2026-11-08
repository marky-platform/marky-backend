import os
import subprocess
import sys

import json

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.exceptions import ImproperlyConfigured
from django.db import connection
from django.test import SimpleTestCase, TestCase, override_settings
from rest_framework.test import APIClient

from marky_backend.prod_checks import validate_production_env

# Entorno mínimo válido para arrancar con DEBUG=False (usado en subprocesos).
PROD_ENV = {
    'DEBUG': 'False',
    'SECRET_KEY': 'x' * 60 + '-prod-like-secret-key-with-enough-entropy-1234567890',
    'DATABASE_URL': 'postgis://u:p@db.example.com:5432/marky',
    'HOST': 'api.marky.one',
    'FRONTEND_URL': 'https://app.marky.one',
    'RESEND_API_KEY': 're_test_key',
    'AWS_ACCESS_KEY_ID': 'key',
    'AWS_SECRET_ACCESS_KEY': 'secret',
    'AWS_STORAGE_BUCKET_NAME': 'bucket',
    'AWS_S3_ENDPOINT_URL': 'https://acc.r2.cloudflarestorage.com',
    'NUM_PROXIES': '1',
}


def run_settings(overrides, code):
    """Ejecuta `code` en un subproceso con settings cargados bajo `overrides`."""
    environment = os.environ.copy()
    environment['DJANGO_SETTINGS_MODULE'] = 'marky_backend.settings'
    environment.update(overrides)
    return subprocess.run(
        [sys.executable, '-c', code],
        cwd=settings.BASE_DIR, env=environment, capture_output=True, text=True, check=False,
    )


class TestProductionLogging(SimpleTestCase):
    def test_django_request_errors_reach_stderr(self):
        environment = os.environ.copy()
        environment.update(PROD_ENV)
        environment['DJANGO_SETTINGS_MODULE'] = 'marky_backend.settings'
        result = subprocess.run(
            [
                sys.executable,
                '-c', (
                    'import django; '
                    'django.setup(); '
                    'import logging; '
                    "logging.getLogger('django.request').error('SENTINEL')"
                ),
            ],
            cwd=settings.BASE_DIR,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('SENTINEL', result.stderr)


class TestValidateProductionEnv(SimpleTestCase):
    def test_complete_env_passes(self):
        validate_production_env(PROD_ENV)

    def test_each_required_variable_is_enforced(self):
        required = ['DATABASE_URL', 'HOST', 'FRONTEND_URL', 'RESEND_API_KEY', 'AWS_ACCESS_KEY_ID',
                    'AWS_SECRET_ACCESS_KEY', 'AWS_STORAGE_BUCKET_NAME', 'AWS_S3_ENDPOINT_URL']
        for name in required:
            with self.subTest(name=name):
                incomplete = {k: v for k, v in PROD_ENV.items() if k != name}
                with self.assertRaises(ImproperlyConfigured) as ctx:
                    validate_production_env(incomplete)
                self.assertIn(name, str(ctx.exception))

    def test_blank_value_counts_as_missing(self):
        with self.assertRaises(ImproperlyConfigured):
            validate_production_env({**PROD_ENV, 'RESEND_API_KEY': '  '})

    def test_wildcard_host_rejected(self):
        with self.assertRaises(ImproperlyConfigured):
            validate_production_env({**PROD_ENV, 'HOST': '*'})


class TestProductionSettings(SimpleTestCase):
    def test_settings_fail_fast_without_required_env(self):
        result = run_settings(
            {**PROD_ENV, 'DATABASE_URL': '', 'RESEND_API_KEY': ''},
            'import django; django.setup()',
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('ImproperlyConfigured', result.stderr)
        self.assertIn('DATABASE_URL', result.stderr)

    def test_debug_true_does_not_require_env(self):
        result = run_settings(
            {'DEBUG': 'True', 'DATABASE_URL': '', 'HOST': '', 'RESEND_API_KEY': '', 'AWS_ACCESS_KEY_ID': ''},
            'import django; django.setup()',
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_railway_healthcheck_host_is_allowed_when_not_debug(self):
        code = (
            'import django, json; django.setup(); from django.conf import settings as s; '
            'print(json.dumps({"hosts": s.ALLOWED_HOSTS, "trusted": s.CSRF_TRUSTED_ORIGINS}))'
        )
        result = run_settings(PROD_ENV, code)
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertIn('healthcheck.railway.app', data['hosts'])
        # El host del healthcheck no es un origen confiable para CSRF.
        self.assertNotIn('https://healthcheck.railway.app', data['trusted'])

    def test_security_settings_when_not_debug(self):
        code = (
            'import django, json; django.setup(); from django.conf import settings as s; '
            'print(json.dumps({"hsts": s.SECURE_HSTS_SECONDS, "ssl": s.SECURE_SSL_REDIRECT, '
            '"sess": s.SESSION_COOKIE_SECURE, "csrf": s.CSRF_COOKIE_SECURE, '
            '"trusted": s.CSRF_TRUSTED_ORIGINS, "exempt": s.SECURE_REDIRECT_EXEMPT, '
            '"age": s.DATABASES["default"]["CONN_MAX_AGE"], '
            '"health": s.DATABASES["default"]["CONN_HEALTH_CHECKS"], '
            '"cache": s.CACHES["default"]["BACKEND"], '
            '"auth": s.REST_FRAMEWORK["DEFAULT_AUTHENTICATION_CLASSES"]}))'
        )
        result = run_settings(PROD_ENV, code)
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertGreater(data['hsts'], 0)
        self.assertLessEqual(data['hsts'], 86400)  # max-age corto al arrancar
        self.assertTrue(data['ssl'] and data['sess'] and data['csrf'])
        self.assertIn('https://api.marky.one', data['trusted'])
        self.assertIn('https://app.marky.one', data['trusted'])
        self.assertTrue(any('healthz' in p for p in data['exempt']))
        self.assertTrue(any('readyz' in p for p in data['exempt']))
        self.assertEqual(data['age'], 60)
        self.assertTrue(data['health'])
        self.assertEqual(data['cache'], 'django.core.cache.backends.db.DatabaseCache')
        self.assertEqual(data['auth'], ['rest_framework_simplejwt.authentication.JWTAuthentication'])

    def test_conn_max_age_and_hsts_configurable(self):
        code = (
            'import django; django.setup(); from django.conf import settings as s; '
            'print(s.DATABASES["default"]["CONN_MAX_AGE"], s.SECURE_HSTS_SECONDS)'
        )
        result = run_settings({**PROD_ENV, 'CONN_MAX_AGE': '5', 'SECURE_HSTS_SECONDS': '120'}, code)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip().splitlines()[-1], '5 120')

    def test_sqlite_keeps_default_conn_max_age(self):
        code = (
            'import django; django.setup(); from django.conf import settings as s; '
            'print(s.DATABASES["default"]["ENGINE"], s.DATABASES["default"].get("CONN_MAX_AGE", 0))'
        )
        result = run_settings({'DEBUG': 'True', 'DATABASE_URL': ''}, code)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip().splitlines()[-1], 'django.db.backends.sqlite3 0')

    def test_cors_regex_is_strict(self):
        import re
        code = (
            'import django; django.setup(); from django.conf import settings as s; '
            'import json; print(json.dumps(s.CORS_ALLOWED_ORIGIN_REGEXES))'
        )
        result = run_settings({**PROD_ENV, 'VERCEL_TEAM_SLUG': 'marky-team'}, code)
        self.assertEqual(result.returncode, 0, result.stderr)
        patterns = json.loads(result.stdout.strip().splitlines()[-1])

        def allowed(origin):
            return any(re.match(p, origin) for p in patterns)

        self.assertTrue(allowed('https://marky-admin.vercel.app'))
        self.assertTrue(allowed('https://marky-admin-git-main-marky-team.vercel.app'))
        self.assertTrue(allowed('https://marky-admin-abc123-marky-team.vercel.app'))
        self.assertFalse(allowed('https://marky-admin-evil.vercel.app'))
        self.assertFalse(allowed('https://marky-admin-abc123-other-team.vercel.app'))
        self.assertFalse(allowed('https://marky-admin.vercel.app.evil.com'))
        self.assertFalse(allowed('https://marky-adminXvercel.app'))


class TestCheckDeploy(SimpleTestCase):
    def test_check_deploy_has_no_unexpected_warnings(self):
        result = run_settings(
            PROD_ENV,
            'import django; django.setup(); '
            'from django.core.management import call_command; '
            'call_command("check", deploy=True, fail_level="CRITICAL")',
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        # W005 (includeSubDomains) y W021 (preload) son decisiones deliberadas con max-age corto.
        unexpected = [
            line for line in result.stderr.splitlines()
            if 'security.W' in line and not any(code in line for code in ('W005', 'W021'))
        ]
        self.assertEqual(unexpected, [], result.stderr)


class TestHealthEndpoints(TestCase):
    def test_healthz_is_public_and_needs_no_db(self):
        with self.assertNumQueries(0):
            response = self.client.get('/healthz')
        self.assertEqual(response.status_code, 200)

    def test_readyz_ok_when_db_reachable(self):
        response = self.client.get('/readyz')
        self.assertEqual(response.status_code, 200)

    def test_readyz_503_when_db_down(self):
        from unittest import mock
        with mock.patch.object(connection, 'cursor', side_effect=Exception('db down')):
            response = self.client.get('/readyz')
        self.assertEqual(response.status_code, 503)

    def test_health_not_throttled(self):
        for _ in range(100):
            self.assertEqual(self.client.get('/healthz').status_code, 200)


class TestDocsGating(TestCase):
    URLS = ['/api/schema/', '/api/schema/swagger/', '/api/schema/redoc/']

    def test_anonymous_blocked_when_not_debug(self):
        for url in self.URLS:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 404)

    def test_non_staff_blocked(self):
        user = get_user_model().objects.create_user('plain', 'plain@x.com', 'TestPass123!')
        self.client.force_login(user)
        for url in self.URLS:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 404)

    def test_staff_allowed(self):
        staff = get_user_model().objects.create_user('staff', 'staff@x.com', 'TestPass123!', is_staff=True)
        self.client.force_login(staff)
        for url in self.URLS:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)

    @override_settings(DEBUG=True)
    def test_anonymous_allowed_in_debug(self):
        for url in self.URLS:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)


class TestThrottleScopesAndIsolation(TestCase):
    def setUp(self):
        cache.clear()

    def tearDown(self):
        cache.clear()

    def test_new_scopes_configured(self):
        from rest_framework.settings import api_settings
        rates = api_settings.DEFAULT_THROTTLE_RATES
        self.assertIn('password_change', rates)
        self.assertIn('validate_name', rates)

    def test_validate_name_throttled_per_client(self):
        a = APIClient(REMOTE_ADDR='10.0.0.1')
        b = APIClient(REMOTE_ADDR='10.0.0.2')
        statuses = [a.get('/api/v1/business/validate-name/', {'business_id': 'x'}).status_code
                    for _ in range(40)]
        self.assertIn(429, statuses)
        self.assertEqual(b.get('/api/v1/business/validate-name/', {'business_id': 'x'}).status_code, 200)

    def test_password_change_throttled_per_client(self):
        a = APIClient(REMOTE_ADDR='10.0.0.1')
        b = APIClient(REMOTE_ADDR='10.0.0.2')
        payload = {'uid': 'x', 'token': 'y', 'new_password': 'z'}
        statuses = [a.post('/api/v1/users/password-change/', payload).status_code for _ in range(12)]
        self.assertIn(429, statuses)
        self.assertNotEqual(b.post('/api/v1/users/password-change/', payload).status_code, 429)

    def test_throttle_uses_shared_cache_backend(self):
        # El contador vive en CACHES['default'] (DatabaseCache por defecto, no LocMem por proceso).
        self.assertNotIn('locmem', settings.CACHES['default']['BACKEND'].lower())
