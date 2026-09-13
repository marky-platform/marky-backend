import logging

from cities_light.models import City, Country
from drf_spectacular.utils import extend_schema, OpenApiParameter
from rest_framework import generics, filters
from rest_framework import mixins, viewsets
from rest_framework import status
from rest_framework.exceptions import NotFound
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.parsers import MultiPartParser, FormParser
from django.db import transaction, IntegrityError

logger = logging.getLogger(__name__)

from business.models import BusinessCategory, Currency, BusinessProfile, SocialMediaLink, BranchAttribute, Branch
from business.serializers import BusinessCategorySerializer, CurrencySerializer, CitySerializer, CountrySerializer, \
    BusinessProfileWriteSerializer, BusinessProfileListSerializer, BusinessProfileDetailSerializer, SocialMediaLinkSerializer, \
    SocialMediaLinksReplaceSerializer, SocialMediaLinksReplaceResponseSerializer, BusinessProfileHomePageSerializer, \
    BusinessProfileUpdateSerializer, BusinessProfileUpdateResponseSerializer, BranchAttributeSerializer, \
    BusinessProfileImageSerializer, BranchLocationSerializer, BranchLocationsReplaceSerializer, \
    BranchLocationsReplaceResponseSerializer
from rest_framework.permissions import AllowAny
from utils.permissions import IsBusinessOrSuperAdmin
from .models import BusinessProfile
from business.serializers import AccountInfoSerializer, AccountInfoUpdateSerializer


@extend_schema(tags=['Business'])
class BusinessProfileViewSet(mixins.CreateModelMixin,
                             mixins.RetrieveModelMixin,
                             mixins.UpdateModelMixin,
                             viewsets.GenericViewSet):
    permission_classes = [IsBusinessOrSuperAdmin]
    queryset = BusinessProfile.objects.all()
    serializer_class = BusinessProfileWriteSerializer

    def get_queryset(self):
        if self.request.user.is_superuser:
            return BusinessProfile.objects.all()
        return BusinessProfile.objects.filter(user=self.request.user)

    def get_serializer_class(self):
        if self.action == "list":
            return BusinessProfileListSerializer
        elif self.action == "retrieve":
            return BusinessProfileDetailSerializer
        elif self.action in ["create", "update", "partial_update"]:
            return BusinessProfileWriteSerializer
        return BusinessProfileWriteSerializer

    def perform_create(self, serializer):
        serializer.save(user=self.request.user)


@extend_schema(tags=['Business'])
class BranchAttributeListView(generics.ListAPIView):
    permission_classes = [IsBusinessOrSuperAdmin]
    queryset = BranchAttribute.objects.all()
    serializer_class = BranchAttributeSerializer
    filter_backends = [filters.SearchFilter]
    search_fields = ['name']


@extend_schema(tags=['Business'])
class BusinessCategoryListView(generics.ListAPIView):
    permission_classes = [IsBusinessOrSuperAdmin]
    queryset = BusinessCategory.objects.all()
    serializer_class = BusinessCategorySerializer
    filter_backends = [filters.SearchFilter]
    search_fields = ['name']


@extend_schema(tags=['Business'])
class CurrencyListView(generics.ListAPIView):
    permission_classes = [IsBusinessOrSuperAdmin]
    queryset = Currency.objects.all()
    serializer_class = CurrencySerializer


@extend_schema(tags=['Cities'], parameters=[
    OpenApiParameter('country_id', type=int, description='Country ID')
])
class CityListView(generics.ListAPIView):
    permission_classes = [IsBusinessOrSuperAdmin]
    queryset = City.objects.all()
    serializer_class = CitySerializer

    def get_queryset(self):
        country_id = self.request.query_params.get('country_id', None)
        ALLOWED_CITIES = ["Asunción", "Caracas"]

        if country_id:
            cities = City.objects.filter(country_id=country_id, name__in=ALLOWED_CITIES)

            if not cities.exists():
                raise NotFound('No se encontraron ciudades.')

            return cities
        else:
            return City.objects.all()


