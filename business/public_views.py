from drf_spectacular.utils import extend_schema
from rest_framework.exceptions import NotFound
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import BusinessProfile
from .public_serializers import PublicBusinessProfileSerializer


def get_business_or_404(business_id, *, full=False):
    """Look up a BusinessProfile by its public slug for an anonymous caller.

    Case-insensitive lookup mirrors validation (business_id uniqueness is
    enforced case-insensitively), though the DB constraint itself is
    case-sensitive — `MultipleObjectsReturned` is treated as a 404 rather
    than leaking a 500, since it can only mean two profiles differing only
    in case (validation was bypassed or predates the reserved-word/case
    checks). 404s on plain miss too, rather than leaking existence via a
    different status code.

    `full=True` prefetches the relations only the profile view renders
    (social links, categories, branch attributes) — the catalog/categories/
    product-detail views only need `business` itself and skip that cost.
    """
    queryset = BusinessProfile.objects.all()
    if full:
        queryset = queryset.select_related('user').prefetch_related(
            'social_links', 'categories', 'branches__attributes'
        )
    try:
        return queryset.get(business_id__iexact=business_id)
    except (BusinessProfile.DoesNotExist, BusinessProfile.MultipleObjectsReturned):
        raise NotFound('No se encontró el negocio solicitado.')


@extend_schema(tags=['Business'], responses={200: PublicBusinessProfileSerializer})
class PublicBusinessProfileView(APIView):
    """Public, unauthenticated business profile — `marky.one/<business_id>`."""
    permission_classes = [AllowAny]
    throttle_scope = 'public'

    def get(self, request, business_id, *args, **kwargs):
        business = get_business_or_404(business_id, full=True)
        serializer = PublicBusinessProfileSerializer(
            business, context={'request': request, 'business_profile': business}
        )
        return Response(serializer.data)
