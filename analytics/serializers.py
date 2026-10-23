from datetime import timedelta

from django.utils import timezone
from rest_framework import serializers

from .models import AnalyticsEvent

EventType = AnalyticsEvent.EventType

# Diferencia máxima tolerada entre `occurred_at` del cliente y la hora del servidor.
# Fuera de ese rango se descarta el valor (queda en NULL); `received_at` manda para reportes.
MAX_CLIENT_CLOCK_SKEW = timedelta(hours=24)


class AnalyticsEventInputSerializer(serializers.Serializer):
    """Valida UN evento del batch. Las propiedades desconocidas se ignoran."""

    event_id = serializers.UUIDField()
    visitor_id = serializers.UUIDField()
    session_id = serializers.UUIDField()
    type = serializers.ChoiceField(choices=EventType.choices)
    product_id = serializers.IntegerField(required=False, allow_null=True, min_value=1, max_value=2**31 - 1)
    source = serializers.ChoiceField(
        choices=AnalyticsEvent.Source.choices, required=False, allow_blank=True, default='',
    )
    occurred_at = serializers.DateTimeField(required=False, allow_null=True, default=None)
    engaged_ms = serializers.IntegerField(
        required=False, min_value=0, max_value=AnalyticsEvent.MAX_ENGAGED_MS, default=0,
    )
    action_type = serializers.ChoiceField(
        choices=AnalyticsEvent.ActionType.choices, required=False, allow_blank=True, default='',
    )

    def validate(self, attrs):
        event_type = attrs['type']
        product_id = attrs.get('product_id')
        if event_type in AnalyticsEvent.PRODUCT_EVENT_TYPES:
            if product_id is None:
                raise serializers.ValidationError({'product_id': 'Obligatorio para este tipo de evento.'})
        else:
            # Los eventos sin producto no deben arrastrar uno.
            attrs['product_id'] = None
        if event_type == EventType.ACTION_CLICK:
            if not attrs.get('action_type'):
                raise serializers.ValidationError({'action_type': 'Obligatorio para action_click.'})
        else:
            attrs['action_type'] = ''
        if attrs.get('engaged_ms') and event_type != EventType.PRODUCT_ENGAGEMENT:
            raise serializers.ValidationError({'engaged_ms': 'Solo permitido en product_engagement.'})
        occurred_at = attrs.get('occurred_at')
        if occurred_at is not None and abs(timezone.now() - occurred_at) > MAX_CLIENT_CLOCK_SKEW:
            attrs['occurred_at'] = None
        return attrs


class PublicEventBatchSerializer(serializers.Serializer):
    """Respuesta 202 de la ingesta (solo contadores)."""

    accepted = serializers.IntegerField()
    duplicates = serializers.IntegerField()
    rejected = serializers.IntegerField()
