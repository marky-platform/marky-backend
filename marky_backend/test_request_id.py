import logging

from django.test import SimpleTestCase, TestCase

from marky_backend.request_id import RequestIdFilter, get_request_id


class RequestIdMiddlewareTests(TestCase):
    def test_generates_id_when_missing(self):
        response = self.client.get('/healthz')
        rid = response['X-Request-ID']
        self.assertRegex(rid, r'^[0-9a-f]{32}$')

    def test_echoes_valid_incoming_id(self):
        response = self.client.get('/healthz', HTTP_X_REQUEST_ID='abc-123.DEF_9')
        self.assertEqual(response['X-Request-ID'], 'abc-123.DEF_9')

    def test_replaces_invalid_incoming_id(self):
        response = self.client.get('/healthz', HTTP_X_REQUEST_ID='bad id\r\nX: y' + 'a' * 100)
        self.assertRegex(response['X-Request-ID'], r'^[0-9a-f]{32}$')

    def test_id_is_available_to_logging_during_request_and_cleared_after(self):
        seen = {}

        class Capture(logging.Handler):
            def emit(self, record):
                seen['rid'] = getattr(record, 'request_id', None)

        handler = Capture()
        handler.addFilter(RequestIdFilter())
        logger = logging.getLogger('django.request')
        logger.addHandler(handler)
        try:
            # 404 de Django registra un warning en django.request dentro del request.
            response = self.client.get('/no-existe-xyz/', HTTP_X_REQUEST_ID='trace-1')
        finally:
            logger.removeHandler(handler)
        self.assertEqual(response['X-Request-ID'], 'trace-1')
        self.assertEqual(seen.get('rid'), 'trace-1')
        self.assertEqual(get_request_id(), '-')


class RequestIdFilterTests(SimpleTestCase):
    def test_filter_defaults_to_dash_outside_request(self):
        record = logging.LogRecord('x', logging.INFO, __file__, 1, 'm', None, None)
        self.assertTrue(RequestIdFilter().filter(record))
        self.assertEqual(record.request_id, '-')

    def test_logging_config_uses_request_id(self):
        from django.conf import settings
        handler = settings.LOGGING['handlers']['console']
        self.assertIn('request_id', handler['filters'])
        self.assertIn('request_id', settings.LOGGING['formatters'][handler['formatter']]['format'])
