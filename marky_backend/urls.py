"""
URL configuration for marky_backend project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/5.1/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.http import Http404
from django.urls import path, include
from marky_backend.health import healthz, readyz
from drf_spectacular.views import SpectacularJSONAPIView, SpectacularSwaggerView, SpectacularRedocView

def docs_gate(view):
    """Schema/Swagger/ReDoc: libres con DEBUG; en producción solo staff logueado (sesión de /admin/). Si no, 404."""
    def wrapped(request, *args, **kwargs):
        user = getattr(request, 'user', None)
        if not (settings.DEBUG or (user is not None and user.is_authenticated and user.is_staff)):
            raise Http404
        return view(request, *args, **kwargs)
    return wrapped


api_v1_patterns = [
    path('users/', include('users.urls')),
    path('business/analytics/', include('analytics.urls')),
    path('business/', include('business.urls')),
    path('products/', include('products.urls')),
    path('notifications/', include('notifications.urls')),
    path('public/', include('marky_backend.public_urls')),
]

urlpatterns = [
    path('healthz', healthz, name='healthz'),
    path('readyz', readyz, name='readyz'),
    path('admin/', admin.site.urls),
    path("api/v1/", include(api_v1_patterns)),
    # SWAGGER
    path('api/schema/', docs_gate(SpectacularJSONAPIView.as_view()), name='schema'),
    path('api/schema/swagger/', docs_gate(SpectacularSwaggerView.as_view(url_name='schema')), name='swagger-ui'),
    path('api/schema/redoc/', docs_gate(SpectacularRedocView.as_view(url_name='schema')), name='redoc'),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
