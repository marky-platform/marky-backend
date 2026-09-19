from collections.abc import Mapping

from rest_framework import serializers
from django.db import transaction, IntegrityError
from decimal import Decimal

from .models import Product, ProductCategory, ProductVariant, ProductAddon, ProductMedia
from .validators import validate_media_extension, validate_media_size
from .promotions import (
    resolve_effective_promotion,
    compute_promotion_status,
    ACTIVE as PROMOTION_ACTIVE,
)

# Currencies conventionally quoted without cents (mirrors ISO 4217 zero-decimal
# currencies relevant to Marky's markets, e.g. Guaraní Paraguayo).
ZERO_DECIMAL_CURRENCY_CODES = {'PYG'}


def format_currency_amount(amount, code):
    """Format a Decimal amount consistently for display, e.g. 'USD 1.000.000,00' or 'PYG 150.000'.

    Decimal places are currency-aware: zero-decimal currencies (see
    ZERO_DECIMAL_CURRENCY_CODES) are shown without cents, everything else
    with exactly 2. Thousands are dot-separated, decimals comma-separated
    (LATAM/Spanish formatting), matching how price inputs are normalized.
    """
    from decimal import Decimal, ROUND_HALF_UP

    if amount is None:
        return None

    if not isinstance(amount, Decimal):
        try:
            amount = Decimal(str(amount))
        except Exception:
            return None

    decimal_places = 0 if code in ZERO_DECIMAL_CURRENCY_CODES else 2
    quantum = Decimal('1') if decimal_places == 0 else Decimal('0.01')
    quantized = amount.quantize(quantum, rounding=ROUND_HALF_UP)

    if decimal_places == 0:
        integer_with_commas = f"{int(quantized):,}"
        integer_with_dots = integer_with_commas.replace(',', '.')
        return f"{code} {integer_with_dots}"

    s = f"{quantized:.2f}"
    integer_part, decimal_part = s.split('.')
    integer_with_commas = f"{int(integer_part):,}"  # '1,000,000'
    integer_with_dots = integer_with_commas.replace(',', '.')
    return f"{code} {integer_with_dots},{decimal_part}"


class ProductPriceMixin:
    """Provide helper methods used to compute/format primary/secondary prices.
    Field declarations (SerializerMethodField) must live on the concrete
    serializer classes so DRF will register them properly.
    """

    def _get_business_profile(self, obj):
        context_bp = self.context.get('business_profile')
        if context_bp is not None:
            return context_bp
        request = self.context.get('request')
        if request and hasattr(request.user, 'business_profile'):
            return request.user.business_profile
        # Try direct business attribute (Product)
        bp = getattr(obj, 'business', None)
        if bp:
            return bp

        # Fallback for related objects (ProductVariant, ProductAddon) which have a product
        product = getattr(obj, 'product', None)
        if product is not None:
            return getattr(product, 'business', None)

        return None

    def _format_currency_amount(self, amount, code):
        return format_currency_amount(amount, code)

    def _get_effective_discount_percentage(self, obj):
        """Return the effective Decimal discount percentage for a product,
        preferring the product's own discount when active, else the category's.
        Returns Decimal('0') when there is no currently-active discount, or
        when `obj` doesn't carry promo fields at all (e.g. ProductVariant/ProductAddon).
        """
        if not hasattr(obj, 'discount_percentage'):
            return Decimal('0')

        bundle = resolve_effective_promotion(obj)
        if bundle['status'] != PROMOTION_ACTIVE:
            return Decimal('0')
        return bundle['discount_percentage'] or Decimal('0')

    def _get_discounted_amounts(self, obj):
        """Return (discounted_primary, discounted_secondary, primary_currency, secondary_currency).

        If there is no active discount or currencies/amounts are missing, returns
        (None, None, None, None) or (None, None, primary_currency, secondary_currency)
        as appropriate.
        """
        bp = self._get_business_profile(obj)
        if not bp:
            return None, None, None, None

        primary_amount, secondary_amount, primary_currency, secondary_currency = obj.get_primary_secondary_amounts(bp)

        if primary_amount is None or primary_currency is None:
            return None, None, None, None

        percent = self._get_effective_discount_percentage(obj)
        if not percent or percent <= 0:
            return None, None, primary_currency, secondary_currency

        # Clamp percent between 0 and 100
        if percent < 0:
            percent = Decimal('0')
        if percent > Decimal('100'):
            percent = Decimal('100')

        factor = (Decimal('100') - percent) / Decimal('100')

        try:
            discounted_primary = primary_amount * factor
        except Exception:
            discounted_primary = None

        discounted_secondary = None
        if secondary_amount is not None:
            try:
                discounted_secondary = secondary_amount * factor
            except Exception:
                discounted_secondary = None

        return discounted_primary, discounted_secondary, primary_currency, secondary_currency

    def get_primary_price(self, obj):
        bp = self._get_business_profile(obj)
        if not bp:
            return None
        primary_amount, _, primary_currency, _ = obj.get_primary_secondary_amounts(bp)
        if primary_amount is None or primary_currency is None:
            return None
        return self._format_currency_amount(primary_amount, primary_currency.code)

    def get_primary_price_with_discount(self, obj):
        discounted_primary, _, primary_currency, _ = self._get_discounted_amounts(obj)
        if discounted_primary is None or primary_currency is None:
            return None
        return self._format_currency_amount(discounted_primary, primary_currency.code)

    def get_secondary_price(self, obj):
        bp = self._get_business_profile(obj)
        if not bp:
            return None
        _, secondary_amount, _, secondary_currency = obj.get_primary_secondary_amounts(bp)
        if secondary_amount is None or secondary_currency is None:
            return None
        return self._format_currency_amount(secondary_amount, secondary_currency.code)

    def get_secondary_price_with_discount(self, obj):
        _, discounted_secondary, _, secondary_currency = self._get_discounted_amounts(obj)
        if discounted_secondary is None or secondary_currency is None:
            return None
        return self._format_currency_amount(discounted_secondary, secondary_currency.code)


class ProductMediaSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProductMedia
        fields = ['id', 'file', 'media_type', 'order']


class ProductMediaInputSerializer(serializers.ModelSerializer):
    id = serializers.IntegerField(required=False)
    file = serializers.FileField(
        required=False,
        allow_null=True,
        validators=[validate_media_extension, validate_media_size],
    )
    _delete = serializers.BooleanField(required=False, default=False)

    class Meta:
        model = ProductMedia
        fields = ['id', 'file', 'media_type', 'order', '_delete']

    def to_internal_value(self, data):
        if "_delete" in data and isinstance(data["_delete"], str):
            data["_delete"] = data["_delete"].lower() in ("true", "1")
        return super().to_internal_value(data)


class DeletableNestedItemMixin:
    """Lets a nested child row be sent as a bare tombstone: {id, _delete: true}.

    Removing a variant/addon only needs its id, but the wrapped ModelSerializer
    otherwise requires `name`/`price` (they're non-blank on the model), which
    would reject a client that (reasonably) omits fields it isn't changing.
    Mirrors the string coercion ProductMediaInputSerializer.to_internal_value
    already does for `_delete`, since multipart delivers it as "true"/"1".
    """

    def to_internal_value(self, data):
        if isinstance(data, Mapping):
            flag = data.get('_delete')
            if isinstance(flag, str):
                flag = flag.strip().lower() in ('true', '1')
            item_id = data.get('id')
            if flag and item_id not in (None, ''):
                return {'id': int(item_id), '_delete': True}
        return super().to_internal_value(data)


class ProductAddonSerializer(ProductPriceMixin, serializers.ModelSerializer):
    primary_price = serializers.SerializerMethodField()
    secondary_price = serializers.SerializerMethodField()

    class Meta:
        model = ProductAddon
        fields = '__all__'


class ProductAddonInputSerializer(DeletableNestedItemMixin, serializers.ModelSerializer):
    id = serializers.IntegerField(required=False)
    _delete = serializers.BooleanField(required=False, default=False)

    class Meta:
        model = ProductAddon
        fields = ['id', 'name', 'price', '_delete']


class ProductVariantSerializer(ProductPriceMixin, serializers.ModelSerializer):
    primary_price = serializers.SerializerMethodField()
    secondary_price = serializers.SerializerMethodField()

    class Meta:
        model = ProductVariant
        fields = '__all__'


