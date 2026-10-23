from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from business.models import BusinessProfile
from utils.permissions import IsBusinessOrSuperAdmin

from .services import PRODUCT_SORTS, PULSE_ALLOWED_DAYS, PULSE_DAYS, build_pulse


@extend_schema(
    tags=['Analytics'],
    parameters=[
        OpenApiParameter('days', int, description='Período en días: 7 o 30.'),
        OpenApiParameter('sort', str, description='Orden de productos: views (default) o time.'),
    ],
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
        allowed = {str(d): d for d in PULSE_ALLOWED_DAYS}
        if days not in allowed:
            return Response(
                {'days': f'Valores admitidos: {", ".join(allowed)}.'}, status=status.HTTP_400_BAD_REQUEST,
            )
        sort = request.query_params.get('sort', 'views')
        if sort not in PRODUCT_SORTS:
            return Response(
                {'sort': f'Valores admitidos: {", ".join(PRODUCT_SORTS)}.'}, status=status.HTTP_400_BAD_REQUEST,
            )

        data = build_pulse(business, days=allowed[days], product_sort=sort)
        for product in data['top_products']:
            if product['image']:
                product['image'] = request.build_absolute_uri(product['image'])
        return Response(data)
