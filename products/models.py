from decimal import Decimal
from django.core.validators import MinValueValidator, MaxValueValidator
from django.db import models
from business.models import BusinessProfile

DISCOUNT_PERCENTAGE_VALIDATORS = [MinValueValidator(Decimal('0')), MaxValueValidator(Decimal('100'))]


class MultiBuyType(models.TextChoices):
    TWO_FOR_ONE = "2x1", "2x1"
    THREE_FOR_TWO = "3x2", "3x2"


class ProductCategory(models.Model):
    business = models.ForeignKey(BusinessProfile, on_delete=models.CASCADE, related_name='product_categories')
    name = models.CharField(max_length=255)
    icon = models.CharField(max_length=50)
    order = models.IntegerField(default=1)
    # promotion
    multibuy_option = models.CharField(
        max_length=10,
        choices=MultiBuyType.choices,
        null=True,
        blank=True,
        help_text="Oferta tipo 2x1, 3x2, etc."
    )
    discount_percentage = models.DecimalField(
        max_digits=5, decimal_places=2, default=0, validators=DISCOUNT_PERCENTAGE_VALIDATORS
    )
    promotion_starts_at = models.DateTimeField(null=True, blank=True)
    promotion_ends_at = models.DateTimeField(null=True, blank=True)
    is_available = models.BooleanField(default=True)

    def __str__(self):
        return self.name

    class Meta:
        verbose_name = "Product Category"
        verbose_name_plural = "Product Categories"
        ordering = ['order']


class Product(models.Model):
    STOPPER_CHOICES = [
        ('FAVORITE', 'Favorito del mes'),
        ('RECOMMENDED', 'Recomendado'),
    ]

    name = models.CharField(max_length=255)
    description = models.TextField()
    price = models.DecimalField(max_digits=10, decimal_places=2, validators=[MinValueValidator(Decimal('0'))])
    category = models.ForeignKey(ProductCategory, on_delete=models.CASCADE,
                                 related_name='products', null=True, blank=True)
    is_active = models.BooleanField(default=True) ## hidden or visible
    is_available = models.BooleanField(default=True)
    stopper = models.CharField(
        max_length=20,
        choices=STOPPER_CHOICES,
        null=True,
        blank=True,
        help_text="Tipo de stopper activo (opcional)"
    )
    # promotion
    multibuy_option = models.CharField(
        max_length=10,
        choices=MultiBuyType.choices,
        null=True,
        blank=True,
        help_text="Oferta tipo 2x1, 3x2, etc."
    )
    discount_percentage = models.DecimalField(
        max_digits=5, decimal_places=2, default=0, validators=DISCOUNT_PERCENTAGE_VALIDATORS
    )
    promotion_starts_at = models.DateTimeField(null=True, blank=True)
    promotion_ends_at = models.DateTimeField(null=True, blank=True)
    # optional informational metadata
    featured_ingredients = models.TextField(
        null=True, blank=True, help_text="Ingredientes destacados separados por coma (máx. 8)"
    )
    presentation = models.JSONField(
        null=True, blank=True, help_text="Presentación: cantidad, dimensiones y rendimiento (versionado)"
    )
    allergens = models.TextField(
        null=True, blank=True, help_text="Identificadores de alérgenos separados por coma"
    )
    celiac_info = models.JSONField(
        null=True, blank=True, help_text="Declaraciones SIN TACC; null = desactivado"
    )
    #
    business = models.ForeignKey(BusinessProfile, on_delete=models.CASCADE, related_name='products')

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['category'],
                condition=models.Q(stopper='FAVORITE'),
                name='unique_favorite_stopper_per_category',
            ),
            models.UniqueConstraint(
                fields=['category'],
                condition=models.Q(stopper='RECOMMENDED'),
                name='unique_recommended_stopper_per_category',
            ),
        ]

    def __str__(self):
        return self.name

    def has_stopper(self):
        return self.stopper is not None

    def get_primary_secondary_amounts(self, business_profile=None):
        """Return (primary_amount, secondary_amount, primary_currency, secondary_currency).

        - All amounts are Decimal instances (or None where not applicable).
        - If secondary cannot be computed (missing currency or rate), secondary_amount will be None.
        """
        from decimal import Decimal

        bp = business_profile or self.business
        if not bp or not getattr(bp, 'primary_currency', None):
            return None, None, None, None

        price = self.price if self.price is not None else Decimal('0')
        primary_amount = price
        secondary_amount = None

        sec_currency = getattr(bp, 'secondary_currency', None)
        rate = getattr(bp, 'exchange_rate', None)
        is_p2s = getattr(bp, 'is_primary_to_secondary', True)

        if sec_currency and rate is not None:
            try:
                # rate and price are Decimals (model fields), arithmetic kept in Decimal
                if is_p2s:
                    secondary_amount = price * rate
                else:
                    # interpret rate as "1 secondary = rate primary" => 1 primary = 1/r secondary
                    if rate == 0:
                        secondary_amount = None
                    else:
                        secondary_amount = price / rate
            except Exception:
                secondary_amount = None

        return primary_amount, secondary_amount, bp.primary_currency, sec_currency


