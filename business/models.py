import os

from django.db import models
from django.contrib.auth import get_user_model
from cities_light.models import City
from django.contrib.gis.db import models as gis_models

User = get_user_model()


def business_profile_image_path(instance, filename):
    return os.path.join(
        'business_profiles',
        f'business-{instance.pk}',
        'profile_img',
        filename
    )


class BusinessCategory(models.Model):
    name = models.CharField(max_length=255, unique=True)

    class Meta:
        verbose_name = 'Business Category'
        verbose_name_plural = 'Business Categories'

    def __str__(self):
        return self.name


class Currency(models.Model):
    name = models.CharField(max_length=100)
    code = models.CharField(max_length=10, unique=True)  # e.g. "USD", "PYG", "VES"

    class Meta:
        verbose_name = 'Currency'
        verbose_name_plural = 'Currencies'

    def __str__(self):
        return f"{self.name} ({self.code})"


class BusinessProfile(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='business_profile')
    business_id = models.SlugField(max_length=100, unique=True)
    categories = models.ManyToManyField(BusinessCategory, related_name='business_categories')
    city = models.ForeignKey(City, on_delete=models.SET_NULL, null=True, blank=True)

    primary_currency = models.ForeignKey(Currency, on_delete=models.PROTECT, related_name='primary_businesses')
    secondary_currency = models.ForeignKey(Currency, on_delete=models.SET_NULL,
                                           null=True, blank=True, related_name='secondary_businesses')

    exchange_rate = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    # Indicates the direction of the exchange:
    # True: exchange_rate is interpreted as "1 primary currency unit equals exchange_rate secondary currency units"
    # False: exchange_rate is interpreted as "1 secondary currency unit equals exchange_rate primary currency units"
    is_primary_to_secondary = models.BooleanField(default=True)

    BUSINESS_TYPE_CHOICES = (
        ('commercial', 'Comercial'),
        ('entrepreneur', 'Emprendedor'),
    )
    business_type = models.CharField(
        max_length=20,
        choices=BUSINESS_TYPE_CHOICES,
        default='commercial',
        help_text="Tipo de negocio"
    )

    management_methods = models.JSONField(blank=True, null=True)  # medios_gestion_cuenta
    display_methods = models.JSONField(blank=True, null=True)  # medios_mostrar_cuenta

    profile_image = models.ImageField(
        upload_to=business_profile_image_path,
        null=True,
        blank=True,
        default='business_profiles/default.png',
        verbose_name='Imagen de perfil'
    )
    description = models.TextField(blank=True, null=True)

    class Meta:
        verbose_name = 'Business Profile'
        verbose_name_plural = 'Business Profiles'

    def __str__(self):
        return f"{self.business_id} - {self.user}"


class SocialMediaLink(models.Model):
    """
    Social media links for the Business profiles.
    """
    PLATFORM_CHOICES = [
        ('instagram', 'Instagram'),
        ('facebook', 'Facebook'),
        ('tiktok', 'TikTok'),
        ('whatsapp', 'WhatsApp'),
        ('link', 'Enlaces'),
    ]
    # Canales que admiten varios destinos, cada uno con nombre identificador.
    MULTI_ENTRY_PLATFORMS = {'whatsapp', 'link'}
    MAX_ENTRIES_PER_PLATFORM = 3
    MAX_SELECTED_PLATFORMS = 3
    LABEL_MAX_LENGTH = 22  # coincide con el contador x/22 del diseño

    business = models.ForeignKey(
        BusinessProfile,
        related_name='social_links',
        on_delete=models.CASCADE
    )
    platform = models.CharField(
        max_length=20,
        choices=PLATFORM_CHOICES
    )
    label = models.CharField(max_length=LABEL_MAX_LENGTH, blank=True)
    url = models.CharField(max_length=255,)
    order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ['platform', 'order', 'id']
        constraints = [
            models.UniqueConstraint(
                fields=['business', 'platform', 'order'],
                name='uniq_social_link_business_platform_order',
            ),
            # Defensa a nivel de DB del tope de 1 entrada para los canales
            # que no están en MULTI_ENTRY_PLATFORMS, para cualquier código
            # que cree filas fuera de SocialMediaLinksReplaceSerializer.
            models.UniqueConstraint(
                fields=['business', 'platform'],
                condition=~models.Q(platform__in=('whatsapp', 'link')),
                name='uniq_social_link_business_platform_single_entry',
            ),
        ]

    def __str__(self):
        return f"{self.business.business_id} – {self.get_platform_display()}"


class BranchAttribute(models.Model):
    name = models.CharField(max_length=50, unique=True)

    def __str__(self):
        return self.name

    class Meta:
        verbose_name = "Atributo de Sucursal"
        verbose_name_plural = "Atributos de Sucursal"


class BranchBenefit(models.Model):
    name = models.CharField(max_length=50, unique=True)

    def __str__(self):
        return self.name

    class Meta:
        verbose_name = "Beneficio de Sucursal"
        verbose_name_plural = "Beneficios de Sucursal"


class Branch(models.Model):
    """
    Model to handle Business Branches (Sucursales)
    """
    # Tope de direcciones/ubicaciones editables desde el modal "Editar perfil".
    MAX_LOCATIONS = 10

    business = models.ForeignKey(BusinessProfile, related_name='branches', on_delete=models.CASCADE)
    name = models.CharField(max_length=100)
    address = models.CharField(max_length=255)

    # en lugar de latitude + longitude:
    location = gis_models.PointField(
        srid=4326,         # Sistema WGS84
        geography=True,    # para distancias reales en metros
        null=True,
        blank=True
    )

    schedule = models.JSONField(default=dict, blank=True)
    attributes = models.ManyToManyField(BranchAttribute, blank=True, related_name='branches')
    benefits = models.ManyToManyField(BranchBenefit,  blank=True, related_name='branches')

    is_headquarter = models.BooleanField(
        default=False,
        verbose_name="Casa Matriz",
        help_text="Marca si esta sucursal es la casa matriz"
    )

    class Meta:
        ordering = ['business', 'name']

    def __str__(self):
        return f"{self.business.business_id} – {self.name}"
