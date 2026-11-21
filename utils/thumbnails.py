"""Miniaturas de imágenes generadas con Pillow al subir (F-24).

Una miniatura nunca debe hacer fallar una subida: ante cualquier error se
registra y se devuelve None (el frontend usa la imagen original).
"""
import io
import logging
import os

from django.core.files.base import ContentFile
from PIL import Image, ImageOps

logger = logging.getLogger(__name__)

THUMBNAIL_MAX_SIZE = 400
THUMBNAIL_QUALITY = 80


def make_thumbnail(source):
    """Devuelve un ContentFile WebP de a lo sumo 400px (sin ampliar), o None si falla.

    `source` es un archivo abierto/subido; su posición se restaura. Los GIF
    animados usan el primer fotograma.
    """
    try:
        source.seek(0)
        with Image.open(source) as img:
            img = ImageOps.exif_transpose(img)
            img.thumbnail((THUMBNAIL_MAX_SIZE, THUMBNAIL_MAX_SIZE), Image.LANCZOS)
            if img.mode not in ('RGB', 'RGBA'):
                img = img.convert('RGBA' if 'transparency' in img.info or img.mode in ('LA', 'PA') else 'RGB')
            buf = io.BytesIO()
            img.save(buf, format='WEBP', quality=THUMBNAIL_QUALITY)
        stem = os.path.splitext(os.path.basename(getattr(source, 'name', '') or 'image'))[0]
        return ContentFile(buf.getvalue(), name=f'{stem}.webp')
    except Exception:
        logger.exception('No se pudo generar la miniatura de %r', getattr(source, 'name', None))
        return None
    finally:
        try:
            source.seek(0)
        except Exception:
            pass
