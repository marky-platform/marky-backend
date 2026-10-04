from django.db import models
from django.db.models import Case, IntegerField, Value, When

from .filters import ProductCategoryFilter, product_has_active_promotion_q
from .models import Product, ProductCategory
from .promotions import INACTIVE
from .serializers import ProductLiteSerializer


def product_display_ordering():
    """Stopper products first (FAVORITE, then RECOMMENDED), then manual `order`, then id."""
    return [
        Case(
            When(stopper='FAVORITE', then=Value(0)),
            When(stopper='RECOMMENDED', then=Value(1)),
            default=Value(2),
            output_field=IntegerField(),
        ),
        'order',
        'id',
    ]


def build_catalog_payload(*, business, request, query_params, paginator=None,
                           serializer_class, product_queryset=None):
    """Build the categories+products catalog payload for a business.

    Shared by the authenticated admin endpoint (ProductCategoryViewSet.with_products)
    and the public read-only catalog endpoint. `business` replaces every
    `request.user.business_profile` read the original view body did, so a
    caller can look a business up by slug without requiring the requester to
    own it.

    `product_queryset` scopes which products are eligible to appear at all
    (e.g. `Product.objects.filter(is_active=True)` for public callers so
    hidden products never leak); `None` keeps current admin behavior (every
    product, regardless of `is_active`).

    `paginator` is a DRF view instance exposing `paginate_queryset`/
    `get_paginated_response` (a ModelViewSet/GenericAPIView, or None to
    always return the non-paginated shape).

    Returns a plain dict: `{'products_count': int, 'results': [...]}`, with
    pagination keys mixed in when `paginator` produced a page.
    """
    base_qs = ProductCategory.objects.filter(business=business)
    category_filter = ProductCategoryFilter(query_params, queryset=base_qs, product_queryset=product_queryset)
    filtered_qs = category_filter.qs
    has_promotion = bool(category_filter.form.cleaned_data.get('has_promotion'))

    products_base = Product.objects.filter(business=business)
    if product_queryset is not None:
        products_base = products_base & product_queryset
    products_base = products_base.order_by(*product_display_ordering())

    if has_promotion:
        promo_products = products_base.filter(product_has_active_promotion_q())
        products_count = promo_products.filter(category__in=filtered_qs).count()
        # Only the products that are actually on promotion should be shown within each category
        filtered_qs = filtered_qs.prefetch_related(
            models.Prefetch('products', queryset=promo_products)
        )
    else:
        products_count = products_base.filter(category__in=filtered_qs).count()
        # Always prefetch so products come back in display order. When
        # `product_queryset` is set this also scopes them the same way
        # (e.g. is_active=True) rather than every product in the category,
        # mirroring has_promotion above.
        filtered_qs = filtered_qs.prefetch_related(
            models.Prefetch('products', queryset=products_base)
        )

    # Products without a category aren't included in filtered_qs (there's no
    # ProductCategory row for them), so products_count must account for them
    # separately or a business whose only products are uncategorized would be
    # reported as having zero products.
    products_without_category = products_base.filter(category__isnull=True)
    if has_promotion:
        products_without_category = products_without_category.filter(product_has_active_promotion_q())
    products_count += products_without_category.count()

    def _serialize(qs):
        serializer = serializer_class(qs, many=True, context={'request': request, 'business_profile': business})
        serialized_data = list(serializer.data)

        if products_without_category.exists():
            uncategorized_products_serializer = ProductLiteSerializer(
                products_without_category, many=True, context={'request': request, 'business_profile': business}
            )
            no_category_data = {
                'id': None,
                'name': 'Sin categoría',
                'icon': 'fa-question-circle',
                'multibuy_option': None,
                'discount_percentage': 0,
                'promotion_starts_at': None,
                'promotion_ends_at': None,
                'promotion_status': INACTIVE,
                'products': uncategorized_products_serializer.data
            }
            serialized_data.append(no_category_data)

        return serialized_data

    if paginator is not None:
        page = paginator.paginate_queryset(filtered_qs)
        if page is not None:
            serialized_data = _serialize(page)
            paginated_response = paginator.get_paginated_response(serialized_data)
            result = paginated_response.data
            result['products_count'] = products_count
            return result

    return {
        'products_count': products_count,
        'results': _serialize(filtered_qs)
    }
