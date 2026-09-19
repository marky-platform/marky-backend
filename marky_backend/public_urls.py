from django.urls import path

from business.public_views import PublicBusinessProfileView
from products.public_views import PublicCatalogView, PublicCategoryListView, PublicProductDetailView

urlpatterns = [
    path('business/<str:business_id>/', PublicBusinessProfileView.as_view(), name='public-business-profile'),
    path('business/<str:business_id>/catalog/', PublicCatalogView.as_view(), name='public-business-catalog'),
    path('business/<str:business_id>/categories/', PublicCategoryListView.as_view(), name='public-business-categories'),
    path('business/<str:business_id>/products/<int:pk>/', PublicProductDetailView.as_view(), name='public-business-product-detail'),
]
