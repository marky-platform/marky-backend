import json
import uuid
from contextlib import contextmanager
from unittest import mock
from datetime import timedelta

from django.core.cache import cache
from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.test import override_settings
from django.utils import timezone
from rest_framework.throttling import ScopedRateThrottle

from analytics.models import AnalyticsEvent
from analytics.services import is_bot_user_agent

from .base import AnalyticsTestCase


class IngestionTests(AnalyticsTestCase):
    def test_anonymous_ingestion_succeeds(self):
        event = self.ev('catalog_visit', source='qr')
        response = self.post_events('biz-a', [event])
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json(), {'accepted': 1, 'duplicates': 0, 'rejected': 0})
        stored = AnalyticsEvent.objects.get()
        self.assertEqual(str(stored.id), event['event_id'])
        self.assertEqual(stored.business, self.biz_a)
        self.assertEqual(stored.source, 'qr')

    def test_business_lookup_is_case_insensitive(self):
        self.assertEqual(self.post_events('BIZ-A', [self.ev()]).status_code, 202)
        self.assertEqual(AnalyticsEvent.objects.get().business, self.biz_a)

    def test_text_plain_body_is_parsed(self):
        body = json.dumps({'events': [self.ev('catalog_visit')]})
        response = self.client.post(
            self.url('biz-a'), data=body, content_type='text/plain;charset=UTF-8')
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()['accepted'], 1)

    def test_text_plain_invalid_json_is_400(self):
        response = self.client.post(self.url('biz-a'), data='{nope', content_type='text/plain;charset=UTF-8')
        self.assertEqual(response.status_code, 400)

    def test_body_too_large_is_413(self):
        with override_settings(ANALYTICS_MAX_BODY_BYTES=100):
            body = json.dumps({'events': [self.ev()] * 3})
            response = self.client.post(self.url('biz-a'), data=body, content_type='text/plain')
        self.assertEqual(response.status_code, 413)

    @override_settings(CORS_ALLOW_ALL_ORIGINS=False)
    def test_cors_allows_frontend_origin_for_simple_post(self):
        body = json.dumps({'events': [self.ev()]})
        response = self.client.post(
            self.url('biz-a'), data=body, content_type='text/plain;charset=UTF-8',
            HTTP_ORIGIN='https://marky.one')
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response['Access-Control-Allow-Origin'], 'https://marky.one')

    def test_unknown_business_404(self):
        self.assertEqual(self.post_events('nope', [self.ev()]).status_code, 404)
        self.assertEqual(AnalyticsEvent.objects.count(), 0)

    def test_body_business_id_is_ignored(self):
        event = self.ev(business_id=self.biz_b.pk, business='biz-b')
        self.post_events('biz-a', [event])
        self.assertEqual(AnalyticsEvent.objects.get().business, self.biz_a)

    def test_product_from_other_business_rejected(self):
        response = self.post_events('biz-a', [self.ev('product_open', product_id=self.prod_b.pk)])
        self.assertEqual(response.json(), {'accepted': 0, 'duplicates': 0, 'rejected': 1})
        self.assertEqual(AnalyticsEvent.objects.count(), 0)

    def test_inactive_product_rejected(self):
        self.prod_a.is_active = False
        self.prod_a.save()
        response = self.post_events('biz-a', [self.ev('product_view', product_id=self.prod_a.pk)])
        self.assertEqual(response.json()['rejected'], 1)

    def test_product_name_copied_to_metadata(self):
        self.post_events('biz-a', [self.ev('product_open', product_id=self.prod_a.pk)])
        self.assertEqual(AnalyticsEvent.objects.get().metadata, {'product_name': 'Pizza'})

    def test_repeated_event_id_is_duplicate(self):
        event = self.ev('product_open', product_id=self.prod_a.pk)
        self.post_events('biz-a', [event])
        response = self.post_events('biz-a', [event])
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json(), {'accepted': 0, 'duplicates': 1, 'rejected': 0})
        self.assertEqual(AnalyticsEvent.objects.count(), 1)

    def test_same_event_id_in_one_batch_counts_one_duplicate(self):
        event = self.ev()
        response = self.post_events('biz-a', [event, event])
        self.assertEqual(response.json(), {'accepted': 1, 'duplicates': 1, 'rejected': 0})

    def test_two_catalog_visits_same_session_one_visit(self):
        visitor, session = str(uuid.uuid4()), str(uuid.uuid4())
        events = [self.ev('catalog_visit', visitor_id=visitor, session_id=session) for _ in range(2)]
        response = self.post_events('biz-a', events)
        self.assertEqual(response.json(), {'accepted': 1, 'duplicates': 1, 'rejected': 0})
        self.assertEqual(AnalyticsEvent.objects.filter(event_type='catalog_visit').count(), 1)

    def test_five_opens_five_view_attempts_one_view(self):
        visitor, session = str(uuid.uuid4()), str(uuid.uuid4())
        pid = self.prod_a.pk
        events = []
        for _ in range(5):
            events.append(self.ev('product_open', visitor_id=visitor, session_id=session, product_id=pid))
            events.append(self.ev('product_view', visitor_id=visitor, session_id=session, product_id=pid))
        # 10 eventos en dos batches (máx. 20 por batch, pero verificamos mezcla con error parcial)
        response = self.post_events('biz-a', events)
        self.assertEqual(response.json(), {'accepted': 6, 'duplicates': 4, 'rejected': 0})
        self.assertEqual(AnalyticsEvent.objects.filter(event_type='product_open').count(), 5)
        self.assertEqual(AnalyticsEvent.objects.filter(event_type='product_view').count(), 1)

    def test_duplicate_does_not_break_rest_of_batch(self):
        visitor, session = str(uuid.uuid4()), str(uuid.uuid4())
        first = self.ev('catalog_visit', visitor_id=visitor, session_id=session)
        self.post_events('biz-a', [first])
        dup = self.ev('catalog_visit', visitor_id=visitor, session_id=session)
        fresh = self.ev('action_click', action_type='whatsapp')
        response = self.post_events('biz-a', [dup, fresh])
        self.assertEqual(response.json(), {'accepted': 1, 'duplicates': 1, 'rejected': 0})

    def test_new_session_same_visitor_new_visit_and_view(self):
        visitor = str(uuid.uuid4())
        pid = self.prod_a.pk
        for _ in range(2):
            session = str(uuid.uuid4())
            self.post_events('biz-a', [
                self.ev('catalog_visit', visitor_id=visitor, session_id=session),
                self.ev('product_view', visitor_id=visitor, session_id=session, product_id=pid),
            ])
        self.assertEqual(AnalyticsEvent.objects.filter(event_type='catalog_visit').count(), 2)
        self.assertEqual(AnalyticsEvent.objects.filter(event_type='product_view').count(), 2)
        self.assertEqual(AnalyticsEvent.objects.values('visitor_id').distinct().count(), 1)

    def test_same_session_id_in_two_businesses_is_not_a_duplicate(self):
        session = str(uuid.uuid4())
        self.post_events('biz-a', [self.ev(session_id=session)])
        response = self.post_events('biz-b', [self.ev(session_id=session)])
        self.assertEqual(response.json()['accepted'], 1)

    def test_batch_validation(self):
        self.assertEqual(self.post_events('biz-a', []).status_code, 400)
        self.assertEqual(self.client.post(self.url('biz-a'), {}, format='json').status_code, 400)
        self.assertEqual(self.client.post(self.url('biz-a'), [1], format='json').status_code, 400)
        too_many = [self.ev() for _ in range(21)]
        self.assertEqual(self.post_events('biz-a', too_many).status_code, 400)
        self.assertEqual(self.post_events('biz-a', [self.ev() for _ in range(20)]).json()['accepted'], 20)

    def test_invalid_events_are_rejected_individually(self):
        pid = self.prod_a.pk
        bad = [
            self.ev(event_id='not-a-uuid'),
            self.ev(visitor_id='x'),
            self.ev(session_id=None),
            self.ev(type_='bogus') | {'type': 'bogus'},
            self.ev('product_open'),                                  # falta product_id
            self.ev('action_click'),                                  # falta action_type
            self.ev('action_click', action_type='tiktok'),
            self.ev('catalog_visit', source='facebook'),
            self.ev('catalog_visit', occurred_at='ayer'),
            self.ev('product_open', product_id=pid, engaged_ms=100),  # engaged_ms solo en engagement
            self.ev('product_engagement', product_id=pid, engaged_ms=30_001),
            self.ev('product_engagement', product_id=pid, engaged_ms=-1),
            self.ev('product_open', product_id='abc'),
        ]
        good = self.ev('product_engagement', product_id=pid, engaged_ms=15_000)
        response = self.post_events('biz-a', bad + [good])
        self.assertEqual(response.json(), {'accepted': 1, 'duplicates': 0, 'rejected': len(bad)})

    def test_occurred_at_far_from_server_time_is_discarded(self):
        far = (timezone.now() - timedelta(days=30)).isoformat()
        near = (timezone.now() - timedelta(minutes=5)).isoformat()
        e_far, e_near = self.ev(occurred_at=far), self.ev(occurred_at=near)
        self.post_events('biz-a', [e_far, e_near])
        self.assertIsNone(AnalyticsEvent.objects.get(pk=e_far['event_id']).client_occurred_at)
        self.assertIsNotNone(AnalyticsEvent.objects.get(pk=e_near['event_id']).client_occurred_at)

    def test_unknown_properties_ignored(self):
        self.assertEqual(self.post_events('biz-a', [self.ev(ip='1.2.3.4', ua='x', foo={'a': 1})]).status_code, 202)
        self.assertEqual(AnalyticsEvent.objects.get().metadata, {})

    def test_product_id_ignored_on_non_product_event(self):
        self.post_events('biz-a', [self.ev('catalog_visit', product_id=self.prod_b.pk)])
        self.assertIsNone(AnalyticsEvent.objects.get().product_id)

    def test_known_bot_user_agent_not_stored(self):
        response = self.post_events('biz-a', [self.ev()], HTTP_USER_AGENT='Googlebot/2.1 (+http://www.google.com/bot.html)')
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()['accepted'], 0)
        self.assertEqual(AnalyticsEvent.objects.count(), 0)

    def test_bot_detection_function(self):
        for ua in ['Googlebot/2.1', 'curl/8.0', 'python-requests/2.31', 'HeadlessChrome/120',
                   'Mozilla/5.0 (compatible; bingbot/2.0)', 'Mozilla/5.0 (compatible; AhrefsBot/7.0)']:
            self.assertTrue(is_bot_user_agent(ua), ua)
        for ua in ['', None, 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 '
                             '(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1',
                   'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36']:
            self.assertFalse(is_bot_user_agent(ua), ua)

    @override_settings(ANALYTICS_INGESTION_ENABLED=False)
    def test_kill_switch_disables_ingestion(self):
        response = self.post_events('biz-a', [self.ev()])
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()['accepted'], 0)
        self.assertEqual(AnalyticsEvent.objects.count(), 0)

    def test_stores_no_ip_or_user_agent(self):
        self.post_events('biz-a', [self.ev()], HTTP_USER_AGENT='Mozilla/5.0 Chrome', REMOTE_ADDR='9.9.9.9')
        event = AnalyticsEvent.objects.get()
        self.assertNotIn('9.9.9.9', json.dumps(event.metadata))
        self.assertNotIn('Mozilla', json.dumps(event.metadata))
        self.assertFalse({f.name for f in AnalyticsEvent._meta.get_fields()} & {'ip', 'ip_address', 'user_agent'})


