from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db.models import Q
from django.utils import timezone
from post_office.models import Email, STATUS


class Command(BaseCommand):
    help = (
        'Elimina de post_office los correos ya enviados/fallidos y los encolados vencidos con más de '
        'MAIL_RETENTION_HOURS horas (F-44: la cola guarda códigos de verificación y enlaces en claro). '
        'Los correos pendientes y vigentes no se tocan. Programar de forma periódica (p. ej. cada hora).'
    )

    def add_arguments(self, parser):
        parser.add_argument('--hours', type=int, default=None,
                            help='Horas de retención (por defecto MAIL_RETENTION_HOURS).')

    def handle(self, *args, **options):
        hours = options['hours'] if options['hours'] is not None else settings.MAIL_RETENTION_HOURS
        now = timezone.now()
        cutoff = now - timedelta(hours=hours)
        old = Email.objects.filter(created__lt=cutoff)
        expired_queued = Q(status=STATUS.queued, expires_at__lt=now)
        deleted, _ = old.filter(Q(status__in=[STATUS.sent, STATUS.failed]) | expired_queued).delete()
        self.stdout.write(f'{deleted} registros de correo eliminados (anteriores a {cutoff:%Y-%m-%d %H:%M}).')
