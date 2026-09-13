import re

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers
from business.models import BusinessCategory, Currency, BusinessProfile, SocialMediaLink, BranchAttribute, Branch
from .validators import validate_image_size, validate_image_extension
from cities_light.models import City, Country
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.validators import URLValidator

User = get_user_model()


class BusinessProfileWriteSerializer(serializers.ModelSerializer):
    categories = serializers.PrimaryKeyRelatedField(queryset=BusinessCategory.objects.all(), many=True, required=False)
    city = serializers.PrimaryKeyRelatedField(queryset=City.objects.all(), required=False)
    primary_currency = serializers.PrimaryKeyRelatedField(queryset=Currency.objects.all())
    secondary_currency = serializers.PrimaryKeyRelatedField(queryset=Currency.objects.all(), required=False)
    is_primary_to_secondary = serializers.BooleanField(required=True)

    class Meta:
        model = BusinessProfile
        fields = [
            'business_id', 'categories', 'city', 'business_type',
            'primary_currency', 'secondary_currency', 'exchange_rate',
            'is_primary_to_secondary',
        ]


class BusinessProfileListSerializer(serializers.ModelSerializer):
    class Meta:
        model = BusinessProfile
        fields = ['id', 'user', 'business_id']
        read_only_fields = fields


class BusinessProfileDetailSerializer(serializers.ModelSerializer):
    class Meta:
        model = BusinessProfile
        fields = ['user', 'business_id', 'categories', 'city',
                  'primary_currency', 'secondary_currency', 'exchange_rate', ]
        read_only_fields = fields


class BusinessCategorySerializer(serializers.ModelSerializer):
    class Meta:
        model = BusinessCategory
        fields = ['id', 'name']


class CurrencySerializer(serializers.ModelSerializer):
    class Meta:
        model = Currency
        fields = ['id', 'name', 'code']


class CitySerializer(serializers.ModelSerializer):
    class Meta:
        model = City
        fields = ['id', 'name', 'country']


class CountrySerializer(serializers.ModelSerializer):
    class Meta:
        model = Country
        fields = ['id', 'name']


class SocialMediaLinkSerializer(serializers.ModelSerializer):
    platform_display = serializers.CharField(source='get_platform_display', read_only=True)

    class Meta:
        model = SocialMediaLink
        fields = ['id', 'platform', 'platform_display', 'label', 'url', 'order']
        read_only_fields = fields


class SocialMediaLinkEntrySerializer(serializers.Serializer):
    """
    One channel destination in a SocialMediaLinksReplaceSerializer payload.
    """
    platform = serializers.ChoiceField(choices=SocialMediaLink.PLATFORM_CHOICES)
    label = serializers.CharField(
        max_length=SocialMediaLink.LABEL_MAX_LENGTH, required=False, allow_blank=True
    )
    url = serializers.CharField(max_length=255)

    def validate_url(self, value):
        if not value.strip():
            raise serializers.ValidationError('El campo no puede estar vacío.')
        return value


class SocialMediaLinksReplaceResponseSerializer(serializers.Serializer):
    """
    Response envelope for the full-replacement channels endpoint.
    """
    social_links = SocialMediaLinkSerializer(many=True, read_only=True)


