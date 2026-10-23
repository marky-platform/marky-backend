from django.urls import path

from .views import AnalyticsPulseView

urlpatterns = [
    path('pulse/', AnalyticsPulseView.as_view(), name='analytics-pulse'),
]