class ProductVariantInputSerializer(DeletableNestedItemMixin, serializers.ModelSerializer):
    image = serializers.ImageField(required=False)
    id = serializers.IntegerField(required=False)
    _delete = serializers.BooleanField(required=False, default=False)

    class Meta:
        model = ProductVariant
        fields = ['id', 'name', 'price', 'description', 'image', '_delete']

    def validate(self, attrs):
        # A new variant (no `id`) must always carry an image. An existing
        # variant being updated doesn't have to resend it — the frontend
        # omits `image` from the payload when it's unchanged (already an
        # uploaded file on disk), and legacy variants saved before this rule
        # existed shouldn't be invalidated by an unrelated-field edit.
        #
        # No `_delete` exemption here: an id-less item is always a *create*
        # regardless of `_delete` (see ProductInputSerializer.create()/
        # process_related(), which only honor `_delete` for items that
        # already have an `id`) — so exempting `_delete` would let a payload
        # like `{"_delete": true}` with no id/image sail through validation
        # and still get created as an image-less variant.
        if not attrs.get('id') and not attrs.get('image'):
            raise serializers.ValidationError({
                'image': 'La imagen es requerida para cada presentación.'
            })
        return attrs


class ProductCategoryLiteSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProductCategory
        fields = ['id', 'name']


class ProductCategoryBasicSerializer(serializers.ModelSerializer):
    promotion_status = serializers.SerializerMethodField()

    class Meta:
        model = ProductCategory
        fields = [
            'id', 'name', 'icon', 'multibuy_option', 'discount_percentage',
            'promotion_starts_at', 'promotion_ends_at', 'promotion_status', 'is_available'
        ]

    def get_promotion_status(self, obj):
        return compute_promotion_status(
            obj.multibuy_option, obj.discount_percentage,
            obj.promotion_starts_at, obj.promotion_ends_at,
        )


class ProductSerializer(ProductPriceMixin, serializers.ModelSerializer):
    primary_price = serializers.SerializerMethodField()
    secondary_price = serializers.SerializerMethodField()
    primary_price_with_discount = serializers.SerializerMethodField()
    secondary_price_with_discount = serializers.SerializerMethodField()
    variants = ProductVariantSerializer(many=True, read_only=True)
    addons = ProductAddonSerializer(many=True, read_only=True)
    media = ProductMediaSerializer(many=True, read_only=True)
    category = ProductCategoryLiteSerializer(read_only=True)
    # Category promotion fields (read-only, sourced from related ProductCategory)
    category_multibuy_option = serializers.CharField(source='category.multibuy_option', read_only=True, allow_null=True)
    category_discount_percentage = serializers.DecimalField(source='category.discount_percentage', max_digits=5, decimal_places=2, read_only=True)
    category_promotion_starts_at = serializers.DateTimeField(source='category.promotion_starts_at', read_only=True, allow_null=True)
    category_promotion_ends_at = serializers.DateTimeField(source='category.promotion_ends_at', read_only=True, allow_null=True)
    # Status of the PRODUCT'S OWN promo config (not category-inherited): this
    # is the edit form for this product's row, so "Esta promoción ya
    # finalizó" must reflect what's actually stored on it, not an inherited
    # category promo (see category_promotion_status for that).
    promotion_status = serializers.SerializerMethodField()
    category_promotion_status = serializers.SerializerMethodField()

    class Meta:
        model = Product
        fields = [
            'id', 'name', 'description', 'price', 'category', 'is_active',
            'category_multibuy_option', 'category_discount_percentage', 'category_promotion_starts_at', 'category_promotion_ends_at',
            'category_promotion_status',
            'stopper', 'multibuy_option', 'discount_percentage', 'is_available',
            'promotion_starts_at', 'promotion_ends_at', 'promotion_status', 'business',
            'variants', 'addons', 'media',
            # Human-readable prices for the business context
            'primary_price', 'secondary_price',
            # Human-readable discounted prices
            'primary_price_with_discount', 'secondary_price_with_discount'
        ]

    def get_promotion_status(self, obj):
        return compute_promotion_status(
            obj.multibuy_option, obj.discount_percentage,
            obj.promotion_starts_at, obj.promotion_ends_at,
        )

    def get_category_promotion_status(self, obj):
        category = getattr(obj, 'category', None)
        if category is None:
            return None
        return compute_promotion_status(
            category.multibuy_option, category.discount_percentage,
            category.promotion_starts_at, category.promotion_ends_at,
        )


