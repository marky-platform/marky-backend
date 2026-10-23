from datetime import timedelta
import uuid

from django.utils import timezone

from analytics.services import build_pulse

from .base import AnalyticsTestCase, local

URL = '/api/v1/business/analytics/pulse/?days=7'


class PulseTests(AnalyticsTestCase):
    def get(self, user, url=URL):
        return self.auth_client(user).get(url)

    def test_requires_authentication(self):
        self.assertEqual(self.client.get(URL).status_code, 401)

    def test_user_without_business_profile_gets_controlled_400(self):
        user, _ = self.make_user('noprof', 'np@test.com', with_profile=False)
        response = self.get(user)
        self.assertEqual(response.status_code, 400)
        self.assertIn('error', response.json())

    def test_user_outside_business_group_forbidden(self):
        from django.contrib.auth import get_user_model
        user = get_user_model().objects.create_user('x', 'x@test.com', 'pw')
        self.assertEqual(self.get(user).status_code, 403)

    def test_days_must_be_7_or_30(self):
        for bad in ('14', 'abc'):
            self.assertEqual(self.get(self.user_a, f'/api/v1/business/analytics/pulse/?days={bad}').status_code, 400)
        self.assertEqual(self.get(self.user_a, '/api/v1/business/analytics/pulse/').status_code, 200)
        data = self.get(self.user_a, '/api/v1/business/analytics/pulse/?days=30').json()
        self.assertEqual(data['days'], 30)
        self.assertEqual(len(data['history']), 30)

    def test_sort_must_be_views_or_time(self):
        self.assertEqual(self.get(self.user_a, f'{URL}&sort=name').status_code, 400)
        self.assertEqual(self.get(self.user_a, f'{URL}&sort=time').status_code, 200)

    def test_period_and_previous_totals(self):
        now = local(2026, 10, 23, 13)
        # Ventana actual: 17–23/10. Anterior: 10–16/10.
        self.make_event(self.biz_a, 'catalog_visit', at=local(2026, 10, 20))
        self.make_event(self.biz_a, 'catalog_visit', at=local(2026, 10, 16, 23, 59))
        self.make_event(self.biz_a, 'catalog_visit', at=local(2026, 10, 10, 0, 0))
        self.make_event(self.biz_a, 'catalog_visit', at=local(2026, 10, 9, 23, 59))  # fuera de ambas
        self.make_event(self.biz_a, 'action_click', at=local(2026, 10, 12), action_type='whatsapp')
        self.make_event(self.biz_a, 'action_click', at=local(2026, 10, 21), action_type='whatsapp')
        pulse = build_pulse(self.biz_a, now=now)
        self.assertEqual(pulse['days'], 7)
        self.assertEqual(pulse['period'], {'start': '2026-10-17', 'end': '2026-10-23'})
        self.assertEqual(pulse['previous'], {'visits': 2, 'actions': 1})

    def test_empty_response_shape_and_zero_history(self):
        data = self.get(self.user_a).json()
        self.assertEqual(set(data), {
            'generated_at', 'timezone', 'days', 'period', 'previous',
            'today', 'history', 'sources', 'actions', 'top_products',
        })
        self.assertEqual(data['previous'], {'visits': 0, 'actions': 0})
        self.assertEqual(data['timezone'], 'America/Asuncion')
        self.assertEqual(data['today'], {'visits': 0, 'unique_visitors': 0, 'last_visit_at': None})
        self.assertEqual(len(data['history']), 7)
        self.assertTrue(all(h['visits'] == 0 for h in data['history']))
        self.assertEqual(data['sources'], {'public_link': 0, 'qr': 0, 'direct': 0, 'other': 0})
        self.assertEqual(data['actions'], {'location': 0, 'whatsapp': 0, 'instagram': 0, 'other': 0})
        self.assertEqual(data['top_products'], [])
        self.assertTrue(data['generated_at'].endswith('Z'))

    def test_tenant_isolation(self):
        self.make_event(self.biz_b, 'catalog_visit', source='qr')
        self.make_event(self.biz_b, 'action_click', action_type='whatsapp')
        self.make_event(self.biz_b, 'product_view', product=self.prod_b)
        data = self.get(self.user_a).json()
        self.assertEqual(data['today']['visits'], 0)
        self.assertEqual(data['sources']['qr'], 0)
        self.assertEqual(data['actions']['whatsapp'], 0)
        self.assertEqual(data['top_products'], [])
        # y B ve lo suyo
        data_b = self.get(self.user_b).json()
        self.assertEqual(data_b['today']['visits'], 1)
        self.assertEqual(data_b['top_products'][0]['name'], 'Taco')

    def test_business_cannot_be_chosen_via_query(self):
        self.make_event(self.biz_b, 'catalog_visit')
        data = self.get(self.user_a, URL + f'&business={self.biz_b.pk}&business_id=biz-b').json()
        self.assertEqual(data['today']['visits'], 0)

    def test_today_visits_unique_visitors_and_last_visit(self):
        now = local(2026, 10, 23, 13, 0)
        v1, v2 = uuid.uuid4(), uuid.uuid4()
        self.make_event(self.biz_a, 'catalog_visit', at=local(2026, 10, 23, 8), visitor_id=v1)
        self.make_event(self.biz_a, 'catalog_visit', at=local(2026, 10, 23, 12, 30), visitor_id=v1)  # otra sesión
        self.make_event(self.biz_a, 'catalog_visit', at=local(2026, 10, 23, 12, 45), visitor_id=v2)
        self.make_event(self.biz_a, 'catalog_visit', at=local(2026, 10, 22, 23, 0), visitor_id=v2)  # ayer
        self.make_event(self.biz_a, 'product_open', at=local(2026, 10, 23, 9), product=self.prod_a)
        pulse = build_pulse(self.biz_a, now=now)
        self.assertEqual(pulse['today']['visits'], 3)
        self.assertEqual(pulse['today']['unique_visitors'], 2)
        self.assertEqual(pulse['today']['last_visit_at'], '2026-10-23T15:45:00Z')  # 12:45 -03

    def test_last_visit_not_limited_to_today(self):
        now = local(2026, 10, 23, 13, 0)
        self.make_event(self.biz_a, 'catalog_visit', at=local(2026, 9, 1, 10))
        pulse = build_pulse(self.biz_a, now=now)
        self.assertEqual(pulse['today']['visits'], 0)
        self.assertEqual(pulse['today']['last_visit_at'], '2026-09-01T13:00:00Z')

    def test_history_zero_filled_and_midnight_bucketing(self):
        now = local(2026, 10, 23, 13, 0)
        # 23:59 y 00:01 locales caen en días distintos, aunque en UTC ambos son "día siguiente" (02:59 / 03:01)
        self.make_event(self.biz_a, 'catalog_visit', at=local(2026, 10, 21, 23, 59))
        self.make_event(self.biz_a, 'catalog_visit', at=local(2026, 10, 22, 0, 1))
        self.make_event(self.biz_a, 'catalog_visit', at=local(2026, 10, 17, 0, 0))   # primer día de la ventana
        self.make_event(self.biz_a, 'catalog_visit', at=local(2026, 10, 16, 23, 59))  # fuera de la ventana
        history = build_pulse(self.biz_a, now=now)['history']
        self.assertEqual([h['date'] for h in history], [
            '2026-10-17', '2026-10-18', '2026-10-19', '2026-10-20', '2026-10-21', '2026-10-22', '2026-10-23'])
        self.assertEqual([h['visits'] for h in history], [1, 0, 0, 0, 1, 1, 0])

    def test_today_boundary_uses_local_midnight(self):
        # 22:30 UTC-3 del 22/10 = 01:30 UTC del 23/10: sigue siendo "ayer" para el negocio
        now = local(2026, 10, 23, 0, 30)
        self.make_event(self.biz_a, 'catalog_visit', at=local(2026, 10, 22, 22, 30))
        self.make_event(self.biz_a, 'catalog_visit', at=local(2026, 10, 23, 0, 10))
        pulse = build_pulse(self.biz_a, now=now)
        self.assertEqual(pulse['today']['visits'], 1)
        self.assertEqual(pulse['history'][-1], {'date': '2026-10-23', 'visits': 1})
        self.assertEqual(pulse['history'][-2], {'date': '2026-10-22', 'visits': 1})

    def test_history_counts_visits_not_all_events(self):
        now = local(2026, 10, 23, 13)
        self.make_event(self.biz_a, 'catalog_visit', at=local(2026, 10, 23, 9))
        for _ in range(5):
            self.make_event(self.biz_a, 'product_open', at=local(2026, 10, 23, 9), product=self.prod_a)
        self.assertEqual(build_pulse(self.biz_a, now=now)['history'][-1]['visits'], 1)

    def test_sources_and_actions(self):
        now = local(2026, 10, 23, 13)
        at = local(2026, 10, 22, 10)
        for source in ['public_link', 'public_link', 'qr', 'direct', 'other', '']:
            self.make_event(self.biz_a, 'catalog_visit', at=at, source=source)
        for action in ['location', 'whatsapp', 'whatsapp', 'instagram', 'other']:
            self.make_event(self.biz_a, 'action_click', at=at, action_type=action)
        self.make_event(self.biz_a, 'action_click', at=local(2026, 10, 1), action_type='location')  # fuera
        pulse = build_pulse(self.biz_a, now=now)
        self.assertEqual(pulse['sources'], {'public_link': 2, 'qr': 1, 'direct': 2, 'other': 1})
        self.assertEqual(pulse['actions'], {'location': 1, 'whatsapp': 2, 'instagram': 1, 'other': 1})

    def test_top_products_ranking_and_average(self):
        now = local(2026, 10, 23, 13)
        at = local(2026, 10, 23, 10)
        pizza, sushi = self.prod_a, self.prod_a2
        # Pizza: 3 vistas, 5 opens, engagement 10s+20s+27s = 57s -> 19s
        for _ in range(3):
            self.make_event(self.biz_a, 'product_view', at=at, product=pizza)
        for _ in range(5):
            self.make_event(self.biz_a, 'product_open', at=at, product=pizza)
        for ms in (10_000, 20_000, 27_000):
            self.make_event(self.biz_a, 'product_engagement', at=at, product=pizza, engaged_ms=ms)
        # Sushi: 1 vista, 12 opens (más opens no suben el ranking), 2500ms -> 3s (half-up)
        self.make_event(self.biz_a, 'product_view', at=at, product=sushi)
        for _ in range(12):
            self.make_event(self.biz_a, 'product_open', at=at, product=sushi)
        self.make_event(self.biz_a, 'product_engagement', at=at, product=sushi, engaged_ms=2_500)
        top = build_pulse(self.biz_a, now=now)['top_products']
        self.assertEqual(top, [
            {'product_id': pizza.pk, 'name': 'Pizza', 'image': None, 'views': 3, 'average_engaged_seconds': 19},
            {'product_id': sushi.pk, 'name': 'Sushi', 'image': None, 'views': 1, 'average_engaged_seconds': 3},
        ])
        by_time = build_pulse(self.biz_a, now=now, product_sort='time')['top_products']
        self.assertEqual([p['name'] for p in by_time], ['Pizza', 'Sushi'])
        # Con más tiempo medio, Sushi pasa primero aunque tenga menos vistas.
        self.make_event(self.biz_a, 'product_engagement', at=at, product=sushi, engaged_ms=30_000)
        by_time = build_pulse(self.biz_a, now=now, product_sort='time')['top_products']
        self.assertEqual([p['name'] for p in by_time], ['Sushi', 'Pizza'])
        self.assertEqual([p['name'] for p in build_pulse(self.biz_a, now=now)['top_products']], ['Pizza', 'Sushi'])

    def test_top_products_include_first_image_as_absolute_url(self):
        import tempfile
        from django.core.files.uploadedfile import SimpleUploadedFile
        from django.test import override_settings
        with override_settings(MEDIA_ROOT=tempfile.mkdtemp()):
            self._assert_first_image(SimpleUploadedFile)

    def _assert_first_image(self, SimpleUploadedFile):
        from products.models import ProductMedia
        ProductMedia.objects.create(product=self.prod_a, order=2, file=SimpleUploadedFile('b.jpg', b'x'))
        first = ProductMedia.objects.create(product=self.prod_a, order=1, file=SimpleUploadedFile('a.jpg', b'x'))
        self.make_event(self.biz_a, 'product_view', product=self.prod_a)
        image = self.get(self.user_a).json()['top_products'][0]['image']
        self.assertTrue(image.startswith('http://testserver/'), image)
        self.assertTrue(image.endswith(first.file.url), image)

    def test_top_products_limited_to_five(self):
        now = local(2026, 10, 23, 13)
        for i in range(7):
            product = self.make_product(self.biz_a, f'P{i}')
            self.make_event(self.biz_a, 'product_view', at=local(2026, 10, 23, 10), product=product)
        self.assertEqual(len(build_pulse(self.biz_a, now=now)['top_products']), 5)

    def test_deleted_product_uses_metadata_name(self):
        now = local(2026, 10, 23, 13)
        at = local(2026, 10, 23, 10)
        self.make_event(self.biz_a, 'product_view', at=at, product=self.prod_a)
        self.make_event(self.biz_a, 'product_engagement', at=at, product=self.prod_a, engaged_ms=4000)
        self.prod_a.delete()
        top = build_pulse(self.biz_a, now=now)['top_products']
        self.assertEqual(top, [{'product_id': None, 'name': 'Pizza', 'image': None, 'views': 1, 'average_engaged_seconds': 4}])

    def test_deleted_product_without_metadata_name(self):
        now = local(2026, 10, 23, 13)
        event = self.make_event(self.biz_a, 'product_view', at=local(2026, 10, 23, 10), product=self.prod_a,
                                metadata={})
        event.product = None
        type(event).objects.filter(pk=event.pk).update(product=None)
        top = build_pulse(self.biz_a, now=now)['top_products']
        self.assertEqual(top[0]['name'], 'Producto eliminado')
        self.assertIsNone(top[0]['product_id'])

    def test_renamed_product_is_one_row_with_current_name(self):
        now = local(2026, 10, 23, 13)
        at = local(2026, 10, 23, 10)
        self.make_event(self.biz_a, 'product_view', at=at, product=self.prod_a, metadata={'product_name': 'Viejo'})
        self.make_event(self.biz_a, 'product_view', at=at, product=self.prod_a, metadata={'product_name': 'Pizza'})
        top = build_pulse(self.biz_a, now=now)['top_products']
        self.assertEqual(len(top), 1)
        self.assertEqual((top[0]['views'], top[0]['name']), (2, 'Pizza'))

    def test_query_count_is_constant(self):
        at = timezone.now() - timedelta(minutes=5)

        for i in range(3):
            product = self.make_product(self.biz_a, f'N{i}')
            self.make_event(self.biz_a, 'product_view', at=at, product=product)
            self.make_event(self.biz_a, 'catalog_visit', at=at)
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        client = self.auth_client(self.user_a)
        with CaptureQueriesContext(connection) as small:
            client.get(URL)
        for i in range(10):
            product = self.make_product(self.biz_a, f'M{i}')
            self.make_event(self.biz_a, 'product_view', at=at, product=product)
        with CaptureQueriesContext(connection) as big:
            client.get(URL)
        self.assertEqual(len(small), len(big))
