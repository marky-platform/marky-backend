import logging
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from post_office import mail

logger = logging.getLogger(__name__)


def queue_mail(recipient, template, context, expires_in=None):
    """Encola un correo en post_office (prioridad por defecto `medium`: no se envía en línea).

    La fila de la cola se crea en la misma transacción que la escritura que la motiva: si esa
    transacción hace rollback el correo desaparece, y el worker nunca ve filas sin commit.
    Un fallo al encolar (plantilla ausente, error de BD) se registra y NO rompe el request;
    el savepoint evita envenenar la transacción exterior. Devuelve True si quedó encolado.
    El envío real, los reintentos y su registro los hace `send_queued_mail`.
    """
    kwargs = {}
    if expires_in is not None:
        kwargs['expires_at'] = timezone.now() + expires_in
    try:
        with transaction.atomic():
            mail.send(recipient, settings.DEFAULT_FROM_EMAIL, template=template, context=context, **kwargs)
    except Exception:
        logger.exception("No se pudo encolar el correo '%s'", template)
        return False
    return True


VERIFICATION_MAIL_TTL = timedelta(minutes=15)
RECOVERY_MAIL_TTL = timedelta(hours=24)
