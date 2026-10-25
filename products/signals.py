from django.db import transaction
from django.db.models.signals import post_delete
from django.dispatch import receiver

from .models import Product3DModel


def delete_stored_files_on_commit(storage, names):
    """Delete files from `storage` once the surrounding transaction commits,
    so a rolled-back request never loses files that are still referenced."""
    names = [name for name in names if name]
    if not names:
        return

    def _delete():
        for name in names:
            storage.delete(name)

    transaction.on_commit(_delete)


@receiver(post_delete, sender=Product3DModel)
def delete_model_3d_files(sender, instance, **kwargs):
    # Covers both an explicit delete and the cascade from deleting a product.
    delete_stored_files_on_commit(
        instance.glb_file.storage,
        [instance.glb_file.name, instance.usdz_file.name if instance.usdz_file else None],
    )
