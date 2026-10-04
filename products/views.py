import logging

from django.db import transaction, models
from django_filters import rest_framework as filters

logger = logging.getLogger(__name__)
from drf_spectacular.utils import extend_schema, OpenApiParameter, extend_schema_view
from drf_spectacular.types import OpenApiTypes
from rest_framework import viewsets, status
from rest_framework.decorators import action
import json
from rest_framework.parsers import MultiPartParser, FormParser
from drf_nested_forms.parsers import NestedMultiPartParser
from rest_framework.response import Response
from rest_framework import serializers
from utils.permissions import IsBusinessOrSuperAdmin
from .filters import ProductCategoryFilter, product_has_active_promotion_q
from .models import ProductCategory, ProductVariant, ProductAddon
from .models import Product, next_product_order
from .services import handle_expired_promotions_for_business
from .catalog import build_catalog_payload
from .serializers import (
    ProductCategoryBasicSerializer,
    ProductCategoryWithProductsSerializer,
    ProductSerializer,
    ProductInputSerializer,
    PromotionSerializer,
    ProductCategoryOrderUpdateSerializer,
    ProductOrderUpdateSerializer,
)


@extend_schema_view(
    list=extend_schema(
        parameters=[
            OpenApiParameter(name='name', description='Filter by category name (case-insensitive)', required=False, type=OpenApiTypes.STR),
        ]
    ),
    with_products=extend_schema(
        parameters=[
            OpenApiParameter(name='name', description='Filter by category name (case-insensitive)', required=False, type=OpenApiTypes.STR),
            OpenApiParameter(name='ids', description='Filter by a comma-separated list of category IDs', required=False, type=OpenApiTypes.STR),
            OpenApiParameter(name='has_promotion', description='Filter for categories with active promotions', required=False, type=OpenApiTypes.BOOL),
        ]
    )
)
@extend_schema(tags=['Products'])
class ProductCategoryViewSet(viewsets.ModelViewSet):
    permission_classes = [IsBusinessOrSuperAdmin]
    serializer_class = ProductCategoryBasicSerializer
    queryset = ProductCategory.objects.all()

    def get_base_queryset(self):
        user = self.request.user
        if user.is_authenticated and hasattr(user, 'business_profile'):
            handle_expired_promotions_for_business(user.business_profile)
            return ProductCategory.objects.filter(business=user.business_profile)
        return ProductCategory.objects.none()

    def get_queryset(self):
        queryset = self.get_base_queryset()

        if self.action == 'list':
            name = self.request.query_params.get('name')
            if name:
                queryset = queryset.filter(name__icontains=name)

        return queryset

    @extend_schema(
        parameters=[
            OpenApiParameter(
                name='name',
                type=str,
                location=OpenApiParameter.QUERY,
                required=False,
                description='Filter categories by name (case-insensitive partial match)'
            )
        ]
    )
    def list(self, request, *args, **kwargs):
        return super().list(request, *args, **kwargs)

    def perform_create(self, serializer):
        business = self.request.user.business_profile
        max_order = ProductCategory.objects.filter(business=business).aggregate(models.Max('order'))['order__max'] or 0
        serializer.save(business=business, order=max_order + 1)

    @extend_schema(
        request=ProductCategoryOrderUpdateSerializer,
        responses={200: None}
    )
    @action(detail=False, methods=['post'])
    def update_order(self, request):
        # TODO: let's see if we can optimize this w/bulk update
        categories_data = request.data.get('categories', [])

        try:
            with transaction.atomic():
                for category_data in categories_data:
                    ProductCategory.objects.filter(id=category_data['id']).update(order=category_data['order'])
        except Exception:
            logger.exception("Error updating category order for user %s", request.user.id)
            return Response({'error': 'Ha ocurrido un error inesperado.'}, status=status.HTTP_400_BAD_REQUEST)

        return Response({'status': 'Order updated successfully'}, status=status.HTTP_200_OK)

    @extend_schema(
        request=ProductOrderUpdateSerializer,
        responses={200: None},
        tags=['Products'],
    )
    @action(detail=True, methods=['post'], serializer_class=ProductOrderUpdateSerializer)
    def update_products_order(self, request, pk=None):
        """Persist the manual order of every product in this category.

        The list must contain exactly the category's current product ids, so a
        stale client (e.g. a product added from another tab) can't silently
        mis-order anything.
        """
        category = self.get_object()
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        product_ids = serializer.validated_data['product_ids']

        products = {p.id: p for p in Product.objects.filter(category=category)}
        if len(set(product_ids)) != len(product_ids) or set(product_ids) != set(products):
            return Response(
                {'error': 'La lista de productos cambió. Actualiza la página e inténtalo de nuevo.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        ordered = []
        for index, product_id in enumerate(product_ids, start=1):
            product = products[product_id]
            product.order = index
            ordered.append(product)
        with transaction.atomic():
            Product.objects.bulk_update(ordered, ['order'])

        return Response({'status': 'Products order updated successfully'}, status=status.HTTP_200_OK)

    @action(detail=True, methods=['post'], serializer_class=PromotionSerializer)
    def add_promotion(self, request, pk=None):
        category = self.get_object()
        serializer = self.get_serializer(category, data=request.data, partial=True)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    @extend_schema(
        responses=ProductCategoryWithProductsSerializer(many=True),
        # Swagger will infer parameters from ProductCategoryFilter
    )
    @action(detail=False, methods=['get'], serializer_class=ProductCategoryWithProductsSerializer)
    def with_products(self, request):
        business = self.request.user.business_profile
        handle_expired_promotions_for_business(business)
        payload = build_catalog_payload(
            business=business,
            request=request,
            query_params=request.GET,
            paginator=self,
            serializer_class=ProductCategoryWithProductsSerializer,
        )
        return Response(payload)


@extend_schema(tags=['Products'])
class ProductViewSet(viewsets.ModelViewSet):
    permission_classes = [IsBusinessOrSuperAdmin]
    queryset = Product.objects.all()
    parser_classes = (NestedMultiPartParser, FormParser)

    def get_serializer_class(self):
        if self.action in ['create', 'update', 'partial_update']:
            return ProductInputSerializer
        return ProductSerializer

    def get_queryset(self):
        user = self.request.user
        if user.is_authenticated and hasattr(user, 'business_profile'):
            handle_expired_promotions_for_business(user.business_profile)
            return Product.objects.filter(business=user.business_profile)
        return Product.objects.none()

    def perform_create(self, serializer):
        category = serializer.validated_data.get('category')
        extra = {'order': next_product_order(category)} if category is not None else {}
        serializer.save(business=self.request.user.business_profile, **extra)

    def perform_update(self, serializer):
        new_category = serializer.validated_data.get('category', serializer.instance.category)
        if new_category is not None and new_category != serializer.instance.category:
            serializer.save(order=next_product_order(new_category))
        else:
            serializer.save()
