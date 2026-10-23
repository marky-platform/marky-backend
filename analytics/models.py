from django.db import models
from django.db.models import Q

from business.models import BusinessProfile
from products.models import Product


class AnalyticsEvent(models.Model):
    """Evento anónimo e inmutable originado en las vistas públicas del catálogo.

    No guarda IP, User-Agent ni huellas del navegador: la identidad es un UUID
    generado por el navegador (`visitor_id`) y una sesión por negocio (`session_id`).
    """

    class EventType(models.TextChoices):
        CATALOG_VISIT = "catalog_visit", "Catalog visit"
        PRODUCT_OPEN = "product_open", "Product open"
        PRODUCT_VIEW = "product_view", "Product view"
        PRODUCT_ENGAGEMENT = "product_engagement", "Product engagement"
        ACTION_CLICK = "action_click", "Action click"

    class Source(models.TextChoices):
        PUBLIC_LINK = "public_link", "Public link"
        QR = "qr", "QR"
        DIRECT = "direct", "Direct"
        OTHER = "other", "Other"

    class ActionType(models.TextChoices):
        LOCATION = "location", "Location"
        WHATSAPP = "whatsapp", "WhatsApp"
        INSTAGRAM = "instagram", "Instagram"
        OTHER = "other", "Other"

    # Tope de ms activos por evento de engagement (chunks de ~15 s desde el cliente).
    MAX_ENGAGED_MS = 30_000

    PRODUCT_EVENT_TYPES = (
        EventType.PRODUCT_OPEN,
        EventType.PRODUCT_VIEW,
        EventType.PRODUCT_ENGAGEMENT,
    )

    id = models.UUIDField(primary_key=True, editable=False)  # event_id del cliente
    business = models.ForeignKey(BusinessProfile, on_delete=models.CASCADE, related_name='analytics_events')
    product = models.ForeignKey(
        Product, null=True, blank=True, on_delete=models.SET_NULL, related_name='analytics_events',
    )
    visitor_id = models.UUIDField()
    session_id = models.UUIDField()
    event_type = models.CharField(max_length=32, choices=EventType.choices)
    source = models.CharField(max_length=24, blank=True)
    action_type = models.CharField(max_length=24, blank=True)
    engaged_ms = models.PositiveIntegerField(default=0)
    client_occurred_at = models.DateTimeField(null=True, blank=True)
    received_at = models.DateTimeField(auto_now_add=True)
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=['business', 'received_at'], name='ae_biz_received_idx'),
            models.Index(fields=['business', 'event_type', 'received_at'], name='ae_biz_type_received_idx'),
            models.Index(
                fields=['business', 'product', 'event_type', 'received_at'], name='ae_biz_prod_type_recv_idx',
            ),
            models.Index(fields=['business', 'session_id'], name='ae_biz_session_idx'),
            models.Index(fields=['business', 'visitor_id', 'received_at'], name='ae_biz_visitor_recv_idx'),
            # Para la purga de retención, que filtra solo por fecha.
            models.Index(fields=['received_at'], name='ae_received_idx'),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=['business', 'session_id', 'event_type'],
                condition=Q(event_type='catalog_visit'),
                name='ae_unique_catalog_visit_per_session',
            ),
            models.UniqueConstraint(
                fields=['business', 'product', 'session_id', 'event_type'],
                condition=Q(event_type='product_view'),
                name='ae_unique_product_view_per_session',
            ),
            models.CheckConstraint(
                condition=Q(event_type__in=['catalog_visit', 'product_open', 'product_view',
                                        'product_engagement', 'action_click']),
                name='ae_valid_event_type',
            ),
            models.CheckConstraint(
                condition=Q(event_type='action_click', action_type__in=['location', 'whatsapp', 'instagram', 'other'])
                | (~Q(event_type='action_click') & Q(action_type='')),
                name='ae_action_type_only_for_clicks',
            ),
            models.CheckConstraint(
                condition=Q(engaged_ms=0)
                | Q(event_type='product_engagement', engaged_ms__lte=30_000),
                name='ae_engaged_ms_only_for_engagement',
            ),
            # `product` es obligatorio para product_open/view/engagement, pero no se puede
            # exigir en la BD: `on_delete=SET_NULL` dejaría en NULL esas filas al borrar un
            # producto y violaría el constraint. Se valida en el serializer de ingesta.
        ]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValueError('AnalyticsEvent es inmutable: no se puede actualizar una vez creado.')
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.event_type} {self.business_id} {self.id}"
