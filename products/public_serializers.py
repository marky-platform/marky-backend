from decimal import Decimal

from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers

from .models import ProductAddon, ProductVariant
from .product_extras import validate_celiac_info, validate_presentation
from .promotions import resolve_effective_promotion, ACTIVE as PROMOTION_ACTIVE
from .serializers import (
    ProductPriceMixin,
    ProductMediaSerializer,
    ProductCategoryLiteSerializer,
)


class PublicProductVariantSerializer(ProductPriceMixin, serializers.ModelSerializer):
    """Public-safe variant serializer with an explicit field list (no `product` FK)."""
    primary_price = serializers.SerializerMethodField()
    secondary_price = serializers.SerializerMethodField()

    class Meta:
        model = ProductVariant
        fields = ['id', 'name', 'price', 'description', 'image', 'primary_price', 'secondary_price']


class PublicProductAddonSerializer(ProductPriceMixin, serializers.ModelSerializer):
    """Public-safe addon serializer with an explicit field list (no `product` FK)."""
    primary_price = serializers.SerializerMethodField()
    secondary_price = serializers.SerializerMethodField()

    class Meta:
        model = ProductAddon
        fields = ['id', 'name', 'price', 'primary_price', 'secondary_price']


class PublicProductSerializer(ProductPriceMixin, serializers.Serializer):
    """
    Public, unauthenticated-safe view of a single product
    (`marky.one/<slug>/product/<id>`).

    Does NOT reuse ProductSerializer, which exposes `business`, `is_active`,
    and the raw unresolved/expired promo window fields meant for the admin
    edit form. Mirrors ProductLiteSerializer's resolved-promotion fields
    (product overrides category, via resolve_effective_promotion) and adds
    `stopper`, `category` (id + name only), full `media`, `variants` and
    `addons` with explicit field lists, plus the descriptive extras
    (`featured_ingredients`, `presentation`, `allergens`, `celiac_info`) in the
    same raw shape the admin serializer emits.
    """
    id = serializers.IntegerField(read_only=True)
    name = serializers.CharField(read_only=True)
    description = serializers.CharField(read_only=True)
    price = serializers.DecimalField(max_digits=10, decimal_places=2, read_only=True)
    stopper = serializers.CharField(read_only=True, allow_null=True)
    is_available = serializers.BooleanField(read_only=True)
    category = ProductCategoryLiteSerializer(read_only=True)
    media = ProductMediaSerializer(many=True, read_only=True)
    variants = PublicProductVariantSerializer(many=True, read_only=True)
    addons = PublicProductAddonSerializer(many=True, read_only=True)
    featured_ingredients = serializers.CharField(read_only=True, allow_null=True)
    allergens = serializers.CharField(read_only=True, allow_null=True)
    presentation = serializers.SerializerMethodField()
    celiac_info = serializers.SerializerMethodField()

    multibuy_option = serializers.SerializerMethodField()
    discount_percentage = serializers.SerializerMethodField()
    promotion_starts_at = serializers.SerializerMethodField()
    promotion_ends_at = serializers.SerializerMethodField()
    promotion_status = serializers.SerializerMethodField()
    primary_price = serializers.SerializerMethodField()
    secondary_price = serializers.SerializerMethodField()
    primary_price_with_discount = serializers.SerializerMethodField()
    secondary_price_with_discount = serializers.SerializerMethodField()

    @staticmethod
    def _sanitized(validator, value):
        """Re-run the write-side validator so a row edited outside the API
        (Django admin, SQL) can never leak a malformed shape publicly."""
        if value is None:
            return None
        try:
            return validator(value)
        except (DjangoValidationError, serializers.ValidationError):
            return None

    def get_presentation(self, obj):
        return self._sanitized(validate_presentation, obj.presentation)

    def get_celiac_info(self, obj):
        return self._sanitized(validate_celiac_info, obj.celiac_info)

    def _get_promotion_bundle(self, obj):
        cache = self.context.setdefault('_promotion_cache', {})
        if obj.pk not in cache:
            cache[obj.pk] = resolve_effective_promotion(obj)
        return cache[obj.pk]

    def get_multibuy_option(self, obj):
        bundle = self._get_promotion_bundle(obj)
        return bundle['multibuy_option'] if bundle['status'] == PROMOTION_ACTIVE else None

    def get_discount_percentage(self, obj):
        bundle = self._get_promotion_bundle(obj)
        return bundle['discount_percentage'] if bundle['status'] == PROMOTION_ACTIVE else Decimal('0')

    def get_promotion_starts_at(self, obj):
        return self._get_promotion_bundle(obj)['promotion_starts_at']

    def get_promotion_ends_at(self, obj):
        return self._get_promotion_bundle(obj)['promotion_ends_at']

    def get_promotion_status(self, obj):
        return self._get_promotion_bundle(obj)['status']

    # get_primary_price / get_secondary_price / get_primary_price_with_discount /
    # get_secondary_price_with_discount are inherited from ProductPriceMixin.
