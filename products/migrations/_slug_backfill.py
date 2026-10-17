"""Frozen slug backfill used by migrations 0019 and 0020.

A copy of the slug rules as of those migrations, so later edits to
`products/slugs.py` never change what a historical migration does. The
leading underscore keeps Django's migration loader from treating this
module as a migration.
"""
from collections import defaultdict

from django.db.models import Q
from django.utils.text import slugify

SLUG_MAX_LENGTH = 255
RESERVED_CATEGORY_SLUGS = frozenset({'sin-categoria', 'product'})


def slugify_name(name):
    return slugify(name or '')[:SLUG_MAX_LENGTH].strip('-')


def fallback_slug(prefix, pk):
    return f'{prefix}-{pk}'


def with_suffix(base, n):
    if n == 1:
        return base[:SLUG_MAX_LENGTH]
    suffix = f'-{n}'
    return base[:SLUG_MAX_LENGTH - len(suffix)].rstrip('-') + suffix


def pick_free_slug(base, taken, reserved=frozenset()):
    n = 1
    while True:
        candidate = with_suffix(base, n)
        if candidate not in taken and candidate not in reserved:
            return candidate
        n += 1


def backfill_missing_slugs(category_model, product_model, batch_size=500):
    """Fill empty slugs in deterministic (business, category, id) order.

    Works with historical models (used by migrations 0019/0020). Existing slugs
    are kept and count as taken. Returns (categories_filled, products_filled).
    """
    empty = Q(slug__isnull=True) | Q(slug='')

    taken = defaultdict(set)
    for business_id, slug in category_model.objects.exclude(empty).values_list('business_id', 'slug'):
        taken[business_id].add(slug)
    categories = list(
        category_model.objects.filter(empty).order_by('business_id', 'id').only('id', 'business_id', 'name')
    )
    for category in categories:
        base = slugify_name(category.name) or fallback_slug('c', category.id)
        category.slug = pick_free_slug(base, taken[category.business_id], RESERVED_CATEGORY_SLUGS)
        taken[category.business_id].add(category.slug)
    category_model.objects.bulk_update(categories, ['slug'], batch_size=batch_size)

    taken = defaultdict(set)
    for business_id, category_id, slug in product_model.objects.exclude(empty).values_list(
        'business_id', 'category_id', 'slug'
    ):
        taken[(business_id, category_id)].add(slug)
    products = list(
        product_model.objects.filter(empty)
        .order_by('business_id', 'category_id', 'id')
        .only('id', 'business_id', 'category_id', 'name')
    )
    for product in products:
        scope = (product.business_id, product.category_id)
        base = slugify_name(product.name) or fallback_slug('p', product.id)
        product.slug = pick_free_slug(base, taken[scope])
        taken[scope].add(product.slug)
    product_model.objects.bulk_update(products, ['slug'], batch_size=batch_size)

    return len(categories), len(products)
