from django.urls import path

from .public_views import PublicAnalyticsEventsView

urlpatterns = [
    path('events/', PublicAnalyticsEventsView.as_view(), name='public-analytics-events'),
]