class ProductLiteSerializer(ProductPriceMixin, serializers.ModelSerializer):
    isFavorite = serializers.SerializerMethodField()
    isRecommended = serializers.SerializerMethodField()
    image = serializers.SerializerMethodField()
    # Promotion fields: resolved (product-overrides-category, as one atomic
    # bundle) via resolve_effective_promotion — see _get_promotion_bundle.
    multibuy_option = serializers.SerializerMethodField()
    discount_percentage = serializers.SerializerMethodField()
    promotion_starts_at = serializers.SerializerMethodField()
    promotion_ends_at = serializers.SerializerMethodField()
    promotion_status = serializers.SerializerMethodField()
    primary_price = serializers.SerializerMethodField()
    secondary_price = serializers.SerializerMethodField()
    # Human-readable discounted prices (None when there is no active percentage discount)
    primary_price_with_discount = serializers.SerializerMethodField()
    secondary_price_with_discount = serializers.SerializerMethodField()

    class Meta:
        model = Product
        fields = ['id', 'name', 'description', 'price', 'isFavorite', 'isRecommended', 'image',
                  'multibuy_option', 'discount_percentage', 'promotion_starts_at', 'promotion_ends_at',
                  'promotion_status', 'is_available',
                  'primary_price', 'secondary_price',
                  'primary_price_with_discount', 'secondary_price_with_discount']

    # _get_business_profile is inherited from ProductPriceMixin — for a
    # Product instance it resolves identically (context override, then
    # request.user's own profile, then obj.business), so overriding it here
    # too only risked the two copies drifting out of sync on a future fix
    # (the A2 context-priority patch already had to touch both).

    def _get_amounts(self, obj):
        """Memoized get_primary_secondary_amounts(bp) per object.

        This serializer is reused across every product in a list (ListSerializer
        calls the same child instance per item), and primary/secondary/discounted
        price fields each need this same tuple — cache it per-pk to avoid redoing
        the Decimal arithmetic and business-profile lookup once per field.
        """
        cache = self.context.setdefault('_amounts_cache', {})
        if obj.pk not in cache:
            bp = self._get_business_profile(obj)
            cache[obj.pk] = obj.get_primary_secondary_amounts(bp) if bp else (None, None, None, None)
        return cache[obj.pk]

    def get_primary_price(self, obj):
        primary_amount, _, primary_currency, _ = self._get_amounts(obj)
        if primary_amount is None or primary_currency is None:
            return None
        return self._format_currency_amount(primary_amount, primary_currency.code)

    def get_secondary_price(self, obj):
        _, secondary_amount, _, secondary_currency = self._get_amounts(obj)
        if secondary_amount is None or secondary_currency is None:
            return None
        return self._format_currency_amount(secondary_amount, secondary_currency.code)

    def _get_promotion_bundle(self, obj):
        """Memoized resolve_effective_promotion(obj) per-pk (see _get_amounts)."""
        cache = self.context.setdefault('_promotion_cache', {})
        if obj.pk not in cache:
            cache[obj.pk] = resolve_effective_promotion(obj)
        return cache[obj.pk]

    def _get_discounted_amounts(self, obj):
        primary_amount, secondary_amount, primary_currency, secondary_currency = self._get_amounts(obj)
        if primary_amount is None or primary_currency is None:
            return None, None, None, None

        bundle = self._get_promotion_bundle(obj)
        percent = bundle['discount_percentage'] if bundle['status'] == PROMOTION_ACTIVE else None
        if not percent or percent <= 0:
            return None, None, primary_currency, secondary_currency

        if percent < 0:
            percent = Decimal('0')
        if percent > Decimal('100'):
            percent = Decimal('100')

        factor = (Decimal('100') - percent) / Decimal('100')

        try:
            discounted_primary = primary_amount * factor
        except Exception:
            discounted_primary = None

        discounted_secondary = None
        if secondary_amount is not None:
            try:
                discounted_secondary = secondary_amount * factor
            except Exception:
                discounted_secondary = None

        return discounted_primary, discounted_secondary, primary_currency, secondary_currency

    def get_isFavorite(self, obj):
        return obj.stopper == 'FAVORITE'

    def get_isRecommended(self, obj):
        return obj.stopper == 'RECOMMENDED'

    def get_image(self, obj):
        request = self.context.get('request')
        media = obj.media.filter(media_type=ProductMedia.IMAGE).first()
        if media and request:
            return request.build_absolute_uri(media.file.url)
        elif media:
            return media.file.url
        return None

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


