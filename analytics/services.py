import re
from collections import defaultdict
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Count, Max, Q, Sum
from django.db.models.fields.json import KeyTextTransform
from django.db.models.functions import TruncDate
from django.utils import timezone

from products.models import Product

from .models import AnalyticsEvent

EventType = AnalyticsEvent.EventType

# Detección básica y testeable de automatizaciones evidentes. No pretende ser perfecta.
_BOT_UA_RE = re.compile(
    r'bot\b|bot/|crawl|spider|slurp|headless|phantomjs|puppeteer|playwright|selenium|lighthouse|'
    r'pingdom|uptime|curl/|wget/|python-requests|python-urllib|httpclient|okhttp|'
    r'go-http-client|java/|libwww|scrapy|facebookexternalhit|whatsapp/|telegrambot',
    re.IGNORECASE,
)


def is_bot_user_agent(user_agent):
    """True si el User-Agent corresponde a un bot/cliente automatizado conocido.

    Un UA vacío no se considera bot (conservador). El valor crudo nunca se guarda.
    """
    if not user_agent:
        return False
    return bool(_BOT_UA_RE.search(user_agent))


# ---------------------------------------------------------------- ingesta

def ingest_events(business, validated_events):
    """Inserta eventos ya validados de forma idempotente.

    Devuelve `(accepted, duplicates, rejected)`. Cada fila se inserta en su propio
    savepoint: una violación de PK (event_id repetido) o de un UniqueConstraint
    (visita/vista ya registrada en la sesión) cuenta como duplicado y no rompe la
    transacción ni el resto del batch. Producto inexistente, de otro negocio o inactivo
    cuenta como rechazado.
    """
    product_ids = {e['product_id'] for e in validated_events if e.get('product_id')}
    products = {}
    if product_ids:
        # Una sola query para todo el batch; el filtro por negocio garantiza el aislamiento.
        products = dict(
            Product.objects.filter(business=business, is_active=True, id__in=product_ids)
            .values_list('id', 'name')
        )

    accepted = duplicates = rejected = 0
    for data in validated_events:
        product_id = data.get('product_id')
        metadata = {}
        if product_id is not None:
            if product_id not in products:
                rejected += 1
                continue
            metadata['product_name'] = products[product_id]
        try:
            with transaction.atomic():
                AnalyticsEvent.objects.create(
                    id=data['event_id'],
                    business=business,
                    product_id=product_id,
                    visitor_id=data['visitor_id'],
                    session_id=data['session_id'],
                    event_type=data['type'],
                    source=data.get('source', ''),
                    action_type=data.get('action_type', ''),
                    engaged_ms=data.get('engaged_ms', 0),
                    client_occurred_at=data.get('occurred_at'),
                    metadata=metadata,
                )
        except IntegrityError:
            duplicates += 1
        else:
            accepted += 1
    return accepted, duplicates, rejected


# ------------------------------------------------------------ agregación

PULSE_DAYS = 7
TOP_PRODUCTS_LIMIT = 5
DELETED_PRODUCT_NAME = 'Producto eliminado'


def _business_tz():
    return ZoneInfo(settings.TIME_ZONE)


def _iso_utc(value):
    if value is None:
        return None
    return value.astimezone(ZoneInfo('UTC')).strftime('%Y-%m-%dT%H:%M:%SZ')


