from django.db.models import Q, Exists, OuterRef
from django.utils import timezone
from django_filters import rest_framework as filters
from .models import ProductCategory, Product


class NumberInFilter(filters.BaseInFilter, filters.NumberFilter):
    pass


def _promotion_active_q(now, prefix=''):
    """Q object for 'has a direct, currently-active promotion', on the given model/relation prefix."""
    multibuy = f'{prefix}multibuy_option'
    discount = f'{prefix}discount_percentage'
    starts = f'{prefix}promotion_starts_at'
    ends = f'{prefix}promotion_ends_at'
    # '' (the product form's "no offer") is not a promotion, same as NULL:
    # mirrors promotions.has_promotion_configured, which uses bool(multibuy).
    has_multibuy = Q(**{f'{multibuy}__isnull': False}) & ~Q(**{multibuy: ''})
    return (
        has_multibuy | Q(**{f'{discount}__gt': 0})
    ) & (
        Q(**{f'{starts}__isnull': True}) | Q(**{f'{starts}__lte': now})
    ) & (
        Q(**{f'{ends}__isnull': True}) | Q(**{f'{ends}__gte': now})
    )


def product_has_active_promotion_q(now=None):
    """Q object (for the Product model) matching products effectively on promotion:
    a promotion on the product itself, or an active promotion inherited from its category.
    Mirrors ProductLiteSerializer.get_multibuy_option/get_discount_percentage.
    """
    if now is None:
        now = timezone.now()
    return _promotion_active_q(now) | _promotion_active_q(now, prefix='category__')


def product_is_featured_q():
    """Q object (for the Product model) matching "featured" products:
    featured = recommended OR favorite (stopper IN FAVORITE, RECOMMENDED)."""
    return Q(stopper__in=['FAVORITE', 'RECOMMENDED'])


class ProductCategoryFilter(filters.FilterSet):
    name = filters.CharFilter(lookup_expr='icontains')
    ids = NumberInFilter(field_name='id', lookup_expr='in')
    has_promotion = filters.BooleanFilter(method='filter_has_promotion')
    has_featured = filters.BooleanFilter(method='filter_has_featured')

    class Meta:
        model = ProductCategory
        fields = ['name', 'ids', 'has_promotion', 'has_featured']

    def __init__(self, *args, product_queryset=None, **kwargs):
        # Scopes the has_promotion Exists subquery below to the same set of
        # products the caller is allowed to see (e.g. is_active=True for the
        # public catalog) — without this, a category whose only promoted
        # product is hidden would still pass this category-level filter
        # (just with an empty products list), showing up as a phantom
        # "on promotion" category to anonymous callers. None (the admin
        # default) preserves prior behavior: every product counts.
        self.product_queryset = product_queryset if product_queryset is not None else Product.objects.all()
        super().__init__(*args, **kwargs)

    def filter_has_promotion(self, queryset, name, value):
        now = timezone.now()
        promotion_conditions = _promotion_active_q(now)

        # Subquery to check for products with promotions within the category
        products_with_promotions = self.product_queryset.filter(
            category=OuterRef('pk')
        ).filter(promotion_conditions)

        if value:
            # Filter for categories that either have a promotion themselves or have products with promotions
            return queryset.filter(
                promotion_conditions | Q(Exists(products_with_promotions))
            ).distinct()
        return queryset

    def filter_has_featured(self, queryset, name, value):
        if not value:
            return queryset

        # Evaluated per product: when has_promotion=true is also requested the
        # same product must be featured AND effectively on promotion, so a
        # category can't match through two different products.
        conditions = product_is_featured_q()
        if self.form.cleaned_data.get('has_promotion'):
            conditions &= product_has_active_promotion_q()

        matching_products = self.product_queryset.filter(category=OuterRef('pk')).filter(conditions)
        return queryset.filter(Exists(matching_products))
