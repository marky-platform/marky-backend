from django.urls import path, include
from business.views import BusinessCategoryListView, CurrencyListView, CityListView, CountryListView, \
    BusinessProfileViewSet, ValidateBusinessNameView, SocialMediaLinkViewSet, SocialMediaLinkBulkUpdateView, \
    BusinessProfileHomePageView, BusinessProfileUpdateView, BranchAttributeListView, BusinessProfileImageView, \
    BranchLocationsBulkUpdateView
from business.views import AccountInfoView
from rest_framework.routers import DefaultRouter


router = DefaultRouter()
router.register(r'business_profile', BusinessProfileViewSet)
router.register(r'social_media_links', SocialMediaLinkViewSet, basename='social_media_links')

urlpatterns = [
    path('', include(router.urls)),
    path('branch_attributes/', BranchAttributeListView.as_view(), name='branch-attribute-list'),
    path('categories/', BusinessCategoryListView.as_view(), name='business-category-list'),
    path('currencies/', CurrencyListView.as_view(), name='currency-list'),
    path('cities/', CityListView.as_view(), name='city-list'),
    path('countries/', CountryListView.as_view(), name='country-list'),
    path('validate-name/', ValidateBusinessNameView.as_view(), name='validate-business-name'),
    path('social-media-links/bulk-update/', SocialMediaLinkBulkUpdateView.as_view(), name='social-media-links-bulk-update'),
    path('locations/bulk-update/', BranchLocationsBulkUpdateView.as_view(), name='locations-bulk-update'),
    path('home-page/', BusinessProfileHomePageView.as_view(), name='business-profile-home-page'),
    path('update/', BusinessProfileUpdateView.as_view(), name='business-profile-update'),
    path('profile-image/', BusinessProfileImageView.as_view(), name='business-profile-image'),
    path('account-info/', AccountInfoView.as_view(), name='business-account-info'),
]
