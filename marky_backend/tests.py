import os
import subprocess
import sys

from django.conf import settings
from django.test import SimpleTestCase


class TestProductionLogging(SimpleTestCase):
    def test_django_request_errors_reach_stderr(self):
        environment = os.environ.copy()
        environment['DEBUG'] = 'False'
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