@extend_schema(tags=['Cities'])
class CountryListView(generics.ListAPIView):
    permission_classes = [IsBusinessOrSuperAdmin]
    queryset = Country.objects.all()
    serializer_class = CountrySerializer


class ValidateBusinessNameView(APIView):
    permission_classes = [AllowAny]

    def get(self, request, format=None):
        business_id = request.query_params.get("business_id", None)
        if not business_id:
            return Response(
                {"error": "No se encontró nombre a validar."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Check if a BusinessProfile with the given business_id (business name) exists.
        # Using iexact makes the check case-insensitive.
        exists = BusinessProfile.objects.filter(business_id__iexact=business_id).exists()
        return Response({"is_taken": exists}, status=status.HTTP_200_OK)


@extend_schema(tags=['Business'])
class SocialMediaLinkViewSet(mixins.RetrieveModelMixin,
                             mixins.ListModelMixin,
                             viewsets.GenericViewSet):
    """
    ViewSet for managing social media links for a business.
    Supports retrieving and listing social media links.
    Use the bulk update endpoint for creating/updating/deleting links.
    """
    permission_classes = [IsBusinessOrSuperAdmin]
    serializer_class = SocialMediaLinkSerializer
    
    def get_queryset(self):
        """
        Filter social media links by the business profile of the authenticated user.
        """
        try:
            business_profile = self.request.user.business_profile
            return SocialMediaLink.objects.filter(business=business_profile)
        except BusinessProfile.DoesNotExist:
            return SocialMediaLink.objects.none()


@extend_schema(
    tags=['Business'],
    summary="Replace social media / contact channels (Atomic)",
    description="""
    Replaces the full set of a business's channels (Instagram, Facebook,
    TikTok, WhatsApp, Enlaces) in one shot.

    **Atomic, full replacement:** the request must carry every channel the
    business wants to keep — anything not present is deleted. Up to 3
    channels may be selected; WhatsApp and Enlaces accept up to 3 named
    entries each, the rest exactly one.

    **Example request:**
    ```json
    {
        "channels": [
            {"platform": "instagram", "url": "https://www.instagram.com/example"},
            {"platform": "whatsapp", "label": "Pedidos", "url": "+595 981 234 567"},
            {"platform": "whatsapp", "label": "Atención al cliente", "url": "+595 982 345 678"},
            {"platform": "link", "label": "Cómo llegar", "url": "maps.google.com/..."}
        ]
    }
    ```
    """,
    request=SocialMediaLinksReplaceSerializer,
    responses={
        200: SocialMediaLinksReplaceResponseSerializer,
        400: {"description": "Bad request - invalid data or missing business profile"}
    }
)
class SocialMediaLinkBulkUpdateView(APIView):
    """
    Full-replacement endpoint for a business's social/contact channels.
    """
    permission_classes = [IsBusinessOrSuperAdmin]

    def post(self, request, *args, **kwargs):
        try:
            business_profile = request.user.business_profile
        except BusinessProfile.DoesNotExist:
            return Response(
                {"error": "No business profile found for this user."},
                status=status.HTTP_400_BAD_REQUEST
            )

        serializer = SocialMediaLinksReplaceSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        channels = serializer.validated_data['channels']

        order_by_platform = {}
        new_links = []
        for entry in channels:
            platform = entry['platform']
            order = order_by_platform.get(platform, 0)
            order_by_platform[platform] = order + 1
            new_links.append(SocialMediaLink(
                business=business_profile,
                platform=platform,
                label=entry['label'],
                url=entry['url'],
                order=order,
            ))

        try:
            with transaction.atomic():
                business_profile.social_links.all().delete()
                created_links = SocialMediaLink.objects.bulk_create(new_links)
        except IntegrityError:
            logger.exception(
                "Integrity error replacing social links for user %s", request.user.id
            )
            return Response(
                {"error": "Ha ocurrido un error inesperado."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        response_serializer = SocialMediaLinkSerializer(created_links, many=True)
        return Response(
            {"social_links": response_serializer.data},
            status=status.HTTP_200_OK,
        )


@extend_schema(
    tags=['Business'],
    summary="Replace a business's locations (Ubicaciones)",
    description="""
    Full-replacement endpoint for a business's named addresses/locations.

    Entries with an `id` update that existing location in place (preserving
    any attributes already attached to it); entries without an `id` are
    created; existing locations whose `id` is omitted from the payload are
    deleted. The first location in the submitted list is marked as the
    business's headquarter branch (used elsewhere for the "Atributos" row).
    """,
    request=BranchLocationsReplaceSerializer,
    responses={200: BranchLocationsReplaceResponseSerializer},
)
class BranchLocationsBulkUpdateView(APIView):
    """
    Full-replacement endpoint for a business's locations/addresses.
    """
    permission_classes = [IsBusinessOrSuperAdmin]

    def post(self, request, *args, **kwargs):
        try:
            business_profile = request.user.business_profile
        except BusinessProfile.DoesNotExist:
            return Response(
                {"error": "No business profile found for this user."},
                status=status.HTTP_400_BAD_REQUEST
            )

        serializer = BranchLocationsReplaceSerializer(
            data=request.data, context={"business": business_profile}
        )
        serializer.is_valid(raise_exception=True)
        locations = serializer.validated_data['locations']

        try:
            with transaction.atomic():
                submitted_ids = {entry['id'] for entry in locations if entry.get('id')}
                business_profile.branches.exclude(id__in=submitted_ids).delete()

                result_branches = []
                for index, entry in enumerate(locations):
                    is_headquarter = index == 0
                    if entry.get('id'):
                        branch = business_profile.branches.get(id=entry['id'])
                        branch.name = entry['name']
                        branch.address = entry['address']
                        branch.is_headquarter = is_headquarter
                        branch.save()
                    else:
                        branch = Branch.objects.create(
                            business=business_profile,
                            name=entry['name'],
                            address=entry['address'],
                            is_headquarter=is_headquarter,
                        )
                    result_branches.append(branch)
        except IntegrityError:
            logger.exception(
                "Integrity error replacing locations for user %s", request.user.id
            )
            return Response(
                {"error": "Ha ocurrido un error inesperado."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        response_serializer = BranchLocationSerializer(result_branches, many=True)
        return Response(
            {"locations": response_serializer.data},
            status=status.HTTP_200_OK,
        )


@extend_schema(
    tags=['Business'],
    summary="Get business profile home page data",
    description="""
    Get all necessary business profile information for the frontend home page.
    
    **Returns:**
    - Business name (from user model)
    - All social media links/channels
    - Business description
    - Business categories
    - Profile image
    - Attributes from the headquarter branch (is_headquarter=True)
    
    This endpoint provides all the data needed to display a business profile on the home page.
    """,
    responses={
        200: BusinessProfileHomePageSerializer,
        400: {"description": "Bad request - missing business profile"}
    }
)
class BusinessProfileHomePageView(APIView):
    """
    Get business profile data for home page display.
    """
    permission_classes = [IsBusinessOrSuperAdmin]
    
    def get(self, request, *args, **kwargs):
        """
        Get business profile home page data for the authenticated user.
        """
        try:
            business_profile = request.user.business_profile
        except BusinessProfile.DoesNotExist:
            return Response(
                {"error": "No business profile found for this user."},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Use select_related and prefetch_related for optimal database queries
        business_profile = BusinessProfile.objects.select_related('user').prefetch_related(
            'social_links',
            'categories',
            'branches__attributes'
        ).get(id=business_profile.id)

        context = {"request": request}
        serializer = BusinessProfileHomePageSerializer(business_profile, context=context)
        return Response(serializer.data, status=status.HTTP_200_OK)


@extend_schema(
    tags=['Business'],
    summary="Update business profile description and/or headquarter attributes",
    description="""
    Update business profile description and/or headquarter branch attributes.
    
    **Flexible Update:** You can update either field independently or both together.
    At least one field must be provided in the request.
    
    **Fields:**
    - `description`: Business profile description (optional)
    - `headquarter_attributes`: List of attribute IDs for the headquarter branch (optional)
    
    **Example requests:**
    
    Update only description:
    ```json
    {
        "description": "New business description"
    }
    ```
    
    Update only headquarter attributes:
    ```json
    {
        "headquarter_attributes": [1, 2, 3]
    }
    ```
    
    Update both:
    ```json
    {
        "description": "New business description",
        "headquarter_attributes": [1, 2, 3]
    }
    ```
    """,
    request=BusinessProfileUpdateSerializer,
    responses={
        200: BusinessProfileUpdateResponseSerializer,
        400: {"description": "Bad request - invalid data, validation error, or missing business profile"}
    }
)
class BusinessProfileUpdateView(APIView):
    """
    Update business profile description and/or headquarter attributes.
    """
    permission_classes = [IsBusinessOrSuperAdmin]
    
    def patch(self, request, *args, **kwargs):
        """
        Update business profile description and/or headquarter attributes.
        All operations are atomic - if any operation fails, all changes are rolled back.
        """
        try:
            business_profile = request.user.business_profile
        except BusinessProfile.DoesNotExist:
            return Response(
                {"error": "No business profile found for this user."},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Validate request data using serializer
        serializer = BusinessProfileUpdateSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        
        validated_data = serializer.validated_data
        updated_fields = []
        
        try:
            with transaction.atomic():
                # Update description if provided
                if 'description' in validated_data:
                    business_profile.description = validated_data['description']
                    business_profile.save()
                    updated_fields.append('description')
                
                # Update headquarter attributes if provided
                if 'headquarter_attributes' in validated_data:
                    # Get or create headquarter branch
                    headquarter_branch, created = business_profile.branches.get_or_create(
                        is_headquarter=True,
                        defaults={
                            'name': 'Casa Matriz',
                            'address': 'Dirección principal'
                        }
                    )
                    
                    # Update attributes
                    attribute_ids = validated_data['headquarter_attributes']
                    if attribute_ids:
                        # Set the new attributes
                        headquarter_branch.attributes.set(attribute_ids)
                    else:
                        # Clear all attributes if empty list provided
                        headquarter_branch.attributes.clear()
                    
                    updated_fields.append('headquarter_attributes')
                
                # Get updated business profile with all related data
                updated_business_profile = BusinessProfile.objects.select_related('user').prefetch_related(
                    'social_links',
                    'categories',
                    'branches__attributes'
                ).get(id=business_profile.id)
                
                # Prepare response
                response_data = {
                    "message": "Business profile updated successfully",
                    "updated_fields": updated_fields,
                    "business_profile": BusinessProfileHomePageSerializer(updated_business_profile).data
                }
                
                return Response(response_data, status=status.HTTP_200_OK)
                
        except Exception:
            logger.exception("Error updating business profile for user %s", request.user.id)
            return Response(
                {"error": "Ha ocurrido un error inesperado."},
                status=status.HTTP_400_BAD_REQUEST
            )


class BusinessProfileImageView(APIView):
    """
    Upload or update business profile image.
    """
    permission_classes = [IsBusinessOrSuperAdmin]
    parser_classes = (MultiPartParser, FormParser)
    
    @extend_schema(
        tags=['Business'],
        summary="Upload or update business profile image",
        description="""
        Upload or update the business profile image.
        
        **Request:**
        - `profile_image`: The image file to upload.
        
        **Validation:**
        - Max file size: 2MB
        - Allowed extensions: .jpg, .jpeg, .png, .gif
        """,
        request={
            'multipart/form-data': {
                'type': 'object',
                'properties': {
                    'profile_image': {
                        'type': 'string',
                        'format': 'binary'
                    }
                }
            }
        },
        responses={
            200: BusinessProfileImageSerializer,
            400: {"description": "Bad request - invalid data, validation error, or missing business profile"}
        }
    )
    def patch(self, request, *args, **kwargs):
        """
        Update business profile image.
        """
        try:
            business_profile = request.user.business_profile
        except BusinessProfile.DoesNotExist:
            return Response(
                {"error": "No business profile found for this user."},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        serializer = BusinessProfileImageSerializer(
            instance=business_profile,
            data=request.data,
            partial=True
        )
        
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data, status=status.HTTP_200_OK)
        
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


@extend_schema(
    tags=['Business'],
    summary='Get current business account info',
    description='Devuelve datos de configuración de cuenta del usuario autenticado (business).',
    responses={200: AccountInfoSerializer},
)
class AccountInfoView(APIView):
    permission_classes = [IsBusinessOrSuperAdmin]

    def get(self, request, *args, **kwargs):
        user = request.user

        # Base response with user fields
        data = {
            'business_name': getattr(user, 'business_name', None),
            'email': getattr(user, 'email', None),
            'phone_number': getattr(user, 'phone_number', None),

            'business_id': None,
            'business_type': None,
            'exchange_rate': None,

            'city_id': None,
            'city_name': None,
            'country_id': None,
            'country_name': None,

            'primary_currency_id': None,
            'primary_currency_name': None,
            'primary_currency_code': None,

            'secondary_currency_id': None,
            'secondary_currency_name': None,
            'secondary_currency_code': None,

            'is_primary_to_secondary': True,

            'categories': [],
        }

        try:
            bp = (
                BusinessProfile.objects
                .select_related('city__country', 'primary_currency', 'secondary_currency')
                .prefetch_related('categories')
                .get(user=user)
            )
        except BusinessProfile.DoesNotExist:
            bp = None

        if bp is not None:
            data.update({
                'business_id': bp.business_id,
                'business_type': bp.business_type,
                'exchange_rate': bp.exchange_rate,
            })

            if bp.city is not None:
                data.update({
                    'city_id': bp.city.id,
                    'city_name': bp.city.name,
                    'country_id': bp.city.country.id if bp.city.country else None,
                    'country_name': bp.city.country.name if bp.city.country else None,
                })

            if bp.primary_currency is not None:
                data.update({
                    'primary_currency_id': bp.primary_currency.id,
                    'primary_currency_name': bp.primary_currency.name,
                    'primary_currency_code': bp.primary_currency.code,
                })

            if bp.secondary_currency is not None:
                data.update({
                    'secondary_currency_id': bp.secondary_currency.id,
                    'secondary_currency_name': bp.secondary_currency.name,
                    'secondary_currency_code': bp.secondary_currency.code,
                })

            if bp.is_primary_to_secondary is not None:
                data.update({
                    'is_primary_to_secondary': bp.is_primary_to_secondary
                })

            # categories: use serializer to get {id, name} list
            data['categories'] = list(bp.categories.all())

        serializer = AccountInfoSerializer(data)
        return Response(serializer.data, status=status.HTTP_200_OK)

    @extend_schema(
        request=AccountInfoUpdateSerializer,
        responses={200: AccountInfoSerializer}
    )
    @transaction.atomic
    def patch(self, request, *args, **kwargs):
        """Partial update: updates User fields and related BusinessProfile for the authenticated user."""
        user = request.user
        try:
            bp = BusinessProfile.objects.select_for_update().get(user=user)
        except BusinessProfile.DoesNotExist:
            return Response({"error": "No business profile found for this user."}, status=status.HTTP_400_BAD_REQUEST)

        serializer = AccountInfoUpdateSerializer(instance=bp, data=request.data, partial=True, context={"user": user})
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        serializer.save()

        # Return the same representation as GET
        return self.get(request)
