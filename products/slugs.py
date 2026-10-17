"""Persistent URL slugs for categories and products.

Canonical public product URL: `/<business_id>/<category-slug>/<product-slug>`,
with the reserved `sin-categoria` segment standing in for products without a
category.

Policy:
- Slugs are generated once (on first save) from the display name and are never
  regenerated on rename, so shared links keep working.
- Category slugs are unique per business; product slugs are unique per
  (business, category), or per business among uncategorized products.
- Collisions resolve deterministically: `base`, `base-2`, `base-3`, ...
- Names that normalize to an empty slug (emoji, CJK, punctuation only) fall
  back to an id-based slug (`c-<id>` / `p-<id>`).
- The migration backfill keeps its own frozen copy of these rules
  (`migrations/_slug_backfill.py`).
- Uniqueness is enforced by DB constraints (migration 0020); allocation retries
  on IntegrityError so concurrent inserts of the same name both succeed.

This module deliberately imports no models so migrations can use it with
historical models.
"""
import uuid
from django.db import IntegrityError, router, transaction
from django.utils.text import slugify

SLUG_MAX_LENGTH = 255
UNCATEGORIZED_SLUG = 'sin-categoria'
# `product` is the static segment of the legacy `/<business>/product/<id>`
# route, which the frontend router matches before the canonical one.
LEGACY_PRODUCT_SEGMENT = 'product'
RESERVED_CATEGORY_SLUGS = frozenset({UNCATEGORIZED_SLUG, LEGACY_PRODUCT_SEGMENT})
CATEGORY_FALLBACK_PREFIX = 'c'
PRODUCT_FALLBACK_PREFIX = 'p'
MAX_ALLOCATION_ATTEMPTS = 5
# Longest suffix we budget for when truncating (`-` + up to 11 digits).
_SUFFIX_BUDGET = 12


def slugify_name(name):
    """ASCII slug for `name` ('Café Ñandú!' -> 'cafe-nandu'); '' if nothing survives."""
    return slugify(name or '')[:SLUG_MAX_LENGTH].strip('-')


def fallback_slug(prefix, pk):
    return f'{prefix}-{pk}'


def with_suffix(base, n):
    """`base` for n == 1, else `base-n`, truncated so the result fits SLUG_MAX_LENGTH."""
    if n == 1:
        return base[:SLUG_MAX_LENGTH]
    suffix = f'-{n}'
    return base[:SLUG_MAX_LENGTH - len(suffix)].rstrip('-') + suffix


def pick_free_slug(base, taken, reserved=frozenset()):
    """First of `base`, `base-2`, `base-3`, ... not in `taken` nor `reserved`."""
    n = 1
    while True:
        candidate = with_suffix(base, n)
        if candidate not in taken and candidate not in reserved:
            return candidate
        n += 1


def allocate_unique_slug(base, exists_qs, reserved=frozenset()):
    """Deterministically pick a free slug for `base` within `exists_qs` (the scope, excluding self)."""
    prefix = base[:SLUG_MAX_LENGTH - _SUFFIX_BUDGET].rstrip('-')
    taken = set(exists_qs.filter(slug__startswith=prefix).values_list('slug', flat=True))
    return pick_free_slug(base, taken, reserved)


class SlugAllocationMixin:
    """Model mixin: allocates `slug` on first save and keeps it unique in its scope.

    Subclasses define SLUG_SCOPE_FIELDS (attnames whose values define the
    uniqueness scope), SLUG_FALLBACK_PREFIX, optional RESERVED_SLUGS, and
    `slug_scope_queryset()` (rows sharing this row's scope).
    """
    SLUG_SCOPE_FIELDS = ()
    SLUG_FALLBACK_PREFIX = 'x'
    RESERVED_SLUGS = frozenset()

    @classmethod
    def from_db(cls, db, field_names, values):
        instance = super().from_db(db, field_names, values)
        instance._loaded_slug_scope = instance._current_slug_scope()
        return instance

    def _current_slug_scope(self):
        # Read straight from __dict__ so deferred fields never trigger a query;
        # None means "unknown", which makes save() re-check for collisions.
        if any(attname not in self.__dict__ for attname in self.SLUG_SCOPE_FIELDS):
            return None
        return tuple(self.__dict__[attname] for attname in self.SLUG_SCOPE_FIELDS)

    def slug_scope_queryset(self):
        raise NotImplementedError

    def _slug_scope_qs_excluding_self(self):
        qs = self.slug_scope_queryset()
        if self.pk is not None:
            qs = qs.exclude(pk=self.pk)
        return qs

    def _slug_taken(self, slug):
        return slug in self.RESERVED_SLUGS or self._slug_scope_qs_excluding_self().filter(slug=slug).exists()

    def save(self, *args, **kwargs):
        loaded_scope = getattr(self, '_loaded_slug_scope', None)
        scope_unchanged = loaded_scope is not None and loaded_scope == self._current_slug_scope()
        if self.slug and not self._state.adding and scope_unchanged:
            # Fast path: a normal update (rename included) never touches the slug.
            super().save(*args, **kwargs)
            self._loaded_slug_scope = self._current_slug_scope()
            return

        update_fields = kwargs.get('update_fields')
        if update_fields is not None and 'slug' not in update_fields:
            kwargs['update_fields'] = list(update_fields) + ['slug']

        keep_existing = bool(self.slug)  # moved row / caller-supplied slug: keep it unless it collides
        base = self.slug or slugify_name(self.name)
        using = kwargs.get('using') or router.db_for_write(type(self), instance=self)

        for attempt in range(1, MAX_ALLOCATION_ATTEMPTS + 1):
            was_adding = self._state.adding
            needs_id_fallback = False
            final = None
            if keep_existing and attempt == 1 and not self._slug_taken(self.slug):
                pass
            elif base:
                self.slug = allocate_unique_slug(base, self._slug_scope_qs_excluding_self(), self.RESERVED_SLUGS)
            elif self.pk is not None:
                base = fallback_slug(self.SLUG_FALLBACK_PREFIX, self.pk)
                self.slug = allocate_unique_slug(base, self._slug_scope_qs_excluding_self(), self.RESERVED_SLUGS)
            else:
                # Id-based fallback needs the id: insert with a unique
                # placeholder, then swap it inside the same savepoint.
                self.slug = f'tmp-{uuid.uuid4().hex}'
                needs_id_fallback = True

            try:
                with transaction.atomic(using=using):
                    super().save(*args, **kwargs)
                    if needs_id_fallback:
                        final = allocate_unique_slug(
                            fallback_slug(self.SLUG_FALLBACK_PREFIX, self.pk),
                            self._slug_scope_qs_excluding_self(),
                            self.RESERVED_SLUGS,
                        )
                        type(self)._base_manager.using(using).filter(pk=self.pk).update(slug=final)
                        self.slug = final
            except IntegrityError:
                if was_adding:
                    # The savepoint rolled the insert back; forget the id.
                    self.pk = None
                    self._state.adding = True
                # Only retry when our slug lost a race; anything else (e.g. the
                # stopper constraints) propagates to the caller unchanged.
                if attempt == MAX_ALLOCATION_ATTEMPTS or not self._slug_taken(final or self.slug):
                    raise
                if needs_id_fallback:
                    self.slug = None
                continue
            self._loaded_slug_scope = self._current_slug_scope()
            return
