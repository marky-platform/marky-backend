from django.apps import AppConfig


class BusinessConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'business'

    def ready(self):
        from utils.storage_cleanup import connect_file_cleanup
        from .models import BusinessProfile

        # Limpia la imagen de perfil al reemplazarla o borrar el negocio (F-18).
        connect_file_cleanup(BusinessProfile)
