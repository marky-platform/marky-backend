from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from analytics.models import AnalyticsEvent, CatalogVisitWindow
from analytics.services import CATALOG_VISIT_WINDOW


class Command(BaseCommand):
    help = 'Elimina eventos de analytics más antiguos que ANALYTICS_EVENT_RETENTION_DAYS, en lotes acotados.'

    def add_arguments(self, parser):
        parser.add_argument('--days', type=int, default=None,
                            help='Días de retención (por defecto ANALYTICS_EVENT_RETENTION_DAYS).')
        parser.add_argument('--batch-size', type=int, default=5000)
        parser.add_argument('--dry-run', action='store_true', help='Solo cuenta, no borra.')

    def handle(self, *args, **options):
        days = options['days'] if options['days'] is not None else settings.ANALYTICS_EVENT_RETENTION_DAYS
        batch_size = options['batch_size']
        if days < 1 or batch_size < 1:
            raise CommandError('--days y --batch-size deben ser >= 1.')

        cutoff = timezone.now() - timedelta(days=days)
        expired = AnalyticsEvent.objects.filter(received_at__lt=cutoff)
        if options['dry_run']:
            self.stdout.write(f'{expired.count()} eventos expirados (anteriores a {cutoff:%Y-%m-%d}); dry-run.')
            return

        # Una ventana vencida ya no deduplica nada: se descarta aunque no haya retención vencida.
        CatalogVisitWindow.objects.filter(last_counted_at__lt=timezone.now() - CATALOG_VISIT_WINDOW).delete()

        total = 0
        while True:
            ids = list(expired.values_list('pk', flat=True)[:batch_size])
            if not ids:
                break
            deleted, _ = AnalyticsEvent.objects.filter(pk__in=ids).delete()
            total += deleted
        self.stdout.write(f'{total} eventos eliminados (anteriores a {cutoff:%Y-%m-%d}).')