class ProductCategoryWithProductsSerializer(serializers.ModelSerializer):
    products = ProductLiteSerializer(many=True, read_only=True)
    promotion_status = serializers.SerializerMethodField()

    class Meta:
        model = ProductCategory
        fields = [
            'id', 'name', 'icon', 'multibuy_option', 'discount_percentage',
            'promotion_starts_at', 'promotion_ends_at', 'promotion_status', 'is_available', 'products'
        ]

    def get_promotion_status(self, obj):
        return compute_promotion_status(
            obj.multibuy_option, obj.discount_percentage,
            obj.promotion_starts_at, obj.promotion_ends_at,
        )


class PromotionSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProductCategory
        fields = ['multibuy_option', 'discount_percentage', 'promotion_starts_at', 'promotion_ends_at']

    def validate(self, attrs):
        # Same rule as ProductInputSerializer: a promotion window with no
        # discount/multibuy type configured is a no-op, so reject it when
        # this request is the one setting the dates.
        promo_fields_touched = any(
            field in attrs
            for field in ('multibuy_option', 'discount_percentage', 'promotion_starts_at', 'promotion_ends_at')
        )
        if promo_fields_touched:
            multibuy_option = attrs.get('multibuy_option', getattr(self.instance, 'multibuy_option', None))
            discount_percentage = attrs.get('discount_percentage', getattr(self.instance, 'discount_percentage', None))
            starts_at = attrs.get('promotion_starts_at', getattr(self.instance, 'promotion_starts_at', None))
            ends_at = attrs.get('promotion_ends_at', getattr(self.instance, 'promotion_ends_at', None))
            has_type = bool(multibuy_option) or (discount_percentage is not None and discount_percentage > 0)
            has_dates = bool(starts_at) or bool(ends_at)
            if has_dates and not has_type:
                raise serializers.ValidationError({
                    'discount_percentage': 'Debes seleccionar Descuento u Oferta para activar la promoción.'
                })
        return attrs


class ProductCategoryOrderSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    order = serializers.IntegerField()


class ProductCategoryOrderUpdateSerializer(serializers.Serializer):
    categories = ProductCategoryOrderSerializer(many=True)


