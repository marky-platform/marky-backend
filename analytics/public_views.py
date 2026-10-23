from django.conf import settings
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from business.public_views import get_business_or_404

from .parsers import LimitedJSONParser, PlainTextJSONParser
from .serializers import AnalyticsEventInputSerializer, PublicEventBatchSerializer
from .services import ingest_events, is_bot_user_agent


@extend_schema(
    tags=['Analytics'],
    request=AnalyticsEventInputSerializer(many=True),
    responses={202: PublicEventBatchSerializer},
)
class PublicAnalyticsEventsView(APIView):
    """Ingesta anónima de eventos: `POST /api/v1/public/business/<business_id>/analytics/events/`.

    Body `{"events": [...]}` (máx. 20) como JSON o `text/plain` (CORS-simple, sin preflight).
    """
    permission_classes = [AllowAny]
    # Sin autenticación: un Bearer vencido no debe dar 401 ni aplicar CSRF de sesión.
    authentication_classes = []
    parser_classes = [LimitedJSONParser, PlainTextJSONParser]
    throttle_scope = 'analytics'

    def post(self, request, business_id, *args, **kwargs):
        business = get_business_or_404(business_id)

        body = request.data
        events = body.get('events') if isinstance(body, dict) else None
        if not isinstance(events, list) or not events:
            return Response({'events': 'Se espera una lista no vacía de eventos.'},
                            status=status.HTTP_400_BAD_REQUEST)
        if len(events) > settings.ANALYTICS_MAX_BATCH_SIZE:
            return Response(
                {'events': f'Máximo {settings.ANALYTICS_MAX_BATCH_SIZE} eventos por solicitud.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Interruptor global y bots: respuesta 202 sin guardar (el cliente no debe reintentar).
        if not settings.ANALYTICS_INGESTION_ENABLED or is_bot_user_agent(request.META.get('HTTP_USER_AGENT', '')):
            return Response({'accepted': 0, 'duplicates': 0, 'rejected': len(events)},
                            status=status.HTTP_202_ACCEPTED)

        valid, rejected = [], 0
        for raw in events:
            serializer = AnalyticsEventInputSerializer(data=raw if isinstance(raw, dict) else {})
            if serializer.is_valid():
                valid.append(serializer.validated_data)
            else:
                rejected += 1

        accepted, duplicates, product_rejected = ingest_events(business, valid)
        return Response(
            {'accepted': accepted, 'duplicates': duplicates, 'rejected': rejected + product_rejected},
            status=status.HTTP_202_ACCEPTED,
        )