def build_pulse(business, now=None, days=PULSE_DAYS):
    """Métricas del widget "Pulse" de un negocio. Días en America/Asuncion.

    Visitas/orígenes/acciones/top productos cubren la ventana de `days` días locales
    (hoy incluido); `today` cubre solo el día local actual.
    """
    tz = _business_tz()
    now = now or timezone.now()
    today = now.astimezone(tz).date()
    first_day = today - timedelta(days=days - 1)
    window_start = datetime.combine(first_day, time.min, tzinfo=tz)
    today_start = datetime.combine(today, time.min, tzinfo=tz)

    events = AnalyticsEvent.objects.filter(business=business)
    window = events.filter(received_at__gte=window_start, received_at__lte=now)
    visits = window.filter(event_type=EventType.CATALOG_VISIT)

    # Historial: visitas por fecha local, con ceros para los días sin actividad.
    per_day = {
        row['day']: row['visits']
        for row in visits.annotate(day=TruncDate('received_at', tzinfo=tz))
        .values('day').annotate(visits=Count('id')).order_by()
    }
    history = []
    for offset in range(days):
        day = first_day + timedelta(days=offset)
        history.append({'date': day.isoformat(), 'visits': per_day.get(day, 0)})

    today_stats = visits.filter(received_at__gte=today_start).aggregate(
        visits=Count('id'), unique_visitors=Count('visitor_id', distinct=True),
    )
    last_visit_at = events.filter(event_type=EventType.CATALOG_VISIT).aggregate(
        last=Max('received_at'))['last']

    sources = {key: 0 for key in AnalyticsEvent.Source.values}
    for row in visits.values('source').annotate(n=Count('id')).order_by():
        # Visitas sin source (cliente viejo / sin dato) se cuentan como directas.
        key = row['source'] if row['source'] in sources else 'direct'
        sources[key] += row['n']

    actions = {key: 0 for key in AnalyticsEvent.ActionType.values}
    for row in window.filter(event_type=EventType.ACTION_CLICK).values('action_type').annotate(n=Count('id')).order_by():
        if row['action_type'] in actions:
            actions[row['action_type']] += row['n']

    return {
        'generated_at': _iso_utc(now),
        'timezone': settings.TIME_ZONE,
        'today': {
            'visits': today_stats['visits'],
            'unique_visitors': today_stats['unique_visitors'],
            'last_visit_at': _iso_utc(last_visit_at),
        },
        'history': history,
        'sources': sources,
        'actions': actions,
        'top_products': _top_products(window),
    }


def _top_products(window):
    """Ranking por `product_view` (no por opens) con tiempo medio activo por vista.

    Dos queries agrupadas + una de nombres, sin importar cuántos productos haya.
    Un producto borrado (product NULL) se identifica por `metadata.product_name`.
    """
    name_expr = KeyTextTransform('product_name', 'metadata')
    product_events = window.filter(
        event_type__in=[EventType.PRODUCT_VIEW, EventType.PRODUCT_ENGAGEMENT]
    ).annotate(meta_name=name_expr)

    stats = defaultdict(lambda: {'views': 0, 'ms': 0, 'meta_name': None})
    rows = product_events.values('product_id', 'meta_name').annotate(
        views=Count('id', filter=Q(event_type=EventType.PRODUCT_VIEW)),
        ms=Sum('engaged_ms', filter=Q(event_type=EventType.PRODUCT_ENGAGEMENT)),
    ).order_by()
    for row in rows:
        key = row['product_id'] if row['product_id'] is not None else ('deleted', row['meta_name'])
        entry = stats[key]
        entry['views'] += row['views']
        entry['ms'] += row['ms'] or 0
        entry['meta_name'] = entry['meta_name'] or row['meta_name']

    ranked = [(key, entry) for key, entry in stats.items() if entry['views'] > 0]
    ranked.sort(key=lambda item: (
        -item[1]['views'], -item[1]['ms'], str(item[1]['meta_name'] or ''), str(item[0]),
    ))
    ranked = ranked[:TOP_PRODUCTS_LIMIT]

    live_ids = [key for key, _ in ranked if not isinstance(key, tuple)]
    names = dict(Product.objects.filter(id__in=live_ids).values_list('id', 'name')) if live_ids else {}

    result = []
    for key, entry in ranked:
        views = entry['views']
        if isinstance(key, tuple):
            product_id, name = None, entry['meta_name'] or DELETED_PRODUCT_NAME
        else:
            product_id = key
            name = names.get(key) or entry['meta_name'] or DELETED_PRODUCT_NAME
        result.append({
            'product_id': product_id,
            'name': name,
            'views': views,
            # Redondeo half-up en enteros: ms totales / vistas válidas, en segundos.
            'average_engaged_seconds': (2 * entry['ms'] + 1000 * views) // (2000 * views),
        })
    return result
