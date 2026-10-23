from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from business.models import BusinessProfile
from utils.permissions import IsBusinessOrSuperAdmin

from .services import PULSE_DAYS, build_pulse


@extend_schema(
    tags=['Analytics'],
    parameters=[OpenApiParameter('days', int, description='Solo se admite 7.')],
)
class AnalyticsPulseView(APIView):
    """Métricas del widget Pulse del negocio autenticado. `GET /api/v1/business/analytics/pulse/?days=7`."""
    permission_classes = [IsBusinessOrSuperAdmin]

    def get(self, request, *args, **kwargs):
        # El negocio sale siempre del usuario autenticado, nunca de la query.
        try:
            business = request.user.business_profile
        except BusinessProfile.DoesNotExist:
            return Response(
                {"error": "No business profile found for this user."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        days = request.query_params.get('days', str(PULSE_DAYS))
        if days != str(PULSE_DAYS):
            return Response({'days': f'Solo se admite days={PULSE_DAYS}.'}, status=status.HTTP_400_BAD_REQUEST)

        return Response(build_pulse(business, days=PULSE_DAYS))
