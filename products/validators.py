import os

from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

IMAGE_EXTENSIONS = ['.jpg', '.jpeg', '.png', '.webp']
VIDEO_EXTENSIONS = ['.mp4', '.mov', '.webm']

MAX_IMAGE_SIZE = 5 * 1024 * 1024
MAX_VIDEO_SIZE = 80 * 1024 * 1024


def validate_media_extension(value):
    """
    Validate that a product media file has an allowed image or video extension.
    """
    ext = os.path.splitext(value.name)[1].lower()
    valid_extensions = IMAGE_EXTENSIONS + VIDEO_EXTENSIONS
    if ext not in valid_extensions:
        raise ValidationError(_(
            f"Extensión de archivo no válida: {ext}. Extensiones permitidas: "
            f"{', '.join(IMAGE_EXTENSIONS)} (imágenes), {', '.join(VIDEO_EXTENSIONS)} (video)."
        ))


def validate_media_size(value):
    """
    Validate that a product media file does not exceed the max size for its
    type (image or video), inferred from the file extension.
    """
    ext = os.path.splitext(value.name)[1].lower()
    filesize = value.size
    if ext in VIDEO_EXTENSIONS:
        if filesize > MAX_VIDEO_SIZE:
            raise ValidationError(_("El video supera el peso máximo permitido. Máximo permitido: 80MB."))
    elif filesize > MAX_IMAGE_SIZE:
        raise ValidationError(_("La imagen supera el peso máximo permitido. Máximo permitido: 5MB."))


MAX_MODEL_3D_SIZE = 25 * 1024 * 1024

GLB_MAGIC = b'glTF'
ZIP_MAGIC = b'PK\x03\x04'


def _max_model_3d_label():
    return f'{MAX_MODEL_3D_SIZE // (1024 * 1024)}MB'


def _validate_model_3d(value, ext, magic, label):
    """
    Shared GLB/USDZ check: extension, size and magic bytes. Browser MIME types
    are unreliable for these formats, so the file header is inspected instead.
    """
    if os.path.splitext(value.name)[1].lower() != ext:
        raise ValidationError(_(f"El archivo {label} debe tener la extensión {ext}."))
    if value.size > MAX_MODEL_3D_SIZE:
        raise ValidationError(_(
            f"El archivo {label} supera el peso máximo permitido. Máximo permitido: {_max_model_3d_label()}."
        ))
    try:
        value.seek(0)
        header = value.read(len(magic))
    finally:
        value.seek(0)
    if header != magic:
        raise ValidationError(_(f"El archivo no es un {label} válido."))


def validate_glb_file(value):
    _validate_model_3d(value, '.glb', GLB_MAGIC, 'GLB')


def validate_usdz_file(value):
    _validate_model_3d(value, '.usdz', ZIP_MAGIC, 'USDZ')