class SocialMediaLinksReplaceSerializer(serializers.Serializer):
    """
    Replaces the full set of a business's social/contact channels in one shot.
    Validates the whole set together (max channels selected, max entries per
    channel, required label for multi-entry channels, per-platform URL
    normalization/validation, no duplicate URLs within a channel).
    """
    channels = SocialMediaLinkEntrySerializer(many=True, allow_empty=True)

    def _validate_whatsapp(self, url):
        digits = re.sub(r'\D', '', url)
        if len(digits) < 11:
            raise serializers.ValidationError(
                'El número de WhatsApp debe incluir el código de país.'
            )
        return digits

    def _validate_url(self, url):
        # instagram/facebook/tiktok/link todos llegan como URL completa (el
        # frontend antepone el prefijo de cada red antes de enviar).
        normalized = url if re.match(r'^https?://', url, re.IGNORECASE) else f'https://{url}'
        validator = URLValidator()
        try:
            validator(normalized)
        except DjangoValidationError:
            raise serializers.ValidationError('Ingresa una URL válida.')
        return normalized

    def validate(self, data):
        channels = data['channels']

        selected_platforms = {entry['platform'] for entry in channels}
        if len(selected_platforms) > SocialMediaLink.MAX_SELECTED_PLATFORMS:
            raise serializers.ValidationError(
                f'Puedes seleccionar hasta {SocialMediaLink.MAX_SELECTED_PLATFORMS} canales.'
            )

        entries_by_platform: dict = {}
        for entry in channels:
            entries_by_platform.setdefault(entry['platform'], []).append(entry)

        normalized_channels = []
        for platform, entries in entries_by_platform.items():
            is_multi = platform in SocialMediaLink.MULTI_ENTRY_PLATFORMS
            max_entries = (
                SocialMediaLink.MAX_ENTRIES_PER_PLATFORM if is_multi else 1
            )
            if len(entries) > max_entries:
                raise serializers.ValidationError(
                    f'"{platform}" admite como máximo {max_entries} entrada(s).'
                )

            seen_urls = set()
            for entry in entries:
                label = entry.get('label', '').strip()
                url = entry['url'].strip()

                if is_multi and not label:
                    raise serializers.ValidationError(
                        {'label': f'El nombre es obligatorio para "{platform}".'}
                    )
                if not is_multi:
                    label = ''

                if platform == 'whatsapp':
                    url = self._validate_whatsapp(url)
                else:
                    url = self._validate_url(url)

                if url in seen_urls:
                    raise serializers.ValidationError(
                        f'Hay una entrada duplicada en "{platform}".'
                    )
                seen_urls.add(url)

                normalized_channels.append(
                    {'platform': platform, 'label': label, 'url': url}
                )

        data['channels'] = normalized_channels
        return data


class BranchAttributeSerializer(serializers.ModelSerializer):
    class Meta:
        model = BranchAttribute
        fields = ['id', 'name']


class BranchLocationSerializer(serializers.ModelSerializer):
    """
    Read-only view of a Branch as a simple named address, for the
    "Ubicaciones" row of the "Editar perfil" modal.
    """
    class Meta:
        model = Branch
        fields = ['id', 'name', 'address']
        read_only_fields = fields