class ProductInputSerializer(serializers.ModelSerializer):
    variants = ProductVariantInputSerializer(many=True, required=False)
    addons = ProductAddonInputSerializer(many=True, required=False)
    media = ProductMediaInputSerializer(many=True, required=False)
    description = serializers.CharField(max_length=300)
    category = serializers.PrimaryKeyRelatedField(
        queryset=ProductCategory.objects.none(), required=False, allow_null=True
    )

    class Meta:
        model = Product
        fields = [
            'name', 'description', 'price', 'category', 'is_active',
            'stopper', 'multibuy_option', 'discount_percentage',
            'promotion_starts_at', 'promotion_ends_at',
            'variants', 'addons', 'media', 'is_available',
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        request = self.context.get('request')
        if request is not None and hasattr(request.user, 'business_profile'):
            self.fields['category'].queryset = ProductCategory.objects.filter(
                business=request.user.business_profile
            )

    # Fields the caller never sent are left untouched by DRF's own partial
    # (PATCH) handling for free — that's what stops an unrelated-field save
    # from wiping promo config. What DRF does NOT handle for free is a
    # caller that explicitly wants to CLEAR a field: multipart/form-data (and
    # drf_nested_forms' own dict-swap for nested bracketed keys, see
    # promotions.py) means '' arrives as a literal string rather than being
    # converted to None the way a real JSON `null` would be. Normalize that
    # deterministically here rather than relying on DRF's HTML-form
    # empty-string convention, which only fires for genuine QueryDicts.
    _CLEARABLE_PROMOTION_FIELDS = ('multibuy_option', 'promotion_starts_at', 'promotion_ends_at')

    def to_internal_value(self, data):
        data = data.copy()
        for field in self._CLEARABLE_PROMOTION_FIELDS:
            if field in data:
                raw = data.get(field)
                if isinstance(raw, str) and raw.strip().lower() in ('', 'null', 'none'):
                    data[field] = None
        return super().to_internal_value(data)

    STOPPER_CONFLICT_LABELS = {
        'FAVORITE': 'Favorito del mes',
        'RECOMMENDED': 'Recomendado',
    }

    STOPPER_CONFLICT_MESSAGES = {
        'FAVORITE': (
            'Esta categoría ya tiene un Favorito del mes. Para mover este producto, '
            'primero quita esa etiqueta o cambia el favorito actual.'
        ),
        'RECOMMENDED': (
            'Esta categoría ya tiene un Recomendado. Para mover este producto, '
            'primero quita esa etiqueta o cambia el recomendado actual.'
        ),
    }

    def _stopper_conflict(self, stopper, category, exclude_pk=None):
        """Return the Product already holding `stopper` in `category`, or None.

        Shared by the proactive validate() check and the IntegrityError
        backstops in create()/update(): after a race loses to the DB
        constraint, re-querying (rather than trusting the caller's stopper
        value alone) confirms it really was a stopper conflict — not some
        unrelated IntegrityError — without relying on parsing the driver's
        error message, which differs between Postgres and SQLite.
        """
        label = self.STOPPER_CONFLICT_LABELS.get(stopper)
        if label is None or category is None:
            return None
        conflicting = Product.objects.filter(category=category, stopper=stopper)
        if exclude_pk is not None:
            conflicting = conflicting.exclude(pk=exclude_pk)
        return conflicting.first()

    def validate(self, attrs):
        stopper = attrs.get('stopper', getattr(self.instance, 'stopper', None))
        category = attrs.get('category', getattr(self.instance, 'category', None))

        existing = self._stopper_conflict(
            stopper, category, exclude_pk=self.instance.pk if self.instance is not None else None
        )
        if existing is not None:
            raise serializers.ValidationError({
                'error': self.STOPPER_CONFLICT_MESSAGES[stopper]
            })

        # Resolve against self.instance so a partial update that only sends
        # one of the two dates is still checked against the other's stored
        # value. Deliberately no "end must be in the future" rule — an
        # already-expired promotion must survive an unrelated-field save
        # (see products/promotions.py), so past end dates are valid.
        starts_at = attrs.get('promotion_starts_at', getattr(self.instance, 'promotion_starts_at', None))
        ends_at = attrs.get('promotion_ends_at', getattr(self.instance, 'promotion_ends_at', None))
        if starts_at and ends_at and starts_at >= ends_at:
            raise serializers.ValidationError({
                'promotion_ends_at': 'La fecha de fin debe ser posterior a la fecha de inicio.'
            })

        # A promotion window (dates) with no actual discount/multibuy type is
        # a no-op that misleads the merchant into thinking a promo is live.
        # Only checked when this request actually touches a promo field —
        # an unrelated-field save (e.g. price) must still be able to go
        # through untouched even for a legacy product saved before this rule
        # existed.
        promo_fields_touched = any(
            field in attrs
            for field in ('multibuy_option', 'discount_percentage', 'promotion_starts_at', 'promotion_ends_at')
        )
        if promo_fields_touched:
            multibuy_option = attrs.get('multibuy_option', getattr(self.instance, 'multibuy_option', None))
            discount_percentage = attrs.get('discount_percentage', getattr(self.instance, 'discount_percentage', None))
            has_type = bool(multibuy_option) or (discount_percentage is not None and discount_percentage > 0)
            has_dates = bool(starts_at) or bool(ends_at)
            if has_dates and not has_type:
                raise serializers.ValidationError({
                    'discount_percentage': 'Debes seleccionar Descuento u Oferta para activar la promoción.'
                })

        # media is the full desired gallery state (existing items kept/updated,
        # new items, and items flagged `_delete`); when omitted entirely
        # (e.g. a PATCH that doesn't touch the gallery) there's nothing to
        # check. Images and videos cap independently.
        media_data = attrs.get('media')
        if media_data is not None:
            remaining_media = [item for item in media_data if not item.get('_delete')]
            image_count = sum(
                1 for item in remaining_media
                if item.get('media_type', ProductMedia.IMAGE) == ProductMedia.IMAGE
            )
            video_count = sum(
                1 for item in remaining_media
                if item.get('media_type') == ProductMedia.VIDEO
            )
            if image_count > 3:
                raise serializers.ValidationError({
                    'media': 'No se pueden tener más de 3 imágenes en la galería.'
                })
            if video_count > 1:
                raise serializers.ValidationError({
                    'media': 'No se puede tener más de 1 video en la galería.'
                })

        return attrs

    @transaction.atomic
    def create(self, validated_data):
        variants_data = validated_data.pop('variants', [])
        addons_data = validated_data.pop('addons', [])
        media_data = validated_data.pop('media', [])

        try:
            # Nested atomic = savepoint: on IntegrityError, only this insert
            # rolls back, leaving the outer transaction usable so the
            # conflict re-check below can still query the DB.
            with transaction.atomic():
                product = Product.objects.create(**validated_data)
        except IntegrityError:
            stopper = validated_data.get('stopper')
            category = validated_data.get('category')
            label = self.STOPPER_CONFLICT_LABELS.get(stopper)
            if label is not None and self._stopper_conflict(stopper, category) is not None:
                raise serializers.ValidationError({
                    'error': self.STOPPER_CONFLICT_MESSAGES[stopper]
                })
            raise

        for variant_data in variants_data:
            if '_delete' in variant_data:
                variant_data.pop('_delete')
            ProductVariant.objects.create(product=product, **variant_data)

        for addon_data in addons_data:
            if '_delete' in addon_data:
                addon_data.pop('_delete')
            ProductAddon.objects.create(product=product, **addon_data)

        for media_item in media_data:
            if '_delete' in media_item:
                media_item.pop('_delete')
            ProductMedia.objects.create(product=product, **media_item)

        return product

    @transaction.atomic
    def update(self, instance, validated_data):
        variants_data = validated_data.pop('variants', [])
        addons_data = validated_data.pop('addons', [])
        media_data = validated_data.pop('media', [])

        try:
            with transaction.atomic():
                instance = super().update(instance, validated_data)
        except IntegrityError:
            stopper = validated_data.get('stopper', instance.stopper)
            category = validated_data.get('category', instance.category)
            label = self.STOPPER_CONFLICT_LABELS.get(stopper)
            if label is not None and self._stopper_conflict(stopper, category, exclude_pk=instance.pk) is not None:
                raise serializers.ValidationError({
                    'error': self.STOPPER_CONFLICT_MESSAGES[stopper]
                })
            raise

        # Helper to process related objects generically
        def process_related(model_class, existing_qs, items_data, create_fields_map=None):
            """
            model_class: ProductVariant, ProductMedia, etc.
            existing_qs: instance.variants.all()
            items_data: list of dicts from validated_data (or None)
            create_fields_map: optional function to map incoming dict -> create kwargs
            """
            if items_data is None:
                # If frontend didn't send anything for this relation, do nothing
                return

            keep_ids = []
            for item in items_data:
                item_id = item.get('id', None)
                should_delete = item.get('_delete', False)

                if item_id:
                    # Existing object: fetch and update or delete
                    try:
                        obj = existing_qs.get(id=item_id)
                    except model_class.DoesNotExist:
                        continue

                    if should_delete:
                        obj.delete()
                        continue

                    # Update allowed fields — avoid overwriting file field with None
                    update_data = {k: v for k, v in item.items() if k not in ('id', '_delete')}

                    # If file/image present, assign it
                    for k, v in update_data.items():
                        if v is not None:
                            setattr(obj, k, v)
                    obj.save()
                    keep_ids.append(obj.id)
                else:
                    # New item (no id): create
                    create_kwargs = {k: v for k, v in item.items() if k != '_delete'}
                    create_kwargs['product'] = instance
                    new_obj = model_class.objects.create(**create_kwargs)
                    keep_ids.append(new_obj.id)

            # Optionally: If you want strict behavior to remove any existing objects not in keep_ids:
            # existing_qs.exclude(id__in=keep_ids).delete()

        # Process each relation
        process_related(ProductVariant, instance.variants, variants_data)
        process_related(ProductAddon, instance.addons, addons_data)
        process_related(ProductMedia, instance.media, media_data)

        return instance


