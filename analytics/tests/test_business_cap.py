from django.core.cache import cache
from django.test import override_settings

from analytics.models import AnalyticsEvent

from .base import AnalyticsTestCase


@override_settings(ANALYTICS_BUSINESS_MAX_EVENTS_PER_MINUTE=5)
class BusinessIngestionCapTests(AnalyticsTestCase):
    def test_events_over_cap_are_rejected_with_unchanged_contract(self):
        r1 = self.post_events('biz-a', [self.ev() for _ in range(4)])
        self.assertEqual(r1.json(), {'accepted': 4, 'duplicates': 0, 'rejected': 0})
        r2 = self.post_events('biz-a', [self.ev() for _ in range(3)])
        self.assertEqual(r2.status_code, 202)
        self.assertEqual(r2.json(), {'accepted': 0, 'duplicates': 0, 'rejected': 3})
        self.assertEqual(AnalyticsEvent.objects.filter(business=self.biz_a).count(), 4)

    def test_cap_is_per_business(self):
        self.post_events('biz-a', [self.ev() for _ in range(5)])
        response = self.post_events('biz-b', [self.ev()])
        self.assertEqual(response.json()['accepted'], 1)

    def test_cap_resets_with_the_window(self):
        self.post_events('biz-a', [self.ev() for _ in range(5)])
        cache.clear()
        self.assertEqual(self.post_events('biz-a', [self.ev()]).json()['accepted'], 1)