class BranchLocationEntrySerializer(serializers.Serializer):
    """
    One location entry in a BranchLocationsReplaceSerializer payload.
    `id` identifies an existing Branch to update in place; omit it to
    create a new one.
    """
    id = serializers.IntegerField(required=False, allow_null=True)
    name = serializers.CharField(max_length=100)
    address = serializers.CharField(max_length=255)

    def validate_name(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError('El nombre no puede estar vacío.')
        return value

    def validate_address(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError('La dirección no puede estar vacía.')
        return value


class BranchLocationsReplaceResponseSerializer(serializers.Serializer):
    """
    Response envelope for the full-replacement locations endpoint.
    """
    locations = BranchLocationSerializer(many=True, read_only=True)


class BranchLocationsReplaceSerializer(serializers.Serializer):
    """
    Replaces the full set of a business's locations/addresses in one shot.
    Existing branches are matched by `id` (and preserved, keeping any
    attributes already attached); omitted `id`s are deleted, and entries
    without an `id` are created. `business` is supplied via context, not
    the payload, so `id`s can be validated as belonging to this business.
    """
    locations = BranchLocationEntrySerializer(many=True, allow_empty=True)

    def validate_locations(self, value):
        if len(value) > Branch.MAX_LOCATIONS:
            raise serializers.ValidationError(
                f'Puedes agregar como máximo {Branch.MAX_LOCATIONS} ubicaciones.'
            )
        business = self.context['business']
        submitted_ids = {entry['id'] for entry in value if entry.get('id')}
        if submitted_ids:
            existing_ids = set(
                business.branches.filter(id__in=submitted_ids).values_list('id', flat=True)
            )
            invalid_ids = submitted_ids - existing_ids
            if invalid_ids:
                raise serializers.ValidationError(
                    f'No se encontraron las ubicaciones: {sorted(invalid_ids)}'
                )
        return value


class BusinessProfileHomePageSerializer(serializers.ModelSerializer):
    """
    Serializer for business profile home page data.
    Returns all necessary information for the frontend home page.
    """
    business_name = serializers.CharField(source='user.business_name', read_only=True)
    social_links = SocialMediaLinkSerializer(many=True, read_only=True)
    categories = BusinessCategorySerializer(many=True, read_only=True)
    headquarter_attributes = serializers.SerializerMethodField()
    profile_image = serializers.SerializerMethodField()
    locations = BranchLocationSerializer(source='branches', many=True, read_only=True)

    class Meta:
        model = BusinessProfile
        fields = [
            'business_name',
            'social_links',
            'description',
            'categories',
            'profile_image',
            'headquarter_attributes',
            'locations',
        ]

    def get_headquarter_attributes(self, obj):
        """
        Get attributes from the headquarter branch (is_headquarter=True).
        """
        try:
            headquarter_branch = obj.branches.filter(is_headquarter=True).first()
            if headquarter_branch:
                return BranchAttributeSerializer(headquarter_branch.attributes.all(), many=True).data
            return []
        except Exception:
            return []

    def get_profile_image(self, obj):
        request = self.context.get('request')
        profile_image = obj.profile_image
        if profile_image and request:
            return request.build_absolute_uri(profile_image.url)
        elif profile_image:
            return profile_image.url
        return None


class BusinessProfileUpdateSerializer(serializers.Serializer):
    """
    Serializer for updating business profile description and/or headquarter attributes.
    At least one field must be provided.
    """
    description = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=100,
        help_text="Business profile description"
    )
    headquarter_attributes = serializers.ListField(
        child=serializers.IntegerField(),
        required=False,
        allow_empty=True,
        help_text="List of attribute IDs for the headquarter branch"
    )
    
    def validate(self, data):
        """
        Validate that at least one field is provided.
        """
        if not data.get('description') and 'headquarter_attributes' not in data:
            raise serializers.ValidationError(
                "At least one field must be provided: 'description' or 'headquarter_attributes'"
            )
        
        # Validate that all attribute IDs exist
        if 'headquarter_attributes' in data:
            attribute_ids = data['headquarter_attributes']
            if attribute_ids:  # Only validate if not empty
                existing_ids = set(BranchAttribute.objects.filter(
                    id__in=attribute_ids
                ).values_list('id', flat=True))
                invalid_ids = set(attribute_ids) - existing_ids
                if invalid_ids:
                    raise serializers.ValidationError({
                        'headquarter_attributes': f'Invalid attribute IDs: {list(invalid_ids)}'
                    })
        
        return data


class BusinessProfileUpdateResponseSerializer(serializers.Serializer):
    """
    Serializer for the business profile update response.
    """
    message = serializers.CharField(read_only=True, help_text="Success message")
    updated_fields = serializers.ListField(
        child=serializers.CharField(),
        read_only=True,
        help_text="List of fields that were updated"
    )
    business_profile = BusinessProfileHomePageSerializer(read_only=True, help_text="Updated business profile data")


class BusinessProfileImageSerializer(serializers.ModelSerializer):
    profile_image_display = serializers.ImageField(source='profile_image', read_only=True)
    profile_image = serializers.ImageField(
        validators=[validate_image_size, validate_image_extension],
        write_only=True
    )

    class Meta:
        model = BusinessProfile
        fields = ['profile_image', 'profile_image_display']


class AccountInfoSerializer(serializers.Serializer):
    # User fields
    business_name = serializers.CharField(allow_null=True)
    email = serializers.EmailField(allow_null=True)
    phone_number = serializers.CharField(allow_null=True, allow_blank=True)

    # BusinessProfile flat fields
    business_id = serializers.CharField(allow_null=True)
    business_type = serializers.CharField(allow_null=True)
    exchange_rate = serializers.DecimalField(max_digits=10, decimal_places=2, allow_null=True)

    # City + Country flattened
    city_id = serializers.IntegerField(allow_null=True)
    city_name = serializers.CharField(allow_null=True)
    country_id = serializers.IntegerField(allow_null=True)
    country_name = serializers.CharField(allow_null=True)

    # Primary currency
    primary_currency_id = serializers.IntegerField(allow_null=True)
    primary_currency_name = serializers.CharField(allow_null=True)
    primary_currency_code = serializers.CharField(allow_null=True)

    # Secondary currency
    secondary_currency_id = serializers.IntegerField(allow_null=True)
    secondary_currency_name = serializers.CharField(allow_null=True)
    secondary_currency_code = serializers.CharField(allow_null=True)

    is_primary_to_secondary = serializers.BooleanField(allow_null=True)

    # Categories list
    categories = BusinessCategorySerializer(many=True, read_only=True)

    def to_representation(self, instance):
        # If instance is a dict (as used in views), use default
        if isinstance(instance, dict):
            return super().to_representation(instance)

        # If instance is a User or BusinessProfile, build same dict as view expects
        return super().to_representation(instance)


class AccountInfoUpdateSerializer(serializers.Serializer):
    # User fields
    business_name = serializers.CharField(required=False)
    email = serializers.EmailField(required=False)
    phone_number = serializers.CharField(required=False, allow_blank=True)

    # BusinessProfile flat fields
    business_id = serializers.CharField(required=False)
    business_type = serializers.ChoiceField(
        choices=BusinessProfile.BUSINESS_TYPE_CHOICES,
        required=False
    )
    exchange_rate = serializers.DecimalField(max_digits=10, decimal_places=2, required=False)
    is_primary_to_secondary = serializers.BooleanField(required=False)

    # City + Currencies as PK related fields (DRF validates existence)
    # `source` is redundant when it matches the field name and causes an assertion
    # in recent DRF versions when generating schema. Remove it to avoid errors
    # while keeping the same validation behaviour.
    city = serializers.PrimaryKeyRelatedField(queryset=City.objects.all(), required=False)
    primary_currency = serializers.PrimaryKeyRelatedField(queryset=Currency.objects.all(), required=False)
    secondary_currency = serializers.PrimaryKeyRelatedField(queryset=Currency.objects.all(), required=False, allow_null=True)

    # Categories: list of IDs
    categories = serializers.PrimaryKeyRelatedField(queryset=BusinessCategory.objects.all(), many=True, required=False)

    def validate_business_id(self, value):
        if not re.match(r'^[a-z0-9\-_]+$', value):
            raise serializers.ValidationError(
                'Solo se permiten letras minúsculas, números, guiones (-) y guiones bajos (_).'
            )
        if len(value) < 4 or len(value) > 24:
            raise serializers.ValidationError(
                'Debe tener entre 4 y 24 caracteres.'
            )
        existing = BusinessProfile.objects.filter(business_id__iexact=value)
        if self.instance is not None:
            existing = existing.exclude(pk=self.instance.pk)
        if existing.exists():
            raise serializers.ValidationError('Este nombre de usuario ya existe.')
        return value

    def update(self, instance: BusinessProfile, validated_data):
        """Update BusinessProfile instance and related User fields.

        `instance` is the BusinessProfile for request.user. User is provided via context['user'].
        """
        user = self.context.get('user')

        # Update user fields
        business_name = validated_data.pop('business_name', None)
        email = validated_data.pop('email', None)
        phone_number = validated_data.pop('phone_number', None)

        user_changed = False
        if business_name is not None:
            user.business_name = business_name
            user_changed = True
        if email is not None:
            user.email = email
            user_changed = True
        if phone_number is not None:
            user.phone_number = phone_number
            user_changed = True
        if user_changed:
            user.save()

        # Handle categories separately
        categories = validated_data.pop('categories', None)

        # Set remaining fields on BusinessProfile
        for attr, value in validated_data.items():
            setattr(instance, attr, value)

        instance.save()

        if categories is not None:
            instance.categories.set(categories)

        return instance
