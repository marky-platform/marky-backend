import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

from django.core.cache import cache

from analytics.models import AnalyticsEvent
from products.models import Product, ProductCategory
from utils.tests_base import MarkyAPITestCase

ASU = ZoneInfo('America/Asuncion')


def local(year, month, day, hour=12, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=ASU)


class AnalyticsTestCase(MarkyAPITestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.user_a, cls.biz_a = cls.make_user('biz-a', 'a@test.com')
        cls.user_b, cls.biz_b = cls.make_user('biz-b', 'b@test.com')
        cls.prod_a = cls.make_product(cls.biz_a, 'Pizza')
        cls.prod_a2 = cls.make_product(cls.biz_a, 'Sushi')
        cls.prod_b = cls.make_product(cls.biz_b, 'Taco')

    @classmethod
    def make_product(cls, business, name, **kwargs):
        category = ProductCategory.objects.create(business=business, name='Cat', icon='x')
        return Product.objects.create(
            business=business, category=category, name=name, description='d', price=10, **kwargs)

    def setUp(self):
        # El throttle usa la cache local: aislar entre tests.
        cache.clear()

    @staticmethod
    def url(business_id):
        return f'/api/v1/public/business/{business_id}/analytics/events/'

    @staticmethod
    def ev(type_='catalog_visit', **kw):
        data = {
            'event_id': str(uuid.uuid4()),
            'visitor_id': str(uuid.uuid4()),
            'session_id': str(uuid.uuid4()),
            'type': type_,
        }
        data.update(kw)
        return data

    def post_events(self, business_id, events, **extra):
        return self.client.post(self.url(business_id), {'events': events}, format='json', **extra)

    def make_event(self, business, event_type, at=None, **kw):
        """Crea un evento directo en la BD, fijando received_at (auto_now_add)."""
        kw.setdefault('visitor_id', uuid.uuid4())
        kw.setdefault('session_id', uuid.uuid4())
        if kw.get('product') is not None:
            kw.setdefault('metadata', {'product_name': kw['product'].name})
        event = AnalyticsEvent.objects.create(id=uuid.uuid4(), business=business, event_type=event_type, **kw)
        if at is not None:
            AnalyticsEvent.objects.filter(pk=event.pk).update(received_at=at)
        return event
