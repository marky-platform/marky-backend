"""Genera miniaturas para imágenes existentes que aún no la tienen (F-24).

Idempotente: solo procesa filas sin miniatura. Uso:
    python manage.py generate_thumbnails
"""
from django.core.management.base import BaseCommand
from django.db.models import Q

from products.models import ProductMedia, ProductVariant
from utils.thumbnails import make_thumbnail


class Command(BaseCommand):
    help = 'Genera miniaturas para imágenes de productos y presentaciones que no la tienen.'

    def _backfill(self, queryset, source_field):
        done = failed = 0
        for obj in queryset.iterator():
            source = getattr(obj, source_field)
            try:
                with source.storage.open(source.name, 'rb') as fh:
                    thumb = make_thumbnail(fh)
            except Exception as exc:
                self.stderr.write(f'{obj._meta.label} {obj.pk}: no se pudo leer {source.name!r} ({exc})')
                failed += 1
                continue
            if thumb is None:
                failed += 1
                continue
            obj.thumbnail.save(thumb.name, thumb, save=False)
            obj.save(update_fields=['thumbnail'])
            done += 1
        return done, failed

    def handle(self, *args, **options):
        missing = Q(thumbnail__isnull=True) | Q(thumbnail='')
        media = ProductMedia.objects.filter(missing, media_type=ProductMedia.IMAGE).exclude(file='')
        variants = ProductVariant.objects.filter(missing).exclude(Q(image='') | Q(image__isnull=True))
        for label, qs, field in (('media', media, 'file'), ('variantes', variants, 'image')):
            done, failed = self._backfill(qs, field)
            self.stdout.write(f'{label}: {done} miniaturas generadas, {failed} fallidas.')
