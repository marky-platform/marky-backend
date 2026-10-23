from django.urls import include, path

from business.public_views import PublicBusinessProfileView
from products.public_views import (
    PublicCatalogView,
    PublicCategoryListView,
    PublicProductBySlugView,
    PublicProductDetailView,
)

urlpatterns = [
    path('business/<str:business_id>/analytics/', include('analytics.public_urls')),
    path('business/<str:business_id>/', PublicBusinessProfileView.as_view(), name='public-business-profile'),
    path('business/<str:business_id>/catalog/', PublicCatalogView.as_view(), name='public-business-catalog'),
    path('business/<str:business_id>/categories/', PublicCategoryListView.as_view(), name='public-business-categories'),
    # Legacy numeric detail, kept during the canonical-URL rollout.
    path('business/<str:business_id>/products/<int:pk>/', PublicProductDetailView.as_view(), name='public-business-product-detail'),
    path(
        'business/<str:business_id>/categories/<slug:category_slug>/products/<slug:product_slug>/',
        PublicProductBySlugView.as_view(),
        name='public-business-product-by-slug',
    ),
]
