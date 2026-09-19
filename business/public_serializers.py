from rest_framework import serializers

from .serializers import BusinessProfileHomePageSerializer


class PublicBusinessProfileSerializer(BusinessProfileHomePageSerializer):
    """
    Public, unauthenticated-safe view of a business profile (`marky.one/<slug>`).

    Subclasses BusinessProfileHomePageSerializer, which already exposes only
    public-safe fields, and adds `business_id` (the slug itself, needed by the
    frontend to build product-detail links). Does NOT reuse
    BusinessProfileDetailSerializer (leaks `user` PK, `exchange_rate`) or
    AccountInfoSerializer (leaks `email`, `phone_number`).
    """
    business_id = serializers.CharField(read_only=True)

    class Meta(BusinessProfileHomePageSerializer.Meta):
        fields = BusinessProfileHomePageSerializer.Meta.fields + ['business_id']
