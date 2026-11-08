from utils.storage_cleanup import connect_file_cleanup, delete_stored_files_on_commit  # noqa: F401

from .models import Product3DModel, ProductMedia, ProductVariant

# Borrado explícito, cascada (producto/categoría/negocio) y reemplazo de archivos (F-18).
connect_file_cleanup(Product3DModel, ProductMedia, ProductVariant)
