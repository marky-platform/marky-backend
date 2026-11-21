import logging
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from post_office import mail

logger = logging.getLogger(__name__)


def _dispatch_now(email):
    """Envía en línea un correo ya confirmado en la BD. Un fallo queda registrado y la fila queda
    `failed`/`queued` para que `send_queued_mail` la reintente cuando exista un worker o cron."""
    try:
        email.dispatch()
    except Exception:
        logger.exception("Falló el envío inmediato del correo %s; queda en cola para reintento", email.pk)


def queue_mail(recipient, template, context, expires_in=None):
    """Registra el correo en post_office y lo envía en cuanto la transacción hace commit.

    La fila de la cola se crea en la misma transacción que la escritura que la motiva: si esa
    transacción hace rollback el correo desaparece y nunca se envía. El envío ocurre tras el
    commit (`on_commit`), así el usuario lo recibe al instante sin depender de un worker. Si el
    envío falla (Resend caído), el request NO se rompe: la fila queda para `send_queued_mail`,
    que reintenta hasta MAX_RETRIES. Un fallo al encolar (plantilla ausente, error de BD) se
    registra y tampoco rompe el request; el savepoint evita envenenar la transacción exterior.
    Devuelve True si quedó registrado.
    """
    kwargs = {}
    if expires_in is not None:
        kwargs['expires_at'] = timezone.now() + expires_in
    try:
        with transaction.atomic():
            email = mail.send(recipient, settings.DEFAULT_FROM_EMAIL, template=template, context=context, **kwargs)
            transaction.on_commit(lambda: _dispatch_now(email))
    except Exception:
        logger.exception("No se pudo encolar el correo '%s'", template)
        return False
    return True


VERIFICATION_MAIL_TTL = timedelta(minutes=15)
RECOVERY_MAIL_TTL = timedelta(hours=24)
