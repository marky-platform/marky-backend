import threading
import uuid
from datetime import datetime, timedelta, timezone as dt_timezone
from unittest import mock, skipUnless

from django.db import connection, connections
from django.test import TransactionTestCase

from analytics.models import AnalyticsEvent, CatalogVisitWindow
from analytics.services import CATALOG_VISIT_WINDOW, ingest_events

from .base import AnalyticsTestCase

T0 = datetime(2026, 10, 23, 12, 0, tzinfo=dt_timezone.utc)


class VisitWindowTests(AnalyticsTestCase):
    """Deduplicación de 4 horas por (negocio, visitante), decidida por el backend."""

    def visit(self, business_id='biz-a', at=T0, visitor=None, session=None, **kw):
        event = self.ev(
            'catalog_visit',
            visitor_id=str(visitor or self.visitor),
            session_id=str(session or uuid.uuid4()),
            **kw,
        )
        with mock.patch('django.utils.timezone.now', return_value=at):
            return self.post_events(business_id, [event]).json()

    def setUp(self):
        super().setUp()
        self.visitor = uuid.uuid4()

    def visits(self, business=None):
        return AnalyticsEvent.objects.filter(
            event_type='catalog_visit', **({'business': business} if business else {})).count()

    def test_first_access_creates_one_visit(self):
        self.assertEqual(self.visit()['accepted'], 1)
        self.assertEqual(self.visits(), 1)

    def test_reload_within_window_is_not_counted(self):
        self.visit()
        result = self.visit(at=T0 + timedelta(minutes=1))
        self.assertEqual((result['accepted'], result['duplicates']), (0, 1))
        self.assertEqual(self.visits(), 1)

    def test_access_just_before_boundary_is_deduplicated(self):
        self.visit()
        self.visit(at=T0 + CATALOG_VISIT_WINDOW - timedelta(seconds=1))
        self.assertEqual(self.visits(), 1)

    def test_access_at_boundary_creates_new_visit(self):
        self.visit()
        self.assertEqual(self.visit(at=T0 + CATALOG_VISIT_WINDOW)['accepted'], 1)
        self.assertEqual(self.visits(), 2)

    def test_ignored_hits_do_not_extend_the_window(self):
        self.visit()
        self.visit(at=T0 + timedelta(hours=3))
        # 4 h después de la PRIMERA visita (no de la última ignorada) ya cuenta de nuevo.
        self.assertEqual(self.visit(at=T0 + timedelta(hours=4))['accepted'], 1)
        self.assertEqual(self.visits(), 2)

    def test_new_session_inside_window_is_still_deduplicated(self):
        self.visit()
        self.visit(at=T0 + timedelta(hours=1), session=uuid.uuid4())
        self.assertEqual(self.visits(), 1)

    def test_different_visitors_count_separately(self):
        self.visit()
        self.visit(visitor=uuid.uuid4(), at=T0 + timedelta(minutes=1))
        self.assertEqual(self.visits(), 2)

    def test_same_visitor_in_different_businesses_counts_separately(self):
        self.visit('biz-a')
        self.visit('biz-b', at=T0 + timedelta(minutes=1))
        self.assertEqual(self.visits(self.biz_a), 1)
        self.assertEqual(self.visits(self.biz_b), 1)

    def test_client_clock_does_not_decide_the_window(self):
        self.visit()
        # `occurred_at` enviado por el cliente no mueve la ventana.
        result = self.visit(at=T0 + timedelta(minutes=5), occurred_at=(T0 + timedelta(hours=9)).isoformat())
        self.assertEqual(result['accepted'], 0)

    def test_product_visit_shares_the_catalog_window(self):
        pid = self.prod_a.pk
        # Entrada por un producto: una visita + una vista de producto.
        event_session = uuid.uuid4()
        batch = [
            self.ev('catalog_visit', visitor_id=str(self.visitor), session_id=str(event_session)),
            self.ev('product_view', visitor_id=str(self.visitor), session_id=str(event_session), product_id=pid),
        ]
        with mock.patch('django.utils.timezone.now', return_value=T0):
            self.assertEqual(self.post_events('biz-a', batch).json()['accepted'], 2)
        # Luego entra por el catálogo dentro de las 4 h: no hay otra visita.
        self.visit(at=T0 + timedelta(hours=1))
        self.assertEqual(self.visits(), 1)
        self.assertEqual(AnalyticsEvent.objects.filter(event_type='product_view').count(), 1)

    def test_product_events_inside_window_keep_their_own_rules(self):
        self.visit()
        pid = self.prod_a.pk
        other_session = str(uuid.uuid4())
        batch = [
            self.ev('catalog_visit', visitor_id=str(self.visitor), session_id=other_session),
            self.ev('product_open', visitor_id=str(self.visitor), session_id=other_session, product_id=pid),
            self.ev('product_view', visitor_id=str(self.visitor), session_id=other_session, product_id=pid),
            self.ev('action_click', visitor_id=str(self.visitor), session_id=other_session, action_type='whatsapp'),
        ]
        with mock.patch('django.utils.timezone.now', return_value=T0 + timedelta(hours=1)):
            result = self.post_events('biz-a', batch).json()
        self.assertEqual(result, {'accepted': 3, 'duplicates': 1, 'rejected': 0})

    def test_window_row_tracks_last_counted_visit(self):
        self.visit()
        self.visit(at=T0 + timedelta(hours=1))
        window = CatalogVisitWindow.objects.get()
        self.assertEqual(window.last_counted_at, T0)
        self.visit(at=T0 + timedelta(hours=5))
        window.refresh_from_db()
        self.assertEqual(window.last_counted_at, T0 + timedelta(hours=5))

    def test_visit_batch_with_repeated_visit_counts_once(self):
        events = [self.ev('catalog_visit', visitor_id=str(self.visitor)) for _ in range(3)]
        with mock.patch('django.utils.timezone.now', return_value=T0):
            result = self.post_events('biz-a', events).json()
        self.assertEqual(result, {'accepted': 1, 'duplicates': 2, 'rejected': 0})


@skipUnless(connection.vendor == 'postgresql', 'La concurrencia real requiere Postgres (select_for_update).')
class ConcurrentVisitTests(TransactionTestCase):
    def setUp(self):
        from django.contrib.auth import get_user_model
        from business.models import BusinessProfile, Currency

        currency = Currency.objects.create(name='US Dollar', code='USD')
        user = get_user_model().objects.create_user(username='conc-biz', email='c@test.com', password='x')
        self.business = BusinessProfile.objects.create(user=user, business_id='conc-biz', primary_currency=currency)

    def test_concurrent_duplicate_requests_create_one_visit(self):
        visitor = uuid.uuid4()
        workers = 8
        barrier = threading.Barrier(workers)
        results, errors = [], []

        def run():
            try:
                event = {
                    'event_id': uuid.uuid4(), 'visitor_id': visitor, 'session_id': uuid.uuid4(),
                    'type': 'catalog_visit',
                }
                barrier.wait()
                results.append(ingest_events(self.business, [event]))
            except Exception as exc:  # pragma: no cover - se reporta abajo
                errors.append(exc)
            finally:
                connections.close_all()

        threads = [threading.Thread(target=run) for _ in range(workers)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(errors, [])
        self.assertEqual(sum(r[0] for r in results), 1)
        self.assertEqual(AnalyticsEvent.objects.filter(event_type='catalog_visit').count(), 1)
        self.assertEqual(CatalogVisitWindow.objects.count(), 1)