class ProductVariant(models.Model):
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name='variants')
    name = models.CharField(max_length=255)
    price = models.DecimalField(max_digits=10, decimal_places=2, validators=[MinValueValidator(Decimal('0'))])
    description = models.TextField(blank=True, null=True)
    image = models.ImageField(upload_to='product_variants/', blank=True, null=True)

    def get_primary_secondary_amounts(self, business_profile=None):
        """Return (primary_amount, secondary_amount, primary_currency, secondary_currency)

        Mirrors Product.get_primary_secondary_amounts but uses the variant's own price
        and derives the business profile from the parent product if not provided.
        """
        from decimal import Decimal

        bp = business_profile or getattr(self.product, 'business', None)
        if not bp or not getattr(bp, 'primary_currency', None):
            return None, None, None, None

        price = self.price if self.price is not None else Decimal('0')
        primary_amount = price
        secondary_amount = None

        sec_currency = getattr(bp, 'secondary_currency', None)
        rate = getattr(bp, 'exchange_rate', None)
        is_p2s = getattr(bp, 'is_primary_to_secondary', True)

        if sec_currency and rate is not None:
            try:
                if is_p2s:
                    secondary_amount = price * rate
                else:
                    if rate == 0:
                        secondary_amount = None
                    else:
                        secondary_amount = price / rate
            except Exception:
                secondary_amount = None

        return primary_amount, secondary_amount, bp.primary_currency, sec_currency

    def __str__(self):
        return f'{self.product.name} - {self.name}'


class ProductAddon(models.Model):
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name='addons')
    name = models.CharField(max_length=255)
    price = models.DecimalField(max_digits=10, decimal_places=2, validators=[MinValueValidator(Decimal('0'))])

    def __str__(self):
        return f'{self.product.name} - {self.name}'

    def get_primary_secondary_amounts(self, business_profile=None):
        """Return (primary_amount, secondary_amount, primary_currency, secondary_currency)

        Mirrors Product.get_primary_secondary_amounts but uses the addon's own price
        and derives the business profile from the parent product if not provided.
        """
        from decimal import Decimal

        bp = business_profile or getattr(self.product, 'business', None)
        if not bp or not getattr(bp, 'primary_currency', None):
            return None, None, None, None

        price = self.price if self.price is not None else Decimal('0')
        primary_amount = price
        secondary_amount = None

        sec_currency = getattr(bp, 'secondary_currency', None)
        rate = getattr(bp, 'exchange_rate', None)
        is_p2s = getattr(bp, 'is_primary_to_secondary', True)

        if sec_currency and rate is not None:
            try:
                if is_p2s:
                    secondary_amount = price * rate
                else:
                    if rate == 0:
                        secondary_amount = None
                    else:
                        secondary_amount = price / rate
            except Exception:
                secondary_amount = None

        return primary_amount, secondary_amount, bp.primary_currency, sec_currency


class ProductMedia(models.Model):
    IMAGE = 'image'
    VIDEO = 'video'

    MEDIA_TYPE_CHOICES = [
        (IMAGE, 'Image'),
        (VIDEO, 'Video'),
    ]

    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name='media')
    file = models.FileField(upload_to='product_media/')
    media_type = models.CharField(max_length=10, choices=MEDIA_TYPE_CHOICES, default=IMAGE)
    order = models.IntegerField(null=True, blank=True)

    def __str__(self):
        return f'{self.product.name} - {self.media_type}'

    class Meta:
        ordering = ['media_type', 'order']