class ThrottleTests(AnalyticsTestCase):
    def _throttled_clients(self):
        results = {}
        for client_ip in ('1.1.1.1', '2.2.2.2'):
            codes = []
            for _ in range(3):
                response = self.post_events(
                    'biz-a', [self.ev()], HTTP_X_FORWARDED_FOR=client_ip, REMOTE_ADDR='10.0.0.1')
                codes.append(response.status_code)
            results[client_ip] = codes
        return results

    @contextmanager
    def _throttle_config(self, num_proxies):
        # SimpleRateThrottle.THROTTLE_RATES se lee al importar DRF: hay que parchear el dict,
        # no basta con override_settings. NUM_PROXIES sí se lee en cada request.
        from django.conf import settings
        rf = {**settings.REST_FRAMEWORK, 'NUM_PROXIES': num_proxies}
        with override_settings(REST_FRAMEWORK=rf), \
                mock.patch.dict(ScopedRateThrottle.THROTTLE_RATES, {'analytics': '2/minute'}):
            cache.clear()
            yield

    def test_clients_behind_proxy_throttled_independently(self):
        with self._throttle_config(num_proxies=1):
            results = self._throttled_clients()
        self.assertEqual(results['1.1.1.1'], [202, 202, 429])
        self.assertEqual(results['2.2.2.2'], [202, 202, 429])

    def test_without_proxies_xff_is_ignored(self):
        with self._throttle_config(num_proxies=0):
            results = self._throttled_clients()
        # Todos comparten REMOTE_ADDR: el 2.º cliente ya llega throttled.
        self.assertEqual(results['1.1.1.1'], [202, 202, 429])
        self.assertEqual(results['2.2.2.2'], [429, 429, 429])


