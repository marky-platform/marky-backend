"""Limpieza de archivos huérfanos al borrar o reemplazar objetos (F-18)."""
import logging

from django.apps import apps
from django.db import transaction
from django.db.models import FileField
from django.db.models.signals import post_delete, post_save, pre_save

logger = logging.getLogger(__name__)


def _is_referenced(name):
    """True si algún FileField de la app todavía apunta a `name`."""
    for model in apps.get_models():
        for field in model._meta.get_fields():
            if isinstance(field, FileField) and model._default_manager.filter(**{field.name: name}).exists():
                return True
    return False


def delete_stored_files_on_commit(storage, names):
    """Borra archivos de `storage` cuando la transacción hace commit.

    Un rollback no pierde archivos que siguen referenciados. Los errores se
    registran pero nunca se propagan (un fallo de R2 no debe dar un 500 tras
    el commit), y no se borra un archivo que otra fila sigue usando.
    """
    names = [name for name in names if name]
    if not names:
        return

    def _delete():
        for name in names:
            try:
                if _is_referenced(name):
                    continue
                storage.delete(name)
            except Exception:
                logger.exception('No se pudo borrar el archivo huérfano %r', name)

    transaction.on_commit(_delete, robust=True)


def _file_fields(model):
    return [f for f in model._meta.get_fields() if isinstance(f, FileField)]


def _deletable_name(field, name):
    """Nombre de archivo borrable (excluye vacío y el valor por defecto compartido)."""
    if not name:
        return None
    default = field.default if field.has_default() else None
    if callable(default):
        default = None
    return None if name == default else name


def _on_post_delete(sender, instance, **kwargs):
    for field in _file_fields(sender):
        ff = getattr(instance, field.attname)
        name = _deletable_name(field, ff.name if ff else None)
        if name:
            delete_stored_files_on_commit(field.storage, [name])


def _on_pre_save(sender, instance, raw=False, **kwargs):
    instance._old_file_names = {}
    if raw or instance.pk is None:
        return
    fields = _file_fields(sender)
    row = sender._default_manager.filter(pk=instance.pk).values(*[f.attname for f in fields]).first()
    if row:
        instance._old_file_names = {f.attname: row[f.attname] for f in fields}


def _on_post_save(sender, instance, created=False, raw=False, **kwargs):
    old = getattr(instance, '_old_file_names', None)
    if raw or created or not old:
        return
    for field in _file_fields(sender):
        previous = _deletable_name(field, old.get(field.attname))
        current = getattr(instance, field.attname)
        if previous and previous != (current.name if current else None):
            delete_stored_files_on_commit(field.storage, [previous])
    instance._old_file_names = {}


def connect_file_cleanup(*models):
    """Conecta borrado y reemplazo de archivos para los FileField de `models`."""
    for model in models:
        uid = f'file-cleanup-{model._meta.label_lower}'
        post_delete.connect(_on_post_delete, sender=model, dispatch_uid=uid + '-del')
        pre_save.connect(_on_pre_save, sender=model, dispatch_uid=uid + '-pre')
        post_save.connect(_on_post_save, sender=model, dispatch_uid=uid + '-post')
