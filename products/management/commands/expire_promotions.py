import logging

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from business.models import BusinessProfile
from products.services import handle_expired_promotions_for_business

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = (
        'Limpia las promociones vencidas (productos y categorías) de todos los negocios '
        'y notifica una vez por cada una. Pensado para programarse (cron / Railway cron), '
        'p. ej. cada 5 minutos. Un negocio que falla no detiene a los demás: el error se '
        'registra y el comando termina con código distinto de cero al final si hubo fallos.'
    )

    def handle(self, *args, **options):
        now = timezone.now()
        count = 0
        failed = 0
        for business in BusinessProfile.objects.select_related('user').iterator():
            try:
                handle_expired_promotions_for_business(business, now=now)
                count += 1
            except Exception:
                failed += 1
                logger.exception(
                    'Error al procesar promociones vencidas del negocio %s', business.pk,
                )
        self.stdout.write(f'Promociones vencidas procesadas para {count} negocios.')
        if failed:
            # CommandError => salida con código 1, para que el cron/Railway lo marque como fallido.
            raise CommandError(f'Falló el procesamiento de {failed} negocio(s); revisa los logs.')