class ConstraintTests(AnalyticsTestCase):
    def _create(self, **kw):
        defaults = dict(id=uuid.uuid4(), business=self.biz_a, visitor_id=uuid.uuid4(), session_id=uuid.uuid4())
        defaults.update(kw)
        with transaction.atomic():
            return AnalyticsEvent.objects.create(**defaults)

    def test_db_rejects_bad_action_type_and_engaged_ms(self):
        with self.assertRaises(IntegrityError):
            self._create(event_type='action_click', action_type='tiktok')
        with self.assertRaises(IntegrityError):
            self._create(event_type='action_click', action_type='')
        with self.assertRaises(IntegrityError):
            self._create(event_type='catalog_visit', action_type='whatsapp')
        with self.assertRaises(IntegrityError):
            self._create(event_type='catalog_visit', engaged_ms=5)
        with self.assertRaises(IntegrityError):
            self._create(event_type='product_engagement', product=self.prod_a, engaged_ms=30_001)
        with self.assertRaises(IntegrityError):
            self._create(event_type='bogus')

    def test_db_unique_visit_and_view(self):
        session = uuid.uuid4()
        self._create(event_type='catalog_visit', session_id=session)
        with self.assertRaises(IntegrityError):
            self._create(event_type='catalog_visit', session_id=session)
        self._create(event_type='product_view', product=self.prod_a, session_id=session)
        with self.assertRaises(IntegrityError):
            self._create(event_type='product_view', product=self.prod_a, session_id=session)
        self._create(event_type='product_view', product=self.prod_a2, session_id=session)

    def test_events_are_immutable(self):
        event = self._create(event_type='catalog_visit')
        with self.assertRaises(ValueError):
            event.save()

    def test_deleting_product_keeps_events(self):
        self._create(event_type='product_view', product=self.prod_a, metadata={'product_name': 'Pizza'})
        self.prod_a.delete()
        event = AnalyticsEvent.objects.get()
        self.assertIsNone(event.product_id)
        self.assertEqual(event.metadata['product_name'], 'Pizza')


class RetentionCommandTests(AnalyticsTestCase):
    def test_purges_only_expired_events_in_batches(self):
        old = timezone.now() - timedelta(days=400)
        for _ in range(5):
            self.make_event(self.biz_a, 'catalog_visit', at=old)
        keep = self.make_event(self.biz_a, 'catalog_visit', at=timezone.now() - timedelta(days=10))
        call_command('purge_analytics_events', '--batch-size', '2', verbosity=0)
        self.assertEqual(list(AnalyticsEvent.objects.values_list('pk', flat=True)), [keep.pk])

    @override_settings(ANALYTICS_EVENT_RETENTION_DAYS=5)
    def test_uses_setting_and_dry_run(self):
        self.make_event(self.biz_a, 'catalog_visit', at=timezone.now() - timedelta(days=6))
        call_command('purge_analytics_events', '--dry-run', verbosity=0)
        self.assertEqual(AnalyticsEvent.objects.count(), 1)
        call_command('purge_analytics_events', verbosity=0)
        self.assertEqual(AnalyticsEvent.objects.count(), 0)
