from drf_spectacular.utils import extend_schema, OpenApiParameter
from drf_spectacular.types import OpenApiTypes
from rest_framework import generics
from rest_framework.exceptions import NotFound
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from business.public_views import get_business_or_404
from .catalog import build_catalog_payload
from .models import Product
from .serializers import ProductCategoryBasicSerializer, ProductCategoryWithProductsSerializer
from .public_serializers import PublicProductSerializer


def _active_products():
    return Product.objects.filter(is_active=True)


@extend_schema(
    tags=['Products'],
    parameters=[
        OpenApiParameter(name='name', description='Filter by category name (case-insensitive)', required=False, type=OpenApiTypes.STR),
        OpenApiParameter(name='ids', description='Filter by a comma-separated list of category IDs', required=False, type=OpenApiTypes.STR),
        OpenApiParameter(name='has_promotion', description='Filter for categories with active promotions', required=False, type=OpenApiTypes.BOOL),
    ],
    responses={200: ProductCategoryWithProductsSerializer(many=True)},
)
class PublicCatalogView(generics.GenericAPIView):
    """Public categories+products catalog — `marky.one/<business_id>`.

    Mirrors ProductCategoryViewSet.with_products via the shared
    build_catalog_payload, scoped to `is_active=True` products so hidden
    products never leak to anonymous traffic. Deliberately never calls
    handle_expired_promotions_for_business — that writes rows and emits
    notifications, and must not fire on anonymous reads.
    """
    permission_classes = [AllowAny]
    throttle_scope = 'public'
    serializer_class = ProductCategoryWithProductsSerializer

    def get(self, request, business_id, *args, **kwargs):
        business = get_business_or_404(business_id)
        payload = build_catalog_payload(
            business=business,
            request=request,
            query_params=request.GET,
            paginator=self,
            serializer_class=ProductCategoryWithProductsSerializer,
            product_queryset=_active_products(),
        )
        return Response(payload)


@extend_schema(tags=['Products'], responses={200: ProductCategoryBasicSerializer(many=True)})
class PublicCategoryListView(generics.ListAPIView):
    """Public category list (filter chips/modal) — `marky.one/<business_id>`."""
    permission_classes = [AllowAny]
    throttle_scope = 'public'
    serializer_class = ProductCategoryBasicSerializer

    def get_queryset(self):
        business = get_business_or_404(self.kwargs['business_id'])
        return business.product_categories.all()


@extend_schema(tags=['Products'], responses={200: PublicProductSerializer})
class PublicProductDetailView(generics.GenericAPIView):
    """Public product detail — `marky.one/<business_id>/product/<id>`."""
    permission_classes = [AllowAny]
    throttle_scope = 'public'
    serializer_class = PublicProductSerializer

    def get(self, request, business_id, pk, *args, **kwargs):
        business = get_business_or_404(business_id)
        try:
            product = _active_products().select_related('category').prefetch_related(
                'variants', 'addons', 'media'
            ).get(business=business, pk=pk)
        except Product.DoesNotExist:
            raise NotFound('No se encontró el producto solicitado.')

        serializer = PublicProductSerializer(
            product, context={'request': request, 'business_profile': business}
        )
        return Response(serializer.data)
