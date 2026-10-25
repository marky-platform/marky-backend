from drf_spectacular.utils import extend_schema, OpenApiParameter
from drf_spectacular.types import OpenApiTypes
from rest_framework import generics
from rest_framework.exceptions import NotFound
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from business.public_views import get_business_or_404
from .catalog import build_catalog_payload, restrict_to_visible_categories
from .models import Product
from .serializers import ProductCategoryBasicSerializer, ProductCategoryWithProductsSerializer
from .public_serializers import PublicProductSerializer
from .slugs import UNCATEGORIZED_SLUG

PRODUCT_NOT_FOUND_MESSAGE = 'No se encontró el producto solicitado.'


def _active_products():
    return Product.objects.filter(is_active=True)


@extend_schema(
    tags=['Products'],
    parameters=[
        OpenApiParameter(name='name', description='Filter by category name (case-insensitive)', required=False, type=OpenApiTypes.STR),
        OpenApiParameter(name='ids', description='Filter by a comma-separated list of category IDs', required=False, type=OpenApiTypes.STR),
        OpenApiParameter(name='has_promotion', description='Filter for categories with active promotions', required=False, type=OpenApiTypes.BOOL),
        OpenApiParameter(name='has_featured', description='Only products marked as Favorite or Recommended', required=False, type=OpenApiTypes.BOOL),
        OpenApiParameter(name='include_counts', description='Include filter_counts ({promotion, featured}) in the response', required=False, type=OpenApiTypes.BOOL),
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
        return restrict_to_visible_categories(
            business.product_categories.all(),
            business=business,
            product_queryset=_active_products(),
        )


def _public_product_response(request, business, **lookup):
    """Shared by the canonical and legacy detail views so both apply the same
    visibility rules (active only, scoped to `business`) and payload."""
    try:
        product = _active_products().select_related('category', 'model_3d').prefetch_related(
            'variants', 'addons', 'media'
        ).get(business=business, **lookup)
    except Product.DoesNotExist:
        raise NotFound(PRODUCT_NOT_FOUND_MESSAGE)

    serializer = PublicProductSerializer(
        product, context={'request': request, 'business_profile': business}
    )
    return Response(serializer.data)


@extend_schema(tags=['Products'], responses={200: PublicProductSerializer})
class PublicProductDetailView(generics.GenericAPIView):
    """LEGACY public product detail by numeric id — `marky.one/<business_id>/product/<id>`.

    Kept for existing clients/bookmarks during the canonical-URL rollout; the
    frontend resolves it and redirects to the canonical slug URL. New clients
    should use PublicProductBySlugView. Remove only in a separate release once
    legacy traffic is negligible.
    """
    permission_classes = [AllowAny]
    throttle_scope = 'public'
    serializer_class = PublicProductSerializer

    def get(self, request, business_id, pk, *args, **kwargs):
        business = get_business_or_404(business_id)
        return _public_product_response(request, business, pk=pk)


@extend_schema(tags=['Products'], responses={200: PublicProductSerializer})
class PublicProductBySlugView(generics.GenericAPIView):
    """Canonical public product detail — `marky.one/<business_id>/<category-slug>/<product-slug>`.

    `sin-categoria` resolves only to uncategorized products; any other
    category segment must be a category of the same business. A product under
    another business, another category, hidden, or missing is a 404.
    """
    permission_classes = [AllowAny]
    throttle_scope = 'public'
    serializer_class = PublicProductSerializer

    def get(self, request, business_id, category_slug, product_slug, *args, **kwargs):
        business = get_business_or_404(business_id)
        if category_slug == UNCATEGORIZED_SLUG:
            category_lookup = {'category__isnull': True}
        else:
            category_lookup = {'category__business': business, 'category__slug': category_slug}
        return _public_product_response(request, business, slug=product_slug, **category_lookup)
