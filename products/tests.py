import json
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest import mock, skipIf

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, connection, models, transaction
from django.test import SimpleTestCase
from django.utils import timezone
from rest_framework import serializers

from utils.tests_base import MarkyAPITestCase
from notifications.models import Notification
from products.filters import ProductCategoryFilter
from products.models import Product, ProductCategory, ProductVariant, ProductAddon
from products.services import handle_expired_promotions_for_business
from products import slugs as slugs_module
from products.migrations._slug_backfill import backfill_missing_slugs
from products.slugs import SLUG_MAX_LENGTH, slugify_name, with_suffix
from products.validators import validate_media_extension, validate_media_size
from products.promotions import (
    compute_promotion_status,
    resolve_effective_promotion,
    ACTIVE,
    SCHEDULED,
    EXPIRED,
    INACTIVE,
)

User = get_user_model()


# ---------------------------------------------------------------------------
# Unit tests — no database, test model methods in isolation
# ---------------------------------------------------------------------------

def _make_bp(primary_currency=object(), secondary_currency=None, exchange_rate=None, is_primary_to_secondary=True):
    """Return a minimal business-profile-like namespace for currency tests."""
    return SimpleNamespace(
        primary_currency=primary_currency,
        secondary_currency=secondary_currency,
        exchange_rate=exchange_rate,
        is_primary_to_secondary=is_primary_to_secondary,
    )


class TestProductCurrencyArithmetic(SimpleTestCase):

    def test_primary_to_secondary_multiplies_by_rate(self):
        product = Product(price=Decimal('10.00'))
        bp = _make_bp(secondary_currency=object(), exchange_rate=Decimal('5000'), is_primary_to_secondary=True)
        primary, secondary, _, __ = product.get_primary_secondary_amounts(bp)
        self.assertEqual(primary, Decimal('10.00'))
        self.assertEqual(secondary, Decimal('50000.00'))

    def test_secondary_to_primary_divides_by_rate(self):
        product = Product(price=Decimal('10.00'))
        bp = _make_bp(secondary_currency=object(), exchange_rate=Decimal('2'), is_primary_to_secondary=False)
        _, secondary, _, __ = product.get_primary_secondary_amounts(bp)
        self.assertEqual(secondary, Decimal('5.00'))

    def test_rate_zero_returns_none_for_secondary(self):
        product = Product(price=Decimal('10.00'))
        bp = _make_bp(secondary_currency=object(), exchange_rate=Decimal('0'), is_primary_to_secondary=False)
        _, secondary, _, __ = product.get_primary_secondary_amounts(bp)
        self.assertIsNone(secondary)

    def test_no_secondary_currency_returns_none(self):
        product = Product(price=Decimal('10.00'))
        bp = _make_bp(secondary_currency=None, exchange_rate=Decimal('5000'))
        _, secondary, _, sec_cur = product.get_primary_secondary_amounts(bp)
        self.assertIsNone(secondary)
        self.assertIsNone(sec_cur)

    def test_no_exchange_rate_returns_none(self):
        product = Product(price=Decimal('10.00'))
        bp = _make_bp(secondary_currency=object(), exchange_rate=None)
        _, secondary, _, __ = product.get_primary_secondary_amounts(bp)
        self.assertIsNone(secondary)

    def test_none_price_uses_zero(self):
        product = Product(price=None)
        bp = _make_bp(secondary_currency=object(), exchange_rate=Decimal('5000'), is_primary_to_secondary=True)
        primary, secondary, _, __ = product.get_primary_secondary_amounts(bp)
        self.assertEqual(primary, Decimal('0'))
        self.assertEqual(secondary, Decimal('0'))

    def test_no_primary_currency_returns_all_none(self):
        product = Product(price=Decimal('10.00'))
        bp = _make_bp(primary_currency=None)
        result = product.get_primary_secondary_amounts(bp)
        self.assertEqual(result, (None, None, None, None))

    def test_decimal_precision_maintained(self):
        product = Product(price=Decimal('0.01'))
        bp = _make_bp(secondary_currency=object(), exchange_rate=Decimal('3'), is_primary_to_secondary=True)
        _, secondary, _, __ = product.get_primary_secondary_amounts(bp)
        self.assertIsInstance(secondary, Decimal)
        self.assertEqual(secondary, Decimal('0.03'))


class TestProductVariantCurrencyArithmetic(SimpleTestCase):

    def test_primary_to_secondary_multiplies_by_rate(self):
        variant = ProductVariant(price=Decimal('5.00'))
        bp = _make_bp(secondary_currency=object(), exchange_rate=Decimal('4'), is_primary_to_secondary=True)
        primary, secondary, _, __ = variant.get_primary_secondary_amounts(bp)
        self.assertEqual(primary, Decimal('5.00'))
        self.assertEqual(secondary, Decimal('20.00'))

    def test_rate_zero_returns_none_for_secondary(self):
        variant = ProductVariant(price=Decimal('5.00'))
        bp = _make_bp(secondary_currency=object(), exchange_rate=Decimal('0'), is_primary_to_secondary=False)
        _, secondary, _, __ = variant.get_primary_secondary_amounts(bp)
        self.assertIsNone(secondary)

    def test_no_secondary_currency_returns_none(self):
        variant = ProductVariant(price=Decimal('5.00'))
        bp = _make_bp(secondary_currency=None, exchange_rate=Decimal('4'))
        _, secondary, _, __ = variant.get_primary_secondary_amounts(bp)
        self.assertIsNone(secondary)

    def test_no_primary_currency_returns_all_none(self):
        variant = ProductVariant(price=Decimal('5.00'))
        bp = _make_bp(primary_currency=None)
        result = variant.get_primary_secondary_amounts(bp)
        self.assertEqual(result, (None, None, None, None))


class TestProductAddonCurrencyArithmetic(SimpleTestCase):

    def test_primary_to_secondary_multiplies_by_rate(self):
        addon = ProductAddon(price=Decimal('2.50'))
        bp = _make_bp(secondary_currency=object(), exchange_rate=Decimal('4'), is_primary_to_secondary=True)
        primary, secondary, _, __ = addon.get_primary_secondary_amounts(bp)
        self.assertEqual(primary, Decimal('2.50'))
        self.assertEqual(secondary, Decimal('10.00'))

    def test_rate_zero_returns_none_for_secondary(self):
        addon = ProductAddon(price=Decimal('2.50'))
        bp = _make_bp(secondary_currency=object(), exchange_rate=Decimal('0'), is_primary_to_secondary=False)
        _, secondary, _, __ = addon.get_primary_secondary_amounts(bp)
        self.assertIsNone(secondary)

    def test_no_secondary_currency_returns_none(self):
        addon = ProductAddon(price=Decimal('2.50'))
        bp = _make_bp(secondary_currency=None, exchange_rate=Decimal('4'))
        _, secondary, _, __ = addon.get_primary_secondary_amounts(bp)
        self.assertIsNone(secondary)

    def test_no_primary_currency_returns_all_none(self):
        addon = ProductAddon(price=Decimal('2.50'))
        bp = _make_bp(primary_currency=None)
        result = addon.get_primary_secondary_amounts(bp)
        self.assertEqual(result, (None, None, None, None))


# ---------------------------------------------------------------------------
# Integration tests — database required
# ---------------------------------------------------------------------------

class TestPromotionFilter(MarkyAPITestCase):

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.user, cls.profile = cls.make_user('promo_user', 'promo@test.com')
        now = timezone.now()

        cls.cat_multibuy = ProductCategory.objects.create(
            business=cls.profile, name='Multibuy', icon='icon', multibuy_option='2x1',
        )
        cls.cat_discount = ProductCategory.objects.create(
            business=cls.profile, name='Discount', icon='icon', discount_percentage=Decimal('10.00'),
        )
        cls.cat_future = ProductCategory.objects.create(
            business=cls.profile, name='Future', icon='icon',
            discount_percentage=Decimal('10.00'),
            promotion_starts_at=now + timedelta(days=1),
        )
        cls.cat_expired = ProductCategory.objects.create(
            business=cls.profile, name='Expired', icon='icon',
            discount_percentage=Decimal('10.00'),
            promotion_ends_at=now - timedelta(days=1),
        )
        cls.cat_child_promo = ProductCategory.objects.create(
            business=cls.profile, name='Child Promo', icon='icon',
        )
        Product.objects.create(
            name='Promo Child', description='Desc', price=Decimal('10.00'),
            business=cls.profile, category=cls.cat_child_promo,
            discount_percentage=Decimal('15.00'),
        )
        cls.cat_child_expired = ProductCategory.objects.create(
            business=cls.profile, name='Child Expired', icon='icon',
        )
        Product.objects.create(
            name='Expired Child', description='Desc', price=Decimal('10.00'),
            business=cls.profile, category=cls.cat_child_expired,
            discount_percentage=Decimal('15.00'),
            promotion_ends_at=now - timedelta(days=1),
        )
        cls.cat_no_promo = ProductCategory.objects.create(
            business=cls.profile, name='No Promo', icon='icon',
        )

    def _filter(self, value):
        qs = ProductCategory.objects.filter(business=self.profile)
        return set(ProductCategoryFilter({'has_promotion': value}, queryset=qs).qs.values_list('id', flat=True))

    def test_multibuy_category_included(self):
        self.assertIn(self.cat_multibuy.id, self._filter('true'))

    def test_discount_category_included(self):
        self.assertIn(self.cat_discount.id, self._filter('true'))

    def test_future_start_date_excluded(self):
        self.assertNotIn(self.cat_future.id, self._filter('true'))

    def test_past_end_date_excluded(self):
        self.assertNotIn(self.cat_expired.id, self._filter('true'))

    def test_child_product_with_active_promo_includes_category(self):
        self.assertIn(self.cat_child_promo.id, self._filter('true'))

    def test_child_product_with_expired_promo_excludes_category(self):
        self.assertNotIn(self.cat_child_expired.id, self._filter('true'))

    def test_no_promo_category_excluded(self):
        self.assertNotIn(self.cat_no_promo.id, self._filter('true'))

    def test_false_value_returns_all_categories_unfiltered(self):
        all_ids = self._filter('false')
        self.assertIn(self.cat_no_promo.id, all_ids)
        self.assertIn(self.cat_multibuy.id, all_ids)


class TestWithProductsPromotionFilter(MarkyAPITestCase):
    """Covers the with_products endpoint: when has_promotion=true, only categories that
    qualify should be returned, and only the products actually on promotion within them
    (own promo, or inherited from an active category-level promo) should be shown."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.user, cls.profile = cls.make_user('promo_endpoint_user', 'promo_endpoint@test.com')

        # Category with no promotion of its own, but a mix of promoted/unpromoted products.
        cls.cat_mixed = ProductCategory.objects.create(
            business=cls.profile, name='Galletas', icon='icon',
        )
        cls.promoted_product = Product.objects.create(
            name='Promoted', description='Desc', price=Decimal('3.00'),
            business=cls.profile, category=cls.cat_mixed, discount_percentage=Decimal('30.00'),
        )
        cls.unpromoted_product_1 = Product.objects.create(
            name='Unpromoted 1', description='Desc', price=Decimal('2.00'),
            business=cls.profile, category=cls.cat_mixed,
        )
        cls.unpromoted_product_2 = Product.objects.create(
            name='Unpromoted 2', description='Desc', price=Decimal('2.00'),
            business=cls.profile, category=cls.cat_mixed,
        )

        # Category with its own active promotion: every product in it inherits the promo.
        cls.cat_promo = ProductCategory.objects.create(
            business=cls.profile, name='Bebidas', icon='icon', discount_percentage=Decimal('10.00'),
        )
        cls.inherited_product = Product.objects.create(
            name='Inherited', description='Desc', price=Decimal('5.00'),
            business=cls.profile, category=cls.cat_promo,
        )

        # Category with no promotion at all: should be excluded entirely.
        cls.cat_none = ProductCategory.objects.create(
            business=cls.profile, name='Sin promo', icon='icon',
        )
        Product.objects.create(
            name='No promo product', description='Desc', price=Decimal('1.00'),
            business=cls.profile, category=cls.cat_none,
        )

    def test_only_promoted_products_shown_in_mixed_category(self):
        client = self.auth_client(self.user)
        response = client.get('/api/v1/products/product-categories/with_products/', {'has_promotion': 'true'})
        self.assertEqual(response.status_code, 200)

        results = {cat['name']: cat for cat in response.data['results']}
        self.assertNotIn('Sin promo', results)

        mixed_product_ids = {p['id'] for p in results['Galletas']['products']}
        self.assertEqual(mixed_product_ids, {self.promoted_product.id})

    def test_all_products_shown_when_category_has_active_promotion(self):
        client = self.auth_client(self.user)
        response = client.get('/api/v1/products/product-categories/with_products/', {'has_promotion': 'true'})

        results = {cat['name']: cat for cat in response.data['results']}
        promo_product_ids = {p['id'] for p in results['Bebidas']['products']}
        self.assertEqual(promo_product_ids, {self.inherited_product.id})

    def test_products_count_reflects_only_promoted_products(self):
        client = self.auth_client(self.user)
        response = client.get('/api/v1/products/product-categories/with_products/', {'has_promotion': 'true'})
        # promoted_product (Galletas) + inherited_product (Bebidas) = 2
        self.assertEqual(response.data['products_count'], 2)

    def test_blank_multibuy_option_is_not_a_promotion(self):
        # The product form saves "no offer" as '' rather than NULL; an empty
        # string must not count as an active promotion in the filter or in
        # the pill counts.
        blank_cat = ProductCategory.objects.create(
            business=self.profile, name='Blank multibuy cat', icon='icon', multibuy_option='',
        )
        blank_product = Product.objects.create(
            name='Blank multibuy', description='Desc', price=Decimal('1.00'),
            business=self.profile, category=self.cat_mixed, multibuy_option='',
        )
        blank_in_blank_cat = Product.objects.create(
            name='Blank in blank cat', description='Desc', price=Decimal('1.00'),
            business=self.profile, category=blank_cat, multibuy_option='',
        )

        client = self.auth_client(self.user)
        response = client.get(
            '/api/v1/products/product-categories/with_products/',
            {'has_promotion': 'true', 'include_counts': 'true'},
        )

        results = {cat['name']: cat for cat in response.data['results']}
        self.assertNotIn('Blank multibuy cat', results)
        shown_ids = {p['id'] for cat in results.values() for p in cat['products']}
        self.assertNotIn(blank_product.id, shown_ids)
        self.assertNotIn(blank_in_blank_cat.id, shown_ids)
        self.assertEqual(response.data['products_count'], 2)
        self.assertEqual(response.data['filter_counts']['promotion'], 2)

    def test_without_filter_all_products_shown(self):
        client = self.auth_client(self.user)
        response = client.get('/api/v1/products/product-categories/with_products/')
        results = {cat['name']: cat for cat in response.data['results']}
        mixed_product_ids = {p['id'] for p in results['Galletas']['products']}
        self.assertEqual(
            mixed_product_ids,
            {self.promoted_product.id, self.unpromoted_product_1.id, self.unpromoted_product_2.id},
        )


class TestWithProductsFeaturedFilter(MarkyAPITestCase):
    """has_featured=true keeps only FAVORITE/RECOMMENDED products; combined with
    has_promotion the same product must satisfy both."""

    URL = '/api/v1/products/product-categories/with_products/'

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.user, cls.profile = cls.make_user('featured_user', 'featured@test.com')

        def make(name, category, **kwargs):
            return Product.objects.create(
                name=name, description='Desc', price=Decimal('3.00'),
                business=cls.profile, category=category, **kwargs,
            )

        # Favorite + Recommended + a plain one + a promoted-only one.
        cls.cat_a = ProductCategory.objects.create(business=cls.profile, name='A', icon='icon')
        cls.fav = make('Fav', cls.cat_a, stopper='FAVORITE')
        cls.rec = make('Rec', cls.cat_a, stopper='RECOMMENDED')
        cls.plain = make('Plain', cls.cat_a)
        cls.promo_only = make('PromoOnly', cls.cat_a, discount_percentage=Decimal('20.00'))

        # Featured product that is also on promotion.
        cls.cat_b = ProductCategory.objects.create(business=cls.profile, name='B', icon='icon')
        cls.fav_promo = make('FavPromo', cls.cat_b, stopper='FAVORITE', discount_percentage=Decimal('10.00'))

        # One promoted product and a different featured product: the category
        # must NOT match the combined filter.
        cls.cat_c = ProductCategory.objects.create(business=cls.profile, name='C', icon='icon')
        cls.c_promo = make('CPromo', cls.cat_c, discount_percentage=Decimal('15.00'))
        cls.c_fav = make('CFav', cls.cat_c, stopper='FAVORITE')

        # Featured product inheriting its promotion from the category.
        cls.cat_d = ProductCategory.objects.create(
            business=cls.profile, name='D', icon='icon', discount_percentage=Decimal('5.00'),
        )
        cls.d_rec = make('DRec', cls.cat_d, stopper='RECOMMENDED')
        cls.d_plain = make('DPlain', cls.cat_d)

        # No featured products at all.
        cls.cat_e = ProductCategory.objects.create(business=cls.profile, name='E', icon='icon')
        make('EPlain', cls.cat_e)

        # Uncategorized featured + plain product.
        cls.u_fav = make('UFav', None, stopper='FAVORITE')
        make('UPlain', None)

    def _get(self, **params):
        response = self.auth_client(self.user).get(self.URL, params)
        self.assertEqual(response.status_code, 200)
        return response.data

    @staticmethod
    def _by_category(data):
        return {c['name']: {p['id'] for p in c['products']} for c in data['results']}

    def test_featured_only_returns_favorite_and_recommended(self):
        data = self._get(has_featured='true')
        cats = self._by_category(data)
        self.assertEqual(cats['A'], {self.fav.id, self.rec.id})
        self.assertEqual(cats['B'], {self.fav_promo.id})
        self.assertEqual(cats['C'], {self.c_fav.id})
        self.assertEqual(cats['D'], {self.d_rec.id})
        self.assertEqual(cats['Sin categoría'], {self.u_fav.id})

    def test_featured_removes_categories_without_featured_products(self):
        cats = self._by_category(self._get(has_featured='true'))
        self.assertNotIn('E', cats)

    def test_featured_products_count_includes_uncategorized(self):
        # fav, rec, fav_promo, c_fav, d_rec, u_fav
        self.assertEqual(self._get(has_featured='true')['products_count'], 6)

    def test_featured_keeps_display_order(self):
        data = self._get(has_featured='true')
        a = next(c for c in data['results'] if c['name'] == 'A')
        self.assertEqual([p['id'] for p in a['products']], [self.fav.id, self.rec.id])

    def test_combined_filters_require_both_on_same_product(self):
        data = self._get(has_featured='true', has_promotion='true')
        cats = self._by_category(data)
        self.assertEqual(cats['B'], {self.fav_promo.id})
        # Inherited category promotion counts as an effective promotion.
        self.assertEqual(cats['D'], {self.d_rec.id})
        # Category C has a promoted product and a featured product, but no
        # single product that is both.
        self.assertNotIn('C', cats)
        self.assertNotIn('A', cats)
        self.assertNotIn('Sin categoría', cats)
        self.assertEqual(data['products_count'], 2)

    def test_promotion_only_regression(self):
        data = self._get(has_promotion='true')
        cats = self._by_category(data)
        self.assertEqual(cats['A'], {self.promo_only.id})
        self.assertEqual(cats['C'], {self.c_promo.id})
        self.assertEqual(cats['D'], {self.d_rec.id, self.d_plain.id})

    def test_absent_and_false_preserve_unfiltered_results(self):
        absent = self._get()
        false = self._get(has_featured='false')
        self.assertEqual(absent['products_count'], false['products_count'])
        self.assertEqual(self._by_category(absent), self._by_category(false))
        self.assertEqual(absent['products_count'], 12)
        self.assertNotIn('filter_counts', absent)

    def test_filter_counts_are_opt_in_and_ignore_toggles(self):
        data = self._get(include_counts='true', has_featured='true')
        # promoted: promo_only, fav_promo, c_promo, d_rec, d_plain
        self.assertEqual(data['filter_counts'], {'promotion': 5, 'featured': 6})


class TestWithProductsUncategorizedCount(MarkyAPITestCase):
    """products_count must include products with no category (the synthetic
    'Sin categoría' bucket), not just products belonging to a real
    ProductCategory row."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.user, cls.profile = cls.make_user('uncategorized_user', 'uncategorized@test.com')

    def test_products_count_when_only_uncategorized_products_exist(self):
        # A business whose only product has no category at all (e.g. the very
        # first product ever added, before any category exists).
        Product.objects.create(
            name='Uncategorized', description='Desc', price=Decimal('4.00'),
            business=self.profile,
        )
        client = self.auth_client(self.user)
        response = client.get('/api/v1/products/product-categories/with_products/')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['products_count'], 1)
        results = {cat['name']: cat for cat in response.data['results']}
        self.assertIn('Sin categoría', results)

    def test_products_count_includes_both_categorized_and_uncategorized(self):
        category = ProductCategory.objects.create(
            business=self.profile, name='Postres', icon='icon',
        )
        Product.objects.create(
            name='Categorized', description='Desc', price=Decimal('3.00'),
            business=self.profile, category=category,
        )
        Product.objects.create(
            name='Uncategorized', description='Desc', price=Decimal('4.00'),
            business=self.profile,
        )
        client = self.auth_client(self.user)
        response = client.get('/api/v1/products/product-categories/with_products/')

        self.assertEqual(response.data['products_count'], 2)


class TestProductTenancy(MarkyAPITestCase):

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.user_a, cls.profile_a = cls.make_user('tenant_a', 'tenant_a@test.com')
        cls.user_b, cls.profile_b = cls.make_user('tenant_b', 'tenant_b@test.com')
        cls.user_no_profile, _ = cls.make_user('no_profile', 'no_profile@test.com', with_profile=False)

        cls.product_a = Product.objects.create(
            name='Product A', description='Desc', price=Decimal('10.00'), business=cls.profile_a,
        )
        cls.product_b = Product.objects.create(
            name='Product B', description='Desc', price=Decimal('20.00'), business=cls.profile_b,
        )

    def test_list_returns_only_own_products(self):
        client = self.auth_client(self.user_a)
        response = client.get('/api/v1/products/products/')
        ids = [p['id'] for p in response.data['results']]
        self.assertIn(self.product_a.id, ids)
        self.assertNotIn(self.product_b.id, ids)

    def test_retrieve_other_user_product_returns_404(self):
        client = self.auth_client(self.user_a)
        response = client.get(f'/api/v1/products/products/{self.product_b.id}/')
        self.assertEqual(response.status_code, 404)

    def test_update_other_user_product_returns_404(self):
        client = self.auth_client(self.user_a)
        response = client.patch(
            f'/api/v1/products/products/{self.product_b.id}/',
            {'name': 'Hacked'},
            format='json',
        )
        self.assertEqual(response.status_code, 404)

    def test_delete_other_user_product_returns_404(self):
        client = self.auth_client(self.user_a)
        response = client.delete(f'/api/v1/products/products/{self.product_b.id}/')
        self.assertEqual(response.status_code, 404)

    def test_user_without_business_profile_gets_empty_list(self):
        client = self.auth_client(self.user_no_profile)
        response = client.get('/api/v1/products/products/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['results'], [])

    def test_unauthenticated_request_returns_401(self):
        response = self.client.get('/api/v1/products/products/')
        self.assertEqual(response.status_code, 401)

    def test_non_business_user_returns_403(self):
        plain_user = User.objects.create_user(
            username='plain_user', email='plain@test.com', password='TestPass123!', is_verified=True,
        )
        client = self.auth_client(plain_user)
        response = client.get('/api/v1/products/products/')
        self.assertEqual(response.status_code, 403)


class TestProductCategoryTenancy(MarkyAPITestCase):

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.user_a, cls.profile_a = cls.make_user('cat_tenant_a', 'cat_a@test.com')
        cls.user_b, cls.profile_b = cls.make_user('cat_tenant_b', 'cat_b@test.com')

        cls.cat_a = ProductCategory.objects.create(
            business=cls.profile_a, name='Cat A', icon='icon',
        )
        cls.cat_b = ProductCategory.objects.create(
            business=cls.profile_b, name='Cat B', icon='icon',
        )

    def test_list_returns_only_own_categories(self):
        client = self.auth_client(self.user_a)
        response = client.get('/api/v1/products/product-categories/')
        ids = [c['id'] for c in response.data['results']]
        self.assertIn(self.cat_a.id, ids)
        self.assertNotIn(self.cat_b.id, ids)

    def test_retrieve_other_user_category_returns_404(self):
        client = self.auth_client(self.user_a)
        response = client.get(f'/api/v1/products/product-categories/{self.cat_b.id}/')
        self.assertEqual(response.status_code, 404)

    def test_update_other_user_category_returns_404(self):
        client = self.auth_client(self.user_a)
        response = client.patch(
            f'/api/v1/products/product-categories/{self.cat_b.id}/',
            {'name': 'Hacked'},
            format='json',
        )
        self.assertEqual(response.status_code, 404)


class TestProductInputSerializerTenancy(MarkyAPITestCase):

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.user_a, cls.profile_a = cls.make_user('input_tenant_a', 'input_tenant_a@test.com')
        cls.user_b, cls.profile_b = cls.make_user('input_tenant_b', 'input_tenant_b@test.com')

        cls.cat_a = ProductCategory.objects.create(business=cls.profile_a, name='Cat A', icon='icon')
        cls.cat_b = ProductCategory.objects.create(business=cls.profile_b, name='Cat B', icon='icon')

        cls.favorite_b = Product.objects.create(
            name='Secret Favorite', description='Desc', price=Decimal('10.00'),
            business=cls.profile_b, category=cls.cat_b, stopper='FAVORITE',
        )

    def test_cannot_assign_product_to_another_tenants_category(self):
        client = self.auth_client(self.user_a)
        response = client.post(
            '/api/v1/products/products/',
            {
                'name': 'New Product', 'description': 'Desc', 'price': '5.00',
                'category': self.cat_b.id, 'stopper': 'FAVORITE',
            },
            format='multipart',
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn('category', response.data)
        # Must be rejected before the FAVORITE-conflict check ever runs, so
        # tenant B's product name can never reach tenant A's response.
        self.assertNotIn('Secret Favorite', str(response.data))


class TestProductFavoriteStopperConstraint(MarkyAPITestCase):
    """DB-level backstop for the FAVORITE-per-category uniqueness rule.

    The serializer's read-then-write check has a TOCTOU gap under concurrent
    requests; a DB constraint guarantees the invariant regardless of races.
    """

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.user, cls.profile = cls.make_user('constraint_tenant', 'constraint_tenant@test.com')
        cls.category = ProductCategory.objects.create(business=cls.profile, name='Cat', icon='icon')

    def test_second_favorite_in_same_category_raises_integrity_error(self):
        from django.db import IntegrityError

        Product.objects.create(
            name='First Favorite', description='Desc', price=Decimal('10.00'),
            business=self.profile, category=self.category, stopper='FAVORITE',
        )
        with self.assertRaises(IntegrityError):
            Product.objects.create(
                name='Second Favorite', description='Desc', price=Decimal('20.00'),
                business=self.profile, category=self.category, stopper='FAVORITE',
            )

    def test_multiple_non_favorite_products_allowed_in_same_category(self):
        Product.objects.create(
            name='Regular 1', description='Desc', price=Decimal('10.00'),
            business=self.profile, category=self.category,
        )
        Product.objects.create(
            name='Regular 2', description='Desc', price=Decimal('20.00'),
            business=self.profile, category=self.category,
        )
        self.assertEqual(Product.objects.filter(category=self.category).count(), 2)

    def test_favorite_allowed_in_different_categories(self):
        other_category = ProductCategory.objects.create(business=self.profile, name='Other', icon='icon')
        Product.objects.create(
            name='Favorite 1', description='Desc', price=Decimal('10.00'),
            business=self.profile, category=self.category, stopper='FAVORITE',
        )
        Product.objects.create(
            name='Favorite 2', description='Desc', price=Decimal('20.00'),
            business=self.profile, category=other_category, stopper='FAVORITE',
        )
        self.assertEqual(Product.objects.filter(stopper='FAVORITE').count(), 2)

    def test_race_past_serializer_check_still_returns_validation_error_not_500(self):
        """Simulate the TOCTOU window: two requests both pass validate() because
        neither commit is visible to the other yet, so the DB constraint is the
        only thing standing between them. The second create() call should turn
        the resulting IntegrityError into a clean DRF ValidationError, not a 500.
        """
        from products.serializers import ProductInputSerializer

        Product.objects.create(
            name='First Favorite', description='Desc', price=Decimal('10.00'),
            business=self.profile, category=self.category, stopper='FAVORITE',
        )

        serializer = ProductInputSerializer()
        with self.assertRaises(serializers.ValidationError):
            serializer.create({
                'name': 'Second Favorite', 'description': 'Desc', 'price': Decimal('20.00'),
                'business': self.profile, 'category': self.category, 'stopper': 'FAVORITE',
            })


class TestProductRecommendedStopperConstraint(MarkyAPITestCase):
    """DB-level backstop for the RECOMMENDED-per-category uniqueness rule.

    Mirrors TestProductFavoriteStopperConstraint: the serializer's
    read-then-write check has a TOCTOU gap under concurrent requests; a DB
    constraint guarantees the invariant regardless of races.
    """

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.user, cls.profile = cls.make_user('recommended_tenant', 'recommended_tenant@test.com')
        cls.category = ProductCategory.objects.create(business=cls.profile, name='Cat', icon='icon')

    def test_second_recommended_in_same_category_raises_integrity_error(self):
        from django.db import IntegrityError

        Product.objects.create(
            name='First Recommended', description='Desc', price=Decimal('10.00'),
            business=self.profile, category=self.category, stopper='RECOMMENDED',
        )
        with self.assertRaises(IntegrityError):
            Product.objects.create(
                name='Second Recommended', description='Desc', price=Decimal('20.00'),
                business=self.profile, category=self.category, stopper='RECOMMENDED',
            )

    def test_multiple_non_recommended_products_allowed_in_same_category(self):
        Product.objects.create(
            name='Regular 1', description='Desc', price=Decimal('10.00'),
            business=self.profile, category=self.category,
        )
        Product.objects.create(
            name='Regular 2', description='Desc', price=Decimal('20.00'),
            business=self.profile, category=self.category,
        )
        self.assertEqual(Product.objects.filter(category=self.category).count(), 2)

    def test_recommended_allowed_in_different_categories(self):
        other_category = ProductCategory.objects.create(business=self.profile, name='Other', icon='icon')
        Product.objects.create(
            name='Recommended 1', description='Desc', price=Decimal('10.00'),
            business=self.profile, category=self.category, stopper='RECOMMENDED',
        )
        Product.objects.create(
            name='Recommended 2', description='Desc', price=Decimal('20.00'),
            business=self.profile, category=other_category, stopper='RECOMMENDED',
        )
        self.assertEqual(Product.objects.filter(stopper='RECOMMENDED').count(), 2)

    def test_favorite_and_recommended_can_coexist_in_same_category(self):
        """The stopper field itself already prevents a product from being both
        FAVORITE and RECOMMENDED at once; this confirms the two constraints
        don't interfere with each other when different products in the same
        category hold different stopper values."""
        Product.objects.create(
            name='The Favorite', description='Desc', price=Decimal('10.00'),
            business=self.profile, category=self.category, stopper='FAVORITE',
        )
        Product.objects.create(
            name='The Recommended', description='Desc', price=Decimal('20.00'),
            business=self.profile, category=self.category, stopper='RECOMMENDED',
        )
        self.assertEqual(Product.objects.filter(category=self.category).count(), 2)

    def test_race_past_serializer_check_still_returns_validation_error_not_500(self):
        """Simulate the TOCTOU window: two requests both pass validate() because
        neither commit is visible to the other yet, so the DB constraint is the
        only thing standing between them. The second create() call should turn
        the resulting IntegrityError into a clean DRF ValidationError, not a 500.
        """
        from products.serializers import ProductInputSerializer

        Product.objects.create(
            name='First Recommended', description='Desc', price=Decimal('10.00'),
            business=self.profile, category=self.category, stopper='RECOMMENDED',
        )

        serializer = ProductInputSerializer()
        with self.assertRaises(serializers.ValidationError):
            serializer.create({
                'name': 'Second Recommended', 'description': 'Desc', 'price': Decimal('20.00'),
                'business': self.profile, 'category': self.category, 'stopper': 'RECOMMENDED',
            })


class TestProductMediaValidators(SimpleTestCase):

    def test_oversized_image_rejected(self):
        f = SimpleUploadedFile('photo.jpg', b'x' * (5 * 1024 * 1024 + 1), content_type='image/jpeg')
        with self.assertRaises(ValidationError):
            validate_media_size(f)

    def test_image_at_exactly_max_size_accepted(self):
        f = SimpleUploadedFile('photo.jpg', b'x' * (5 * 1024 * 1024), content_type='image/jpeg')
        validate_media_size(f)  # should not raise

    def test_oversized_video_rejected(self):
        f = SimpleUploadedFile('clip.mp4', b'x' * (80 * 1024 * 1024 + 1), content_type='video/mp4')
        with self.assertRaises(ValidationError):
            validate_media_size(f)

    def test_video_at_exactly_max_size_accepted(self):
        f = SimpleUploadedFile('clip.mp4', b'x' * (80 * 1024 * 1024), content_type='video/mp4')
        validate_media_size(f)  # should not raise

    def test_small_video_not_held_to_image_limit(self):
        # A 6MB video is over the image cap but well under the video cap —
        # confirms size limits are chosen by extension, not a single shared cap.
        f = SimpleUploadedFile('clip.webm', b'x' * (6 * 1024 * 1024), content_type='video/webm')
        validate_media_size(f)  # should not raise

    def test_disallowed_extension_rejected(self):
        f = SimpleUploadedFile('malware.exe', b'x', content_type='application/octet-stream')
        with self.assertRaises(ValidationError):
            validate_media_extension(f)

    def test_disallowed_video_extension_rejected(self):
        f = SimpleUploadedFile('clip.avi', b'x', content_type='video/x-msvideo')
        with self.assertRaises(ValidationError):
            validate_media_extension(f)

    def test_each_allowed_extension_accepted(self):
        for ext, content_type in [
            ('.jpg', 'image/jpeg'), ('.jpeg', 'image/jpeg'), ('.png', 'image/png'),
            ('.webp', 'image/webp'), ('.mp4', 'video/mp4'), ('.mov', 'video/quicktime'),
            ('.webm', 'video/webm'),
        ]:
            f = SimpleUploadedFile(f'file{ext}', b'x', content_type=content_type)
            validate_media_extension(f)  # should not raise


class TestProductMediaUploadAPI(MarkyAPITestCase):

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.user, cls.profile = cls.make_user('media_tenant', 'media_tenant@test.com')

    def _create_payload(self, file_):
        return {
            'name': 'Product With Media', 'description': 'Desc', 'price': '10.00',
            'media[0][file]': file_, 'media[0][media_type]': 'image', 'media[0][order]': '0',
        }

    def test_oversized_image_rejected_by_api(self):
        client = self.auth_client(self.user)
        big_file = SimpleUploadedFile(
            'photo.jpg', b'x' * (5 * 1024 * 1024 + 1), content_type='image/jpeg',
        )
        response = client.post(
            '/api/v1/products/products/', self._create_payload(big_file), format='multipart',
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(Product.objects.filter(name='Product With Media').exists())

    def test_disallowed_extension_rejected_by_api(self):
        client = self.auth_client(self.user)
        bad_file = SimpleUploadedFile('malware.exe', b'x', content_type='application/octet-stream')
        response = client.post(
            '/api/v1/products/products/', self._create_payload(bad_file), format='multipart',
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(Product.objects.filter(name='Product With Media').exists())

    def test_valid_image_accepted_by_api(self):
        client = self.auth_client(self.user)
        good_file = SimpleUploadedFile('photo.jpg', b'x' * 1024, content_type='image/jpeg')
        response = client.post(
            '/api/v1/products/products/', self._create_payload(good_file), format='multipart',
        )
        self.assertEqual(response.status_code, 201)


def _make_test_image(name='variant.png'):
    """Minimal real PNG bytes — ProductVariant.image is a Pillow-backed
    ImageField, unlike ProductMedia.file, so a valid image is required."""
    import io
    from PIL import Image

    buffer = io.BytesIO()
    Image.new('RGB', (1, 1), color='red').save(buffer, format='PNG')
    buffer.seek(0)
    return SimpleUploadedFile(name, buffer.read(), content_type='image/png')


class TestProductVariantImageRequired(MarkyAPITestCase):
    """Ticket 5: every product variant must have an image."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.user, cls.profile = cls.make_user('variant_tenant', 'variant_tenant@test.com')

    def _create_payload(self, **variant_overrides):
        payload = {
            'name': 'Product With Variants', 'description': 'Desc', 'price': '10.00',
            'variants[0][name]': 'Chico', 'variants[0][price]': '5.00',
        }
        for key, value in variant_overrides.items():
            payload[f'variants[0][{key}]'] = value
        return payload

    def test_new_variant_without_image_rejected_by_api(self):
        client = self.auth_client(self.user)
        response = client.post(
            '/api/v1/products/products/', self._create_payload(), format='multipart',
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(Product.objects.filter(name='Product With Variants').exists())

    def test_new_variant_with_image_accepted_by_api(self):
        client = self.auth_client(self.user)
        response = client.post(
            '/api/v1/products/products/',
            self._create_payload(image=_make_test_image()),
            format='multipart',
        )
        self.assertEqual(response.status_code, 201)
        product = Product.objects.get(name='Product With Variants')
        self.assertEqual(product.variants.count(), 1)
        self.assertTrue(product.variants.first().image)

    def test_existing_variant_update_without_resending_image_is_not_invalidated(self):
        client = self.auth_client(self.user)
        product = Product.objects.create(
            name='Existing Product', description='Desc', price=Decimal('10.00'), business=self.profile,
        )
        variant = ProductVariant.objects.create(
            product=product, name='Chico', price=Decimal('5.00'), image=_make_test_image(),
        )
        response = client.patch(
            f'/api/v1/products/products/{product.id}/',
            {'variants[0][id]': str(variant.id), 'variants[0][name]': 'Chico actualizado'},
            format='multipart',
        )
        self.assertEqual(response.status_code, 200)
        variant.refresh_from_db()
        self.assertEqual(variant.name, 'Chico actualizado')
        self.assertTrue(variant.image)

    def test_legacy_variant_without_image_not_invalidated_by_unrelated_update(self):
        # Grandfathered row: created directly (bypassing the API) before this
        # rule existed, so it has no image on disk.
        client = self.auth_client(self.user)
        product = Product.objects.create(
            name='Legacy Product', description='Desc', price=Decimal('10.00'), business=self.profile,
        )
        variant = ProductVariant.objects.create(product=product, name='Chico', price=Decimal('5.00'))
        response = client.patch(
            f'/api/v1/products/products/{product.id}/',
            {'variants[0][id]': str(variant.id), 'variants[0][price]': '6.00'},
            format='multipart',
        )
        self.assertEqual(response.status_code, 200)
        variant.refresh_from_db()
        self.assertEqual(variant.price, Decimal('6.00'))

    def test_delete_flag_on_id_less_variant_does_not_bypass_image_requirement(self):
        # `_delete` only has meaning for an *existing* variant (one with an
        # `id`) — process_related() ignores it entirely for id-less items and
        # always creates them. A payload that sets `_delete` without an `id`
        # must still be rejected for missing an image, not waved through.
        client = self.auth_client(self.user)
        response = client.post(
            '/api/v1/products/products/',
            {
                **self._create_payload(),
                'variants[0][_delete]': 'true',
            },
            format='multipart',
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(Product.objects.filter(name='Product With Variants').exists())


class TestProductVariantNestedWrite(MarkyAPITestCase):
    """Regression coverage for the frontend sending a fake client-side id
    (Date.now()) on new variant rows: process_related() treats any item with
    an `id` as an update against an existing row, so a fake id made new
    variants silently vanish instead of being created. Id-less items must
    always be created with a DB-allocated pk, and deletion must go through
    the `_delete` tombstone rather than merely disappearing from the array.
    """

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.user, cls.profile = cls.make_user('variant_write_tenant', 'variant_write_tenant@test.com')

    def test_id_less_variant_created_with_db_allocated_pk_not_client_value(self):
        client = self.auth_client(self.user)
        response = client.post(
            '/api/v1/products/products/',
            {
                'name': 'New Product', 'description': 'Desc', 'price': '10.00',
                'variants[0][name]': 'Chico', 'variants[0][price]': '5.00',
                'variants[0][image]': _make_test_image(),
            },
            format='multipart',
        )
        self.assertEqual(response.status_code, 201)
        product = Product.objects.get(name='New Product')
        variant = product.variants.get()
        self.assertEqual(variant.name, 'Chico')

    def test_id_less_variant_added_on_update_is_created_alongside_existing(self):
        client = self.auth_client(self.user)
        product = Product.objects.create(
            name='Existing Product', description='Desc', price=Decimal('10.00'), business=self.profile,
        )
        existing = ProductVariant.objects.create(
            product=product, name='Chico', price=Decimal('5.00'), image=_make_test_image(),
        )
        response = client.patch(
            f'/api/v1/products/products/{product.id}/',
            {
                'variants[0][id]': str(existing.id), 'variants[0][name]': 'Chico',
                'variants[1][name]': 'Grande', 'variants[1][price]': '8.00',
                'variants[1][image]': _make_test_image(),
            },
            format='multipart',
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(product.variants.count(), 2)
        self.assertTrue(product.variants.filter(name='Grande').exists())

    def test_tombstone_deletes_existing_variant_without_resending_image(self):
        client = self.auth_client(self.user)
        product = Product.objects.create(
            name='Existing Product', description='Desc', price=Decimal('10.00'), business=self.profile,
        )
        variant = ProductVariant.objects.create(
            product=product, name='Chico', price=Decimal('5.00'), image=_make_test_image(),
        )
        response = client.patch(
            f'/api/v1/products/products/{product.id}/',
            {'variants[0][id]': str(variant.id), 'variants[0][_delete]': 'true'},
            format='multipart',
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(ProductVariant.objects.filter(id=variant.id).exists())

    def test_omitting_variants_entirely_leaves_them_untouched(self):
        client = self.auth_client(self.user)
        product = Product.objects.create(
            name='Existing Product', description='Desc', price=Decimal('10.00'), business=self.profile,
        )
        ProductVariant.objects.create(
            product=product, name='Chico', price=Decimal('5.00'), image=_make_test_image(),
        )
        response = client.patch(
            f'/api/v1/products/products/{product.id}/',
            {'price': '12.00'},
            format='multipart',
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(product.variants.count(), 1)


class TestProductAddonNestedWrite(MarkyAPITestCase):

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.user, cls.profile = cls.make_user('addon_write_tenant', 'addon_write_tenant@test.com')

    def test_id_less_addon_created_on_create(self):
        client = self.auth_client(self.user)
        response = client.post(
            '/api/v1/products/products/',
            {
                'name': 'New Product', 'description': 'Desc', 'price': '10.00',
                'addons[0][name]': 'Queso extra', 'addons[0][price]': '2.00',
            },
            format='multipart',
        )
        self.assertEqual(response.status_code, 201)
        product = Product.objects.get(name='New Product')
        self.assertEqual(product.addons.count(), 1)
        self.assertEqual(product.addons.get().name, 'Queso extra')

    def test_id_less_addon_added_on_update_is_created_alongside_existing(self):
        client = self.auth_client(self.user)
        product = Product.objects.create(
            name='Existing Product', description='Desc', price=Decimal('10.00'), business=self.profile,
        )
        existing = ProductAddon.objects.create(product=product, name='Queso extra', price=Decimal('2.00'))
        response = client.patch(
            f'/api/v1/products/products/{product.id}/',
            {
                'addons[0][id]': str(existing.id), 'addons[0][name]': 'Queso extra',
                'addons[1][name]': 'Tocino', 'addons[1][price]': '3.00',
            },
            format='multipart',
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(product.addons.count(), 2)
        self.assertTrue(product.addons.filter(name='Tocino').exists())

    def test_tombstone_deletes_existing_addon_without_resending_name_or_price(self):
        client = self.auth_client(self.user)
        product = Product.objects.create(
            name='Existing Product', description='Desc', price=Decimal('10.00'), business=self.profile,
        )
        addon = ProductAddon.objects.create(product=product, name='Queso extra', price=Decimal('2.00'))
        response = client.patch(
            f'/api/v1/products/products/{product.id}/',
            {'addons[0][id]': str(addon.id), 'addons[0][_delete]': 'true'},
            format='multipart',
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(ProductAddon.objects.filter(id=addon.id).exists())

    def test_update_existing_addon_name_and_price_in_place(self):
        client = self.auth_client(self.user)
        product = Product.objects.create(
            name='Existing Product', description='Desc', price=Decimal('10.00'), business=self.profile,
        )
        addon = ProductAddon.objects.create(product=product, name='Queso extra', price=Decimal('2.00'))
        response = client.patch(
            f'/api/v1/products/products/{product.id}/',
            {'addons[0][id]': str(addon.id), 'addons[0][name]': 'Queso premium', 'addons[0][price]': '3.50'},
            format='multipart',
        )
        self.assertEqual(response.status_code, 200)
        addon.refresh_from_db()
        self.assertEqual(addon.name, 'Queso premium')
        self.assertEqual(addon.price, Decimal('3.50'))

    def test_omitting_addons_entirely_leaves_them_untouched(self):
        client = self.auth_client(self.user)
        product = Product.objects.create(
            name='Existing Product', description='Desc', price=Decimal('10.00'), business=self.profile,
        )
        ProductAddon.objects.create(product=product, name='Queso extra', price=Decimal('2.00'))
        response = client.patch(
            f'/api/v1/products/products/{product.id}/',
            {'price': '12.00'},
            format='multipart',
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(product.addons.count(), 1)


class TestProductStopperIntegrityErrorBackstop(MarkyAPITestCase):
    """update()'s IntegrityError backstop (previously missing entirely — only
    create() had one) and the disambiguation that keeps an unrelated
    IntegrityError from being mislabeled as a stopper conflict in either
    method.
    """

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.user, cls.profile = cls.make_user('backstop_tenant', 'backstop_tenant@test.com')
        cls.category = ProductCategory.objects.create(business=cls.profile, name='Cat', icon='icon')

    def test_update_race_past_serializer_check_still_returns_validation_error_not_500(self):
        """Mirrors the equivalent create() race test: two updates both pass
        validate() before either commits, so the DB constraint is the only
        thing standing between them. update() must turn the resulting
        IntegrityError into a clean ValidationError, not a 500.
        """
        from products.serializers import ProductInputSerializer

        Product.objects.create(
            name='First Recommended', description='Desc', price=Decimal('10.00'),
            business=self.profile, category=self.category, stopper='RECOMMENDED',
        )
        other = Product.objects.create(
            name='Not Recommended Yet', description='Desc', price=Decimal('20.00'),
            business=self.profile, category=self.category,
        )

        serializer = ProductInputSerializer()
        with self.assertRaises(serializers.ValidationError):
            serializer.update(other, {'stopper': 'RECOMMENDED'})

    def test_create_unrelated_integrity_error_is_not_mislabeled_as_stopper_conflict(self):
        """If create() hits an IntegrityError that isn't actually a stopper
        conflict (e.g. a concurrently-deleted category causing an FK
        violation), it must propagate as-is rather than being reported as a
        fake 'Ya existe un Recomendado...' conflict."""
        from unittest.mock import patch
        from django.db import IntegrityError
        from products.serializers import ProductInputSerializer

        serializer = ProductInputSerializer()
        with patch(
            'products.serializers.Product.objects.create',
            side_effect=IntegrityError('unrelated constraint violation'),
        ):
            with self.assertRaises(IntegrityError):
                serializer.create({
                    'name': 'X', 'description': 'Desc', 'price': Decimal('10.00'),
                    'business': self.profile, 'category': self.category, 'stopper': 'RECOMMENDED',
                })

    def test_update_unrelated_integrity_error_is_not_mislabeled_as_stopper_conflict(self):
        """Same guarantee as above, for update()."""
        from unittest.mock import patch
        from django.db import IntegrityError
        from products.serializers import ProductInputSerializer

        product = Product.objects.create(
            name='X', description='Desc', price=Decimal('10.00'),
            business=self.profile, category=self.category,
        )

        serializer = ProductInputSerializer()
        with patch(
            'products.models.Product.save',
            side_effect=IntegrityError('unrelated constraint violation'),
        ):
            with self.assertRaises(IntegrityError):
                serializer.update(product, {'name': 'Y'})


# ---------------------------------------------------------------------------
# Promotion single-source-of-truth (Asana #8) — products/promotions.py and
# its wiring into ProductInputSerializer / ProductLiteSerializer / ProductSerializer.
# ---------------------------------------------------------------------------

class TestPromotionStatus(SimpleTestCase):
    """Pure unit tests for compute_promotion_status — no DB needed."""

    def test_inactive_when_nothing_configured(self):
        self.assertEqual(
            compute_promotion_status(None, Decimal('0'), None, None), INACTIVE,
        )

    def test_active_multibuy_only_no_dates(self):
        self.assertEqual(
            compute_promotion_status('2x1', Decimal('0'), None, None), ACTIVE,
        )

    def test_active_discount_only_no_dates(self):
        self.assertEqual(
            compute_promotion_status(None, Decimal('10.00'), None, None), ACTIVE,
        )

    def test_scheduled_when_start_in_future(self):
        now = timezone.now()
        self.assertEqual(
            compute_promotion_status(None, Decimal('10.00'), now + timedelta(days=1), None, now=now),
            SCHEDULED,
        )

    def test_expired_when_end_in_past(self):
        now = timezone.now()
        self.assertEqual(
            compute_promotion_status(None, Decimal('10.00'), None, now - timedelta(days=1), now=now),
            EXPIRED,
        )

    def test_active_within_window(self):
        now = timezone.now()
        self.assertEqual(
            compute_promotion_status(
                None, Decimal('10.00'), now - timedelta(hours=1), now + timedelta(hours=1), now=now,
            ),
            ACTIVE,
        )

    def test_expired_wins_even_if_window_is_otherwise_inverted(self):
        # Defensive: a passed end date means expired regardless of the start.
        now = timezone.now()
        self.assertEqual(
            compute_promotion_status(
                None, Decimal('10.00'), now - timedelta(days=2), now - timedelta(days=1), now=now,
            ),
            EXPIRED,
        )


class TestResolveEffectivePromotion(SimpleTestCase):
    """Category-vs-product bundling must never mix a category discount with
    the product's own dates or vice versa — the ticket's headline root cause."""

    def _category(self, multibuy_option=None, discount_percentage=Decimal('0'), starts_at=None, ends_at=None):
        return SimpleNamespace(
            multibuy_option=multibuy_option, discount_percentage=discount_percentage,
            promotion_starts_at=starts_at, promotion_ends_at=ends_at,
        )

    def _product(self, category=None, multibuy_option=None, discount_percentage=Decimal('0'),
                 starts_at=None, ends_at=None):
        return SimpleNamespace(
            category=category, multibuy_option=multibuy_option, discount_percentage=discount_percentage,
            promotion_starts_at=starts_at, promotion_ends_at=ends_at,
        )

    def test_active_product_promo_wins_over_category(self):
        now = timezone.now()
        # Both windows must independently resolve to ACTIVE — the category
        # is a genuine, undisqualified active candidate here, not one that's
        # disqualified by its own expiry (see test_product_wins_regardless_of_category_expiry
        # for that case).
        category = self._category(
            discount_percentage=Decimal('20.00'),
            starts_at=now - timedelta(hours=2), ends_at=now + timedelta(hours=2),
        )
        product = self._product(
            category=category, discount_percentage=Decimal('5.00'),
            starts_at=now - timedelta(hours=1), ends_at=now + timedelta(hours=1),
        )

        bundle = resolve_effective_promotion(product, now=now)

        self.assertEqual(bundle['source'], 'product')
        self.assertEqual(bundle['status'], ACTIVE)
        self.assertEqual(bundle['discount_percentage'], Decimal('5.00'))
        self.assertEqual(bundle['promotion_starts_at'], product.promotion_starts_at)
        self.assertEqual(bundle['promotion_ends_at'], product.promotion_ends_at)

    def test_product_wins_regardless_of_category_expiry(self):
        # This now passes via the product-first check itself, not via a
        # category-expiry fallback — the category's status is never even
        # consulted here since the product's own promo is already ACTIVE.
        now = timezone.now()
        category = self._category(discount_percentage=Decimal('20.00'), ends_at=now - timedelta(days=1))
        product = self._product(category=category, discount_percentage=Decimal('5.00'))

        bundle = resolve_effective_promotion(product, now=now)

        self.assertEqual(bundle['source'], 'product')
        self.assertEqual(bundle['status'], ACTIVE)
        self.assertEqual(bundle['discount_percentage'], Decimal('5.00'))

    def test_no_category_uses_product_own_bundle(self):
        product = self._product(category=None, discount_percentage=Decimal('5.00'))
        bundle = resolve_effective_promotion(product)
        self.assertEqual(bundle['source'], 'product')
        self.assertEqual(bundle['status'], ACTIVE)

    def test_scheduled_product_does_not_block_active_category(self):
        # A merely SCHEDULED product promo (configured but not started yet)
        # must not override a currently-live category discount — per PM
        # decision, the product only overrides once it is actually ACTIVE.
        now = timezone.now()
        category = self._category(
            discount_percentage=Decimal('20.00'),
            starts_at=now - timedelta(hours=1), ends_at=now + timedelta(hours=1),
        )
        product = self._product(
            category=category, discount_percentage=Decimal('5.00'), starts_at=now + timedelta(days=1),
        )

        bundle = resolve_effective_promotion(product, now=now)

        self.assertEqual(bundle['source'], 'category')
        self.assertEqual(bundle['status'], ACTIVE)
        self.assertEqual(bundle['discount_percentage'], Decimal('20.00'))
        self.assertEqual(bundle['promotion_starts_at'], category.promotion_starts_at)
        self.assertEqual(bundle['promotion_ends_at'], category.promotion_ends_at)

    def test_scheduled_product_wins_when_no_competing_category_promo(self):
        # A product's own scheduled promo still surfaces its "starts in X"
        # state via the final fallback when nothing else competes for it.
        now = timezone.now()
        category = self._category()  # nothing configured
        product = self._product(
            category=category, discount_percentage=Decimal('5.00'), starts_at=now + timedelta(days=1),
        )

        bundle = resolve_effective_promotion(product, now=now)

        self.assertEqual(bundle['source'], 'product')
        self.assertEqual(bundle['status'], SCHEDULED)
        self.assertEqual(bundle['discount_percentage'], Decimal('5.00'))
        self.assertEqual(bundle['promotion_starts_at'], product.promotion_starts_at)
        self.assertEqual(bundle['promotion_ends_at'], product.promotion_ends_at)

    def test_scheduled_category_promo_inherited_by_plain_product(self):
        now = timezone.now()
        category = self._category(
            discount_percentage=Decimal('15.00'),
            starts_at=now + timedelta(days=1), ends_at=now + timedelta(days=8),
        )
        product = self._product(category=category)  # no promo config of its own

        bundle = resolve_effective_promotion(product, now=now)

        self.assertEqual(bundle['source'], 'category')
        self.assertEqual(bundle['status'], SCHEDULED)
        self.assertEqual(bundle['discount_percentage'], Decimal('15.00'))

    def test_inactive_category_falls_back_to_product(self):
        # Trivially true under product-first priority too: the product's own
        # promo is ACTIVE, so the category (inactive or not) is never reached.
        category = self._category()  # nothing configured
        product = self._product(category=category, discount_percentage=Decimal('5.00'))

        bundle = resolve_effective_promotion(product)

        self.assertEqual(bundle['source'], 'product')
        self.assertEqual(bundle['status'], ACTIVE)

    def test_expired_product_promo_falls_back_to_active_category(self):
        now = timezone.now()
        category = self._category(
            discount_percentage=Decimal('20.00'),
            starts_at=now - timedelta(hours=1), ends_at=now + timedelta(hours=1),
        )
        product = self._product(
            category=category, discount_percentage=Decimal('5.00'),
            starts_at=now - timedelta(days=10), ends_at=now - timedelta(days=9),  # expired, not yet swept
        )

        bundle = resolve_effective_promotion(product, now=now)

        self.assertEqual(bundle['source'], 'category')
        self.assertEqual(bundle['status'], ACTIVE)
        self.assertEqual(bundle['discount_percentage'], Decimal('20.00'))

    def test_expired_product_and_no_competing_category_falls_back_to_product_source(self):
        # Kills the "fallback source flipped to category" mutant: with the
        # product's own promo EXPIRED and no category promo to compete, the
        # final fallback must still explicitly report source='product' (not
        # silently default to 'category').
        now = timezone.now()
        category = self._category()  # nothing configured
        product = self._product(
            category=category, discount_percentage=Decimal('5.00'),
            starts_at=now - timedelta(days=10), ends_at=now - timedelta(days=9),
        )

        bundle = resolve_effective_promotion(product, now=now)

        self.assertEqual(bundle['source'], 'product')
        self.assertEqual(bundle['status'], EXPIRED)
        self.assertEqual(bundle['discount_percentage'], Decimal('5.00'))

    def test_active_product_multibuy_wins_over_active_category_discount(self):
        now = timezone.now()
        category = self._category(
            discount_percentage=Decimal('30.00'),
            starts_at=now - timedelta(hours=2), ends_at=now + timedelta(hours=2),
        )
        product = self._product(
            category=category, multibuy_option='3x2',
            starts_at=now - timedelta(hours=1), ends_at=now + timedelta(hours=1),
        )

        bundle = resolve_effective_promotion(product, now=now)

        self.assertEqual(bundle['source'], 'product')
        self.assertEqual(bundle['multibuy_option'], '3x2')
        self.assertEqual(bundle['discount_percentage'], Decimal('0'))

    def test_active_product_discount_wins_over_active_category_multibuy(self):
        now = timezone.now()
        category = self._category(
            multibuy_option='3x2',
            starts_at=now - timedelta(hours=2), ends_at=now + timedelta(hours=2),
        )
        product = self._product(
            category=category, discount_percentage=Decimal('30.00'),
            starts_at=now - timedelta(hours=1), ends_at=now + timedelta(hours=1),
        )

        bundle = resolve_effective_promotion(product, now=now)

        self.assertEqual(bundle['source'], 'product')
        self.assertEqual(bundle['discount_percentage'], Decimal('30.00'))
        self.assertFalse(bundle['multibuy_option'])


class TestPromotionStatusFilterConsistency(MarkyAPITestCase):
    """compute_promotion_status() and filters.py::_promotion_active_q must
    agree on which window combinations count as 'currently active' — they're
    independently implemented, and the serializer side already drifted from
    the filter once (see products/promotions.py's module docstring)."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.user, cls.profile = cls.make_user('consistency_user', 'consistency@test.com')

    def test_status_agrees_with_orm_filter_across_date_combinations(self):
        now = timezone.now()
        matrix = [
            ('no_dates', None, None),
            ('future_start_only', now + timedelta(days=1), None),
            ('past_start_only', now - timedelta(days=1), None),
            ('future_end_only', None, now + timedelta(days=1)),
            ('past_end_only', None, now - timedelta(days=1)),
            ('within_window', now - timedelta(hours=1), now + timedelta(hours=1)),
            ('future_window', now + timedelta(days=1), now + timedelta(days=2)),
            ('past_window', now - timedelta(days=2), now - timedelta(days=1)),
        ]

        for label, starts_at, ends_at in matrix:
            with self.subTest(label=label):
                category = ProductCategory.objects.create(
                    business=self.profile, name=f'Cat {label}', icon='icon',
                    discount_percentage=Decimal('10.00'),
                    promotion_starts_at=starts_at, promotion_ends_at=ends_at,
                )
                status = compute_promotion_status(
                    category.multibuy_option, category.discount_percentage,
                    category.promotion_starts_at, category.promotion_ends_at, now=now,
                )
                matched_by_filter = ProductCategoryFilter(
                    {'has_promotion': 'true'},
                    queryset=ProductCategory.objects.filter(pk=category.pk),
                ).qs.exists()

                # has_promotion=true means "currently benefits from a promo
                # right now" — a scheduled (future-start) promo correctly
                # does not match it, so only ACTIVE should agree with the filter.
                self.assertEqual(
                    status == ACTIVE, matched_by_filter,
                    f'{label}: compute_promotion_status={status} but filter matched={matched_by_filter}',
                )


class TestProductInputSerializerPromotion(MarkyAPITestCase):
    """Regression coverage for the ticket's headline bug: an unrelated-field
    save must never touch promo config, and an explicit clear must actually
    clear it (including the multipart empty-string-vs-NULL quirk)."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.user, cls.profile = cls.make_user('promo_input_user', 'promo_input@test.com')

    def _create_promo_product(self, **overrides):
        now = timezone.now()
        defaults = dict(
            name='Promo Product', description='Desc', price=Decimal('10.00'),
            business=self.profile,
            discount_percentage=Decimal('25.00'),
            promotion_starts_at=now - timedelta(hours=1),
            promotion_ends_at=now + timedelta(days=1),
        )
        defaults.update(overrides)
        return Product.objects.create(**defaults)

    def test_update_without_promotion_fields_leaves_them_untouched(self):
        product = self._create_promo_product()
        starts_at, ends_at = product.promotion_starts_at, product.promotion_ends_at
        client = self.auth_client(self.user)

        response = client.patch(
            f'/api/v1/products/products/{product.id}/', {'price': '15.00'}, format='multipart',
        )

        self.assertEqual(response.status_code, 200)
        product.refresh_from_db()
        self.assertEqual(product.price, Decimal('15.00'))
        self.assertEqual(product.discount_percentage, Decimal('25.00'))
        self.assertEqual(product.promotion_starts_at, starts_at)
        self.assertEqual(product.promotion_ends_at, ends_at)

    def test_multibuy_option_empty_string_normalizes_to_null(self):
        product = Product.objects.create(
            name='Multibuy Product', description='Desc', price=Decimal('10.00'),
            business=self.profile, multibuy_option='2x1',
        )
        client = self.auth_client(self.user)

        response = client.patch(
            f'/api/v1/products/products/{product.id}/', {'multibuy_option': ''}, format='multipart',
        )

        self.assertEqual(response.status_code, 200)
        product.refresh_from_db()
        self.assertIsNone(product.multibuy_option)

    def test_clearing_dates_via_empty_string_nulls_them(self):
        product = self._create_promo_product()
        client = self.auth_client(self.user)

        response = client.patch(
            f'/api/v1/products/products/{product.id}/',
            {'promotion_starts_at': '', 'promotion_ends_at': ''},
            format='multipart',
        )

        self.assertEqual(response.status_code, 200)
        product.refresh_from_db()
        self.assertIsNone(product.promotion_starts_at)
        self.assertIsNone(product.promotion_ends_at)

    def test_promotion_end_before_start_rejected_on_create(self):
        client = self.auth_client(self.user)
        now = timezone.now()

        response = client.post(
            '/api/v1/products/products/',
            {
                'name': 'Bad Window', 'description': 'Desc', 'price': '10.00',
                'discount_percentage': '10.00',
                'promotion_starts_at': (now + timedelta(days=1)).isoformat(),
                'promotion_ends_at': now.isoformat(),
            },
            format='multipart',
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn('promotion_ends_at', response.data)

    def test_partial_update_start_only_validates_against_existing_end(self):
        product = self._create_promo_product()
        client = self.auth_client(self.user)

        response = client.patch(
            f'/api/v1/products/products/{product.id}/',
            {'promotion_starts_at': (product.promotion_ends_at + timedelta(days=1)).isoformat()},
            format='multipart',
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn('promotion_ends_at', response.data)

    def test_expired_promotion_auto_cleared_before_unrelated_update_applies(self):
        """An unrelated save on a product whose promo already expired doesn't
        reject the request — but the read that locates the object for the
        update lazily auto-clears the stale promo config first (ticket #8),
        so the unrelated field change lands on an already-reset product."""
        now = timezone.now()
        product = self._create_promo_product(
            promotion_starts_at=now - timedelta(days=2),
            promotion_ends_at=now - timedelta(days=1),
        )
        client = self.auth_client(self.user)

        response = client.patch(
            f'/api/v1/products/products/{product.id}/', {'price': '20.00'}, format='multipart',
        )

        self.assertEqual(response.status_code, 200)
        product.refresh_from_db()
        self.assertEqual(product.price, Decimal('20.00'))
        self.assertEqual(product.discount_percentage, Decimal('0'))
        self.assertIsNone(product.promotion_starts_at)
        self.assertIsNone(product.promotion_ends_at)

    def test_dates_without_discount_or_multibuy_rejected_on_create(self):
        client = self.auth_client(self.user)
        now = timezone.now()

        response = client.post(
            '/api/v1/products/products/',
            {
                'name': 'Countdown Only', 'description': 'Desc', 'price': '10.00',
                'promotion_starts_at': now.isoformat(),
                'promotion_ends_at': (now + timedelta(days=1)).isoformat(),
            },
            format='multipart',
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn('discount_percentage', response.data)
        self.assertFalse(Product.objects.filter(name='Countdown Only').exists())

    def test_dates_with_discount_accepted_on_create(self):
        client = self.auth_client(self.user)
        now = timezone.now()

        response = client.post(
            '/api/v1/products/products/',
            {
                'name': 'Countdown With Discount', 'description': 'Desc', 'price': '10.00',
                'discount_percentage': '15.00',
                'promotion_starts_at': now.isoformat(),
                'promotion_ends_at': (now + timedelta(days=1)).isoformat(),
            },
            format='multipart',
        )

        self.assertEqual(response.status_code, 201)

    def test_non_numeric_discount_rejected(self):
        client = self.auth_client(self.user)

        response = client.post(
            '/api/v1/products/products/',
            {
                'name': 'Bad Discount', 'description': 'Desc', 'price': '10.00',
                'discount_percentage': 'abc',
            },
            format='multipart',
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn('discount_percentage', response.data)

    def test_legacy_countdown_only_product_survives_unrelated_field_update(self):
        # Grandfathered row saved before this rule existed: dates set, no
        # discount/multibuy. An unrelated-field save must not be blocked by
        # a rule this row predates.
        now = timezone.now()
        product = self._create_promo_product(
            discount_percentage=Decimal('0'),
            promotion_starts_at=now - timedelta(hours=1),
            promotion_ends_at=now + timedelta(days=1),
        )
        client = self.auth_client(self.user)

        response = client.patch(
            f'/api/v1/products/products/{product.id}/', {'price': '30.00'}, format='multipart',
        )

        self.assertEqual(response.status_code, 200)
        product.refresh_from_db()
        self.assertEqual(product.price, Decimal('30.00'))


class TestProductCategoryPromotionValidation(MarkyAPITestCase):
    """Backend backstop for Ticket 3: a promotion window with no discount/
    multibuy type is a no-op and must be rejected, mirroring the product-side
    rule in TestProductInputSerializerPromotion."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.user, cls.profile = cls.make_user('promo_cat_user', 'promo_cat@test.com')
        cls.category = ProductCategory.objects.create(business=cls.profile, name='Cat', icon='icon')

    def test_dates_without_type_rejected(self):
        client = self.auth_client(self.user)
        now = timezone.now()

        response = client.post(
            f'/api/v1/products/product-categories/{self.category.id}/add_promotion/',
            {
                'promotion_starts_at': now.isoformat(),
                'promotion_ends_at': (now + timedelta(days=1)).isoformat(),
            },
            format='json',
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn('discount_percentage', response.data)

    def test_dates_with_multibuy_accepted(self):
        client = self.auth_client(self.user)
        now = timezone.now()

        response = client.post(
            f'/api/v1/products/product-categories/{self.category.id}/add_promotion/',
            {
                'multibuy_option': '2x1',
                'promotion_starts_at': now.isoformat(),
                'promotion_ends_at': (now + timedelta(days=1)).isoformat(),
            },
            format='json',
        )

        self.assertEqual(response.status_code, 200)


class TestPromotionStatusInResponses(MarkyAPITestCase):
    """API-level regression tests for promotion_status and the
    category-discount-with-product-dates bug (ticket #8's headline symptom)."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.user, cls.profile = cls.make_user('promo_status_user', 'promo_status@test.com')

    def test_expired_promotion_status_is_expired_and_fields_auto_cleared(self):
        now = timezone.now()
        product = Product.objects.create(
            name='Expired Promo', description='Desc', price=Decimal('10.00'),
            business=self.profile,
            discount_percentage=Decimal('30.00'),
            promotion_starts_at=now - timedelta(days=2),
            promotion_ends_at=now - timedelta(days=1),
        )
        client = self.auth_client(self.user)

        # The GET itself triggers the lazy auto-deactivation before the object
        # is fetched for serialization, so the response already reflects the
        # cleared, "nothing configured" state.
        response = client.get(f'/api/v1/products/products/{product.id}/')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['promotion_status'], INACTIVE)
        # Auto-teardown: expired promo config is wiped back to normal-product state.
        product.refresh_from_db()
        self.assertIsNone(product.multibuy_option)
        self.assertEqual(product.discount_percentage, Decimal('0'))
        self.assertIsNone(product.promotion_starts_at)
        self.assertIsNone(product.promotion_ends_at)

    def test_category_promo_used_when_products_own_promo_is_expired(self):
        # Outcome is unchanged by the priority flip: the product's own dates
        # here are already expired, so this exercises the fallback branch
        # (product not active/scheduled -> category consulted), not an
        # override of an active product promo by the category.
        now = timezone.now()
        category = ProductCategory.objects.create(
            business=self.profile, name='Cat Promo', icon='icon',
            discount_percentage=Decimal('40.00'),
            promotion_starts_at=now - timedelta(hours=1),
            promotion_ends_at=now + timedelta(hours=1),
        )
        Product.objects.create(
            name='Inherits Category Promo', description='Desc', price=Decimal('10.00'),
            business=self.profile, category=category,
            # Product's OWN promo dates are already past ends_at — by the time
            # this resolves, the lazy expiry sweep (triggered by the GET below)
            # has already cleared them, so the product is plain inactive (not
            # merely "expired") — the resolved dates shown to the card/quick
            # modal must be the category's.
            discount_percentage=Decimal('5.00'),
            promotion_starts_at=now - timedelta(days=10),
            promotion_ends_at=now - timedelta(days=9),
        )
        category.refresh_from_db()
        client = self.auth_client(self.user)

        response = client.get(
            '/api/v1/products/product-categories/with_products/', {'ids': str(category.id)},
        )

        self.assertEqual(response.status_code, 200)
        [returned_product] = response.data['results'][0]['products']
        self.assertEqual(returned_product['promotion_status'], ACTIVE)
        self.assertEqual(returned_product['discount_percentage'], Decimal('40.00'))
        self.assertEqual(returned_product['promotion_starts_at'], category.promotion_starts_at)
        self.assertEqual(returned_product['promotion_ends_at'], category.promotion_ends_at)

    def test_product_promo_active_uses_product_dates_not_category_dates(self):
        now = timezone.now()
        # Category's own window must independently resolve to ACTIVE too — a
        # genuine, undisqualified active candidate, so this proves the
        # product wins over an active category rather than merely winning by
        # the category being disqualified through its own expiry.
        category = ProductCategory.objects.create(
            business=self.profile, name='Cat Promo', icon='icon',
            discount_percentage=Decimal('40.00'),
            promotion_starts_at=now - timedelta(hours=2),
            promotion_ends_at=now + timedelta(hours=2),
        )
        product = Product.objects.create(
            name='Has Own Active Promo', description='Desc', price=Decimal('10.00'),
            business=self.profile, category=category,
            discount_percentage=Decimal('5.00'),
            promotion_starts_at=now - timedelta(hours=1),
            promotion_ends_at=now + timedelta(hours=1),
        )
        client = self.auth_client(self.user)

        response = client.get(
            '/api/v1/products/product-categories/with_products/', {'ids': str(category.id)},
        )

        self.assertEqual(response.status_code, 200)
        [returned_product] = response.data['results'][0]['products']
        self.assertEqual(returned_product['promotion_status'], ACTIVE)
        self.assertEqual(returned_product['discount_percentage'], Decimal('5.00'))
        self.assertEqual(returned_product['promotion_starts_at'], product.promotion_starts_at)
        self.assertEqual(returned_product['promotion_ends_at'], product.promotion_ends_at)

    def test_product_moved_into_promoted_category_inherits_it(self):
        category_a = ProductCategory.objects.create(
            business=self.profile, name='Cat A', icon='icon',
        )
        now = timezone.now()
        category_b = ProductCategory.objects.create(
            business=self.profile, name='Cat B', icon='icon',
            discount_percentage=Decimal('40.00'),
            promotion_starts_at=now - timedelta(hours=1),
            promotion_ends_at=now + timedelta(hours=1),
        )
        product = Product.objects.create(
            name='No Promo Config', description='Desc', price=Decimal('10.00'),
            business=self.profile, category=category_a,
        )
        client = self.auth_client(self.user)

        response = client.patch(
            f'/api/v1/products/products/{product.id}/', {'category': category_b.id}, format='multipart',
        )

        self.assertEqual(response.status_code, 200)
        response = client.get(
            '/api/v1/products/product-categories/with_products/', {'ids': str(category_b.id)},
        )
        [returned_product] = response.data['results'][0]['products']
        self.assertEqual(returned_product['promotion_status'], ACTIVE)
        self.assertEqual(returned_product['discount_percentage'], Decimal('40.00'))
        self.assertEqual(returned_product['promotion_starts_at'], category_b.promotion_starts_at)
        self.assertEqual(returned_product['promotion_ends_at'], category_b.promotion_ends_at)

    def test_product_moved_into_promoted_category_keeps_own_promo(self):
        category_a = ProductCategory.objects.create(
            business=self.profile, name='Cat A', icon='icon',
        )
        now = timezone.now()
        category_b = ProductCategory.objects.create(
            business=self.profile, name='Cat B', icon='icon',
            discount_percentage=Decimal('40.00'),
            promotion_starts_at=now - timedelta(hours=1),
            promotion_ends_at=now + timedelta(hours=1),
        )
        product = Product.objects.create(
            name='Has Own Active Promo', description='Desc', price=Decimal('10.00'),
            business=self.profile, category=category_a,
            discount_percentage=Decimal('5.00'),
            promotion_starts_at=now - timedelta(hours=2),
            promotion_ends_at=now + timedelta(hours=2),
        )
        client = self.auth_client(self.user)

        response = client.patch(
            f'/api/v1/products/products/{product.id}/', {'category': category_b.id}, format='multipart',
        )

        self.assertEqual(response.status_code, 200)
        response = client.get(
            '/api/v1/products/product-categories/with_products/', {'ids': str(category_b.id)},
        )
        [returned_product] = response.data['results'][0]['products']
        self.assertEqual(returned_product['promotion_status'], ACTIVE)
        self.assertEqual(returned_product['discount_percentage'], Decimal('5.00'))
        self.assertEqual(returned_product['promotion_starts_at'], product.promotion_starts_at)
        self.assertEqual(returned_product['promotion_ends_at'], product.promotion_ends_at)

    def test_category_promo_expires_naturally_with_no_product_promo_reads_as_no_promotion(self):
        now = timezone.now()
        category = ProductCategory.objects.create(
            business=self.profile, name='Cat Expired', icon='icon',
            discount_percentage=Decimal('40.00'),
            promotion_ends_at=now - timedelta(days=1),
        )
        Product.objects.create(
            name='No Promo Config', description='Desc', price=Decimal('10.00'),
            business=self.profile, category=category,
            multibuy_option=None, discount_percentage=Decimal('0'),
        )
        client = self.auth_client(self.user)

        response = client.get(
            '/api/v1/products/product-categories/with_products/', {'ids': str(category.id)},
        )

        [returned_product] = response.data['results'][0]['products']
        self.assertEqual(returned_product['promotion_status'], INACTIVE)

    def test_category_expired_falls_back_to_product_own_active_promo(self):
        # Outcome is unchanged by the priority flip — the product's own promo
        # is active, so it wins under product-first priority regardless of
        # whether the category's promo happens to be expired.
        now = timezone.now()
        category = ProductCategory.objects.create(
            business=self.profile, name='Cat Expired', icon='icon',
            discount_percentage=Decimal('40.00'),
            promotion_ends_at=now - timedelta(days=1),
        )
        Product.objects.create(
            name='Own Active Promo', description='Desc', price=Decimal('10.00'),
            business=self.profile, category=category,
            discount_percentage=Decimal('5.00'),
        )
        client = self.auth_client(self.user)

        response = client.get(
            '/api/v1/products/product-categories/with_products/', {'ids': str(category.id)},
        )

        [returned_product] = response.data['results'][0]['products']
        self.assertEqual(returned_product['promotion_status'], ACTIVE)
        self.assertEqual(returned_product['discount_percentage'], Decimal('5.00'))

    def test_scheduled_product_promo_and_active_category_agree_between_filter_and_resolver(self):
        # Regression for a filter/resolver drift: product_has_active_promotion_q()
        # (products/filters.py) and resolve_effective_promotion() must agree on
        # the product-SCHEDULED + category-ACTIVE cell. Before the priority fix
        # (product override requires ACTIVE, not just ACTIVE-or-SCHEDULED), the
        # resolver reported the product's own (not-yet-started) promo here while
        # the filter still matched the category's live discount — i.e. the
        # display showed no discount for a product the filter said was on promo.
        now = timezone.now()
        category = ProductCategory.objects.create(
            business=self.profile, name='Cat Active', icon='icon',
            discount_percentage=Decimal('40.00'),
            promotion_starts_at=now - timedelta(hours=1),
            promotion_ends_at=now + timedelta(hours=1),
        )
        Product.objects.create(
            name='Own Scheduled Promo', description='Desc', price=Decimal('10.00'),
            business=self.profile, category=category,
            discount_percentage=Decimal('5.00'),
            promotion_starts_at=now + timedelta(days=1),
        )
        client = self.auth_client(self.user)

        # (b) The has_promotion filter (product_has_active_promotion_q, used by
        # with_products?has_promotion=true) must include this product: its
        # category has a genuinely active promo even though the product's own
        # promo hasn't started.
        response = client.get(
            '/api/v1/products/product-categories/with_products/',
            {'ids': str(category.id), 'has_promotion': 'true'},
        )

        self.assertEqual(response.status_code, 200)
        [returned_category] = response.data['results']
        [returned_product] = returned_category['products']

        # (a) The resolved display must show the category's ACTIVE discount,
        # not the product's own not-yet-started one — agreeing with the filter.
        self.assertEqual(returned_product['promotion_status'], ACTIVE)
        self.assertEqual(returned_product['discount_percentage'], Decimal('40.00'))
        self.assertEqual(returned_product['promotion_starts_at'], category.promotion_starts_at)
        self.assertEqual(returned_product['promotion_ends_at'], category.promotion_ends_at)


class TestExpiredPromotionAutoDeactivation(MarkyAPITestCase):
    """Ticket: auto-clear an expired promotion's fields and notify once, for
    both Product and ProductCategory, triggered lazily off the tenant-scoped
    read endpoints (no scheduler in this project)."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.user, cls.profile = cls.make_user('expiry_user', 'expiry@test.com')

    def _expired_product(self, name='Expired Product', **kwargs):
        now = timezone.now()
        return Product.objects.create(
            name=name, description='Desc', price=Decimal('10.00'),
            business=self.profile,
            discount_percentage=Decimal('30.00'),
            promotion_starts_at=now - timedelta(days=2),
            promotion_ends_at=now - timedelta(days=1),
            **kwargs,
        )

    def _expired_category(self, name='Expired Category', **kwargs):
        now = timezone.now()
        return ProductCategory.objects.create(
            business=self.profile, name=name, icon='icon',
            discount_percentage=Decimal('30.00'),
            promotion_starts_at=now - timedelta(days=2),
            promotion_ends_at=now - timedelta(days=1),
            **kwargs,
        )

    def test_product_promo_cleared_and_notified_on_read(self):
        product = self._expired_product()
        client = self.auth_client(self.user)

        client.get('/api/v1/products/products/')

        product.refresh_from_db()
        self.assertIsNone(product.multibuy_option)
        self.assertEqual(product.discount_percentage, Decimal('0'))
        self.assertIsNone(product.promotion_starts_at)
        self.assertIsNone(product.promotion_ends_at)

        [notification] = Notification.objects.all()
        self.assertEqual(
            notification.message, 'La promoción del producto "Expired Product" ha finalizado.',
        )
        self.assertEqual(notification.link, f'/product/edit/{product.id}?section=destacar')
        self.assertIn(self.user, notification.recipients.all())

    def test_product_name_truncated_at_20_chars_in_notification(self):
        product = self._expired_product(name='A Very Long Product Name That Exceeds The Limit')
        client = self.auth_client(self.user)

        client.get('/api/v1/products/products/')

        notification = Notification.objects.get()
        self.assertEqual(
            notification.message,
            'La promoción del producto "A Very Long Product ..." ha finalizado.',
        )

    def test_category_promo_cleared_and_notified_on_read(self):
        category = self._expired_category()
        client = self.auth_client(self.user)

        client.get('/api/v1/products/product-categories/')

        category.refresh_from_db()
        self.assertIsNone(category.multibuy_option)
        self.assertEqual(category.discount_percentage, Decimal('0'))
        self.assertIsNone(category.promotion_starts_at)
        self.assertIsNone(category.promotion_ends_at)

        [notification] = Notification.objects.all()
        self.assertEqual(
            notification.message, 'La promoción de la categoría "Expired Category" ha finalizado.',
        )
        self.assertEqual(notification.link, f'/home?promoCategory={category.id}')
        self.assertIn(self.user, notification.recipients.all())

    def test_category_read_via_with_products_also_clears(self):
        category = self._expired_category()
        client = self.auth_client(self.user)

        client.get('/api/v1/products/product-categories/with_products/')

        category.refresh_from_db()
        self.assertIsNone(category.promotion_ends_at)
        self.assertEqual(Notification.objects.count(), 1)

    def test_idempotent_across_repeated_reads(self):
        self._expired_product()
        self._expired_category()
        client = self.auth_client(self.user)

        client.get('/api/v1/products/products/')
        client.get('/api/v1/products/product-categories/')
        client.get('/api/v1/products/products/')
        client.get('/api/v1/products/product-categories/')

        self.assertEqual(Notification.objects.count(), 2)

    def test_reconfigured_promotion_that_expires_again_notifies_independently(self):
        product = self._expired_product()
        client = self.auth_client(self.user)
        client.get('/api/v1/products/products/')
        self.assertEqual(Notification.objects.count(), 1)

        now = timezone.now()
        product.discount_percentage = Decimal('20.00')
        product.promotion_starts_at = now - timedelta(days=2)
        product.promotion_ends_at = now - timedelta(days=1)
        product.save()

        client.get('/api/v1/products/products/')

        self.assertEqual(Notification.objects.count(), 2)
        product.refresh_from_db()
        self.assertIsNone(product.promotion_ends_at)

    def test_tenancy_only_affects_the_requesting_business(self):
        other_user, other_profile = self.make_user('other_expiry_user', 'other_expiry@test.com')
        mine = self._expired_product(name='Mine')
        now = timezone.now()
        theirs = Product.objects.create(
            name='Theirs', description='Desc', price=Decimal('10.00'),
            business=other_profile,
            discount_percentage=Decimal('30.00'),
            promotion_ends_at=now - timedelta(days=1),
        )

        client = self.auth_client(self.user)
        client.get('/api/v1/products/products/')

        mine.refresh_from_db()
        theirs.refresh_from_db()
        self.assertIsNone(mine.promotion_ends_at)
        self.assertIsNotNone(theirs.promotion_ends_at)
        self.assertEqual(Notification.objects.filter(recipients=other_user).count(), 0)

    def test_future_end_date_untouched(self):
        now = timezone.now()
        product = Product.objects.create(
            name='Future Promo', description='Desc', price=Decimal('10.00'),
            business=self.profile,
            discount_percentage=Decimal('30.00'),
            promotion_ends_at=now + timedelta(days=1),
        )
        client = self.auth_client(self.user)

        client.get('/api/v1/products/products/')

        product.refresh_from_db()
        self.assertEqual(product.discount_percentage, Decimal('30.00'))
        self.assertIsNotNone(product.promotion_ends_at)
        self.assertEqual(Notification.objects.count(), 0)

    def test_no_promo_configured_untouched(self):
        product = Product.objects.create(
            name='Plain Product', description='Desc', price=Decimal('10.00'),
            business=self.profile,
        )
        client = self.auth_client(self.user)

        client.get('/api/v1/products/products/')

        self.assertEqual(Notification.objects.count(), 0)

    def test_repeated_service_calls_only_notify_once_per_object(self):
        """Two back-to-back calls to the service entry point (as would happen
        from overlapping requests) must only notify once per object — the
        second call's candidate query no longer matches the already-cleared row."""
        self._expired_product()
        now = timezone.now()

        handle_expired_promotions_for_business(self.profile, now=now)
        handle_expired_promotions_for_business(self.profile, now=now)

        self.assertEqual(Notification.objects.count(), 1)


# ---------------------------------------------------------------------------
# Public (unauthenticated) read API — marky.one/<business_id>
# ---------------------------------------------------------------------------

class TestPublicCatalogAndProductAPI(MarkyAPITestCase):
    """Coverage for the anonymous read endpoints under /api/v1/public/business/<id>/."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.user, cls.profile = cls.make_user('public_biz', 'public_biz@test.com')
        cls.category = ProductCategory.objects.create(
            business=cls.profile, name='Platos', icon='fa-pizza', order=1,
        )
        cls.visible_product = Product.objects.create(
            name='Visible', description='Desc', price=Decimal('10.00'),
            business=cls.profile, category=cls.category, is_active=True,
        )
        cls.hidden_product = Product.objects.create(
            name='Hidden', description='Desc', price=Decimal('20.00'),
            business=cls.profile, category=cls.category, is_active=False,
        )

        # A second business, in a different currency, to prove there is no
        # cross-tenant leakage and no currency-context bleed (A2).
        cls.other_user, cls.other_profile = cls.make_user(
            'other_public_biz', 'other_public_biz@test.com'
        )
        cls.other_profile.primary_currency = cls.secondary_currency  # PYG
        cls.other_profile.save()
        cls.other_product = Product.objects.create(
            name='Theirs', description='Desc', price=Decimal('100000.00'),
            business=cls.other_profile, is_active=True,
        )

    def test_anonymous_client_reaches_all_public_endpoints(self):
        base = f'/api/v1/public/business/{self.profile.business_id}'

        self.assertEqual(self.client.get(f'{base}/').status_code, 200)
        self.assertEqual(self.client.get(f'{base}/catalog/').status_code, 200)
        self.assertEqual(self.client.get(f'{base}/categories/').status_code, 200)
        self.assertEqual(
            self.client.get(f'{base}/products/{self.visible_product.id}/').status_code, 200
        )

    def test_unknown_slug_404s_on_every_endpoint(self):
        base = '/api/v1/public/business/does-not-exist'

        self.assertEqual(self.client.get(f'{base}/').status_code, 404)
        self.assertEqual(self.client.get(f'{base}/catalog/').status_code, 404)
        self.assertEqual(self.client.get(f'{base}/categories/').status_code, 404)
        self.assertEqual(self.client.get(f'{base}/products/{self.visible_product.id}/').status_code, 404)

    def test_inactive_product_absent_from_catalog(self):
        response = self.client.get(f'/api/v1/public/business/{self.profile.business_id}/catalog/')
        self.assertEqual(response.status_code, 200)
        product_ids = [
            product['id']
            for category in response.data['results']
            for product in category['products']
        ]
        self.assertIn(self.visible_product.id, product_ids)
        self.assertNotIn(self.hidden_product.id, product_ids)

    def test_inactive_product_404s_on_detail(self):
        response = self.client.get(
            f'/api/v1/public/business/{self.profile.business_id}/products/{self.hidden_product.id}/'
        )
        self.assertEqual(response.status_code, 404)

    def test_product_of_business_a_not_reachable_under_business_b_slug(self):
        response = self.client.get(
            f'/api/v1/public/business/{self.other_profile.business_id}/'
            f'products/{self.visible_product.id}/'
        )
        self.assertEqual(response.status_code, 404)

    def test_prices_use_target_businesss_currency_even_when_a_different_merchant_is_authenticated(self):
        """Regression for the A2 fix: ProductPriceMixin/ProductLiteSerializer
        must not prefer the authenticated caller's own business_profile over
        the business_profile passed explicitly in serializer context."""
        client = self.auth_client(self.user)  # authenticated as the USD-priced business

        response = client.get(
            f'/api/v1/public/business/{self.other_profile.business_id}/'
            f'products/{self.other_product.id}/'
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['primary_price'], 'PYG 100.000')

        anon_response = self.client.get(
            f'/api/v1/public/business/{self.other_profile.business_id}/'
            f'products/{self.other_product.id}/'
        )
        self.assertEqual(anon_response.data['primary_price'], response.data['primary_price'])

    def test_no_promo_expiry_writes_occur_on_a_public_read(self):
        now = timezone.now()
        expired = Product.objects.create(
            name='Expired Promo', description='Desc', price=Decimal('10.00'),
            business=self.profile, discount_percentage=Decimal('30.00'),
            promotion_ends_at=now - timedelta(days=1),
        )

        self.client.get(f'/api/v1/public/business/{self.profile.business_id}/catalog/')
        self.client.get(
            f'/api/v1/public/business/{self.profile.business_id}/products/{expired.id}/'
        )

        expired.refresh_from_db()
        self.assertEqual(expired.discount_percentage, Decimal('30.00'))
        self.assertIsNotNone(expired.promotion_ends_at)
        self.assertEqual(Notification.objects.count(), 0)

    def test_has_promotion_hides_a_category_whose_only_promoted_product_is_inactive(self):
        """Regression: ProductCategoryFilter.filter_has_promotion's Exists
        subquery must be scoped to the same is_active=True product_queryset
        the public catalog otherwise applies, or a category whose only
        promoted product is hidden shows up as a phantom (empty) category
        under ?has_promotion=true."""
        hidden_promo_category = ProductCategory.objects.create(
            business=self.profile, name='Solo Oculto', icon='fa-box', order=2,
        )
        Product.objects.create(
            name='Hidden Promo Product', description='Desc', price=Decimal('10.00'),
            business=self.profile, category=hidden_promo_category, is_active=False,
            discount_percentage=Decimal('20.00'),
        )

        response = self.client.get(
            f'/api/v1/public/business/{self.profile.business_id}/catalog/',
            {'has_promotion': 'true'},
        )

        self.assertEqual(response.status_code, 200)
        category_ids = [category['id'] for category in response.data['results']]
        self.assertNotIn(hidden_promo_category.id, category_ids)

    def _public_detail(self, product):
        response = self.client.get(
            f'/api/v1/public/business/{self.profile.business_id}/products/{product.id}/'
        )
        self.assertEqual(response.status_code, 200)
        return response.data

    def test_public_detail_exposes_descriptive_extras(self):
        product = Product.objects.create(
            name='Torta', description='Desc', price=Decimal('10.00'),
            business=self.profile, category=self.category, is_active=True,
            featured_ingredients='Chocolate,Dulce de leche',
            allergens='milk,egg',
            presentation={
                'version': 1,
                'amount': {'type': 'weight', 'value': 1.5, 'unit': 'kg'},
                'dimensions': {'shape': 'round', 'diameterCm': 22},
                'approximateYield': {'minPeople': 12, 'maxPeople': 15},
            },
            celiac_info={
                'version': 1,
                'crossContaminationControl': True,
                'glutenFreeGrains': False,
                'certifiedProtocol': True,
            },
        )

        data = self._public_detail(product)

        self.assertEqual(data['featured_ingredients'], 'Chocolate,Dulce de leche')
        self.assertEqual(data['allergens'], 'milk,egg')
        self.assertEqual(data['presentation']['amount'], {'type': 'weight', 'value': 1.5, 'unit': 'kg'})
        self.assertEqual(data['presentation']['approximateYield'], {'minPeople': 12, 'maxPeople': 15})
        self.assertTrue(data['celiac_info']['crossContaminationControl'])
        self.assertFalse(data['celiac_info']['glutenFreeGrains'])

    def test_public_detail_returns_nulls_for_legacy_products(self):
        data = self._public_detail(self.visible_product)

        for field in ('featured_ingredients', 'allergens', 'presentation', 'celiac_info'):
            self.assertIn(field, data)
            self.assertIsNone(data[field])

    def test_public_detail_nulls_malformed_json_extras(self):
        product = Product.objects.create(
            name='Hand edited', description='Desc', price=Decimal('10.00'),
            business=self.profile, category=self.category, is_active=True,
            presentation={'version': 7, 'amount': 'lots'},
            celiac_info={'version': 1, 'crossContaminationControl': 'yes'},
        )

        data = self._public_detail(product)

        self.assertIsNone(data['presentation'])
        self.assertIsNone(data['celiac_info'])

    def test_public_detail_still_hides_admin_only_fields(self):
        data = self._public_detail(self.visible_product)

        for field in ('business', 'is_active', 'order'):
            self.assertNotIn(field, data)


    # --- canonical slug URLs (batch 5) -----------------------------------

    def _canonical_url(self, business_id, category_slug, product_slug):
        return (
            f'/api/v1/public/business/{business_id}/categories/{category_slug}/'
            f'products/{product_slug}/'
        )

    def test_canonical_detail_matches_legacy_numeric_detail(self):
        canonical = self.client.get(
            self._canonical_url(self.profile.business_id, 'platos', 'visible')
        )
        legacy = self.client.get(
            f'/api/v1/public/business/{self.profile.business_id}/products/{self.visible_product.id}/'
        )
        self.assertEqual(canonical.status_code, 200)
        self.assertEqual(legacy.status_code, 200)
        self.assertEqual(canonical.data, legacy.data)
        self.assertEqual(canonical.data['id'], self.visible_product.id)
        self.assertEqual(canonical.data['slug'], 'visible')
        self.assertEqual(canonical.data['category'], {'id': self.category.id, 'name': 'Platos', 'slug': 'platos'})

    def test_canonical_detail_404s_for_hidden_product(self):
        response = self.client.get(self._canonical_url(self.profile.business_id, 'platos', 'hidden'))
        self.assertEqual(response.status_code, 404)
        self.assertEqual(str(response.data['detail']), 'No se encontró el producto solicitado.')

    def test_canonical_detail_404s_for_missing_product_category_or_business(self):
        for url in (
            self._canonical_url(self.profile.business_id, 'platos', 'no-existe'),
            self._canonical_url(self.profile.business_id, 'no-existe', 'visible'),
            self._canonical_url('does-not-exist', 'platos', 'visible'),
        ):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 404)

    def test_canonical_detail_404s_under_the_wrong_category(self):
        ProductCategory.objects.create(business=self.profile, name='Bebidas', icon='fa-glass', order=2)
        self.assertEqual(
            self.client.get(self._canonical_url(self.profile.business_id, 'bebidas', 'visible')).status_code,
            404,
        )
        # A categorized product is never reachable through the uncategorized segment.
        self.assertEqual(
            self.client.get(self._canonical_url(self.profile.business_id, 'sin-categoria', 'visible')).status_code,
            404,
        )

    def test_canonical_detail_is_tenant_scoped(self):
        # Business B has a category with the same slug and an identically named product.
        other_category = ProductCategory.objects.create(
            business=self.other_profile, name='Platos', icon='fa-pizza', order=1,
        )
        other_visible = Product.objects.create(
            name='Visible', description='Desc', price=Decimal('5.00'),
            business=self.other_profile, category=other_category,
        )
        response = self.client.get(self._canonical_url(self.other_profile.business_id, 'platos', 'visible'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['id'], other_visible.id)

        # B's uncategorized product is not reachable under A's business slug.
        self.assertEqual(
            self.client.get(self._canonical_url(self.profile.business_id, 'sin-categoria', 'theirs')).status_code,
            404,
        )
        # A product of A can't be reached through B's category, even with matching slugs.
        Product.objects.filter(pk=other_visible.pk).update(is_active=False)
        self.assertEqual(
            self.client.get(self._canonical_url(self.other_profile.business_id, 'platos', 'visible')).status_code,
            404,
        )

    def test_canonical_detail_resolves_uncategorized_products(self):
        response = self.client.get(
            self._canonical_url(self.other_profile.business_id, 'sin-categoria', 'theirs')
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['id'], self.other_product.id)
        self.assertIsNone(response.data['category'])

    def test_catalog_and_category_list_expose_slugs(self):
        Product.objects.create(
            name='Suelto', description='Desc', price=Decimal('1.00'), business=self.profile,
        )
        catalog = self.client.get(f'/api/v1/public/business/{self.profile.business_id}/catalog/').data
        by_slug = {category['slug']: category for category in catalog['results']}
        self.assertIn('platos', by_slug)
        self.assertEqual(by_slug['platos']['products'][0]['slug'], 'visible')
        self.assertIn('sin-categoria', by_slug)
        self.assertIsNone(by_slug['sin-categoria']['id'])
        self.assertEqual([p['slug'] for p in by_slug['sin-categoria']['products']], ['suelto'])

        categories = self.client.get(f'/api/v1/public/business/{self.profile.business_id}/categories/').data
        rows = categories['results'] if isinstance(categories, dict) else categories
        self.assertEqual([c['slug'] for c in rows], ['platos'])


class TestProductSlugs(MarkyAPITestCase):
    """Slug allocation policy for canonical public product URLs."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.user, cls.profile = cls.make_user('slug_biz', 'slug_biz@test.com')
        cls.other_user, cls.other_profile = cls.make_user('slug_other', 'slug_other@test.com')

    def _category(self, name, business=None):
        return ProductCategory.objects.create(business=business or self.profile, name=name, icon='fa-box')

    def _product(self, name, category=None, business=None, **extra):
        return Product.objects.create(
            name=name, description='Desc', price=Decimal('1.00'),
            business=business or self.profile, category=category, **extra,
        )

    def test_slugify_name_normalizes_accents_punctuation_and_whitespace(self):
        cases = {
            'Café Ñandú': 'cafe-nandu',
            '¡Pizza  &  Pasta!': 'pizza-pasta',
            '  Doble   espacio  ': 'doble-espacio',
            'Hamburguesa_XL 2.0': 'hamburguesa_xl-20',
            '寿司': '',
            '🍕🍕': '',
            '   ': '',
            '---': '',
        }
        for name, expected in cases.items():
            with self.subTest(name=name):
                self.assertEqual(slugify_name(name), expected)

    def test_with_suffix_truncates_to_max_length(self):
        base = 'a' * SLUG_MAX_LENGTH
        self.assertEqual(with_suffix(base, 1), base)
        self.assertEqual(with_suffix(base, 2), 'a' * (SLUG_MAX_LENGTH - 2) + '-2')
        self.assertEqual(len(with_suffix(base, 123)), SLUG_MAX_LENGTH)
        # A truncation landing on a separator never yields a double dash.
        self.assertEqual(with_suffix('a' * 252 + '-bcd', 2), 'a' * 252 + '-2')

    def test_product_slugs_from_names(self):
        self.assertEqual(self._product('Café Ñandú').slug, 'cafe-nandu')
        self.assertEqual(self._product('¡Empanada de Carne!').slug, 'empanada-de-carne')

    def test_empty_names_fall_back_to_id_based_slugs(self):
        product = self._product('寿司')
        self.assertEqual(product.slug, f'p-{product.pk}')
        product.refresh_from_db()
        self.assertEqual(product.slug, f'p-{product.pk}')
        whitespace = self._product('   ')
        self.assertEqual(whitespace.slug, f'p-{whitespace.pk}')
        category = self._category('🍕')
        category.refresh_from_db()
        self.assertEqual(category.slug, f'c-{category.pk}')

    def test_duplicate_names_in_same_category_get_deterministic_suffixes(self):
        category = self._category('Pizzas')
        slugs = [self._product('Muzzarella', category).slug for _ in range(3)]
        self.assertEqual(slugs, ['muzzarella', 'muzzarella-2', 'muzzarella-3'])
        # Names that only differ by accents/case collide too.
        self.assertEqual(self._product('MUZZARÉLLA', category).slug, 'muzzarella-4')

    def test_same_name_in_different_categories_and_businesses_does_not_collide(self):
        pizzas = self._category('Pizzas')
        promos = self._category('Promos')
        self.assertEqual(self._product('Muzzarella', pizzas).slug, 'muzzarella')
        self.assertEqual(self._product('Muzzarella', promos).slug, 'muzzarella')
        self.assertEqual(self._product('Muzzarella').slug, 'muzzarella')  # uncategorized
        self.assertEqual(self._product('Muzzarella').slug, 'muzzarella-2')
        self.assertEqual(self._product('Muzzarella', business=self.other_profile).slug, 'muzzarella')

    def test_category_slugs_unique_per_business(self):
        self.assertEqual(self._category('Bebidas').slug, 'bebidas')
        self.assertEqual(self._category('Bebidas').slug, 'bebidas-2')
        self.assertEqual(self._category('Bebidas', business=self.other_profile).slug, 'bebidas')

    def test_sin_categoria_is_reserved_for_uncategorized_products(self):
        self.assertEqual(self._category('Sin categoría').slug, 'sin-categoria-2')
        self.assertEqual(self._category('SIN CATEGORIA').slug, 'sin-categoria-3')

    def test_legacy_product_segment_is_reserved(self):
        # `/<business>/product/<id>` is the legacy route; a category slug of
        # `product` would make its canonical links unreachable.
        self.assertEqual(self._category('Product').slug, 'product-2')

    def test_long_names_are_truncated_with_room_for_suffix(self):
        category = self._category('Largos')
        name = 'b' * 255
        first = self._product(name, category)
        second = self._product(name, category)
        self.assertEqual(first.slug, 'b' * 255)
        self.assertEqual(second.slug, 'b' * 253 + '-2')

    def test_slug_is_stable_on_rename(self):
        category = self._category('Pizzas')
        product = self._product('Muzzarella', category)
        product.name = 'Napolitana'
        product.save()
        category.name = 'Pizzas a la piedra'
        category.save()
        product.refresh_from_db()
        category.refresh_from_db()
        self.assertEqual(product.slug, 'muzzarella')
        self.assertEqual(category.slug, 'pizzas')

    def test_slug_is_stable_on_rename_through_the_admin_api(self):
        category = self._category('Pizzas')
        product = self._product('Muzzarella', category)
        response = self.auth_client(self.user).patch(
            f'/api/v1/products/products/{product.id}/', {'name': 'Napolitana'},
        )
        self.assertEqual(response.status_code, 200, response.data)
        product.refresh_from_db()
        self.assertEqual(product.name, 'Napolitana')
        self.assertEqual(product.slug, 'muzzarella')

    def test_api_create_allocates_slugs(self):
        client = self.auth_client(self.user)
        response = client.post('/api/v1/products/product-categories/', {'name': 'Sin categoría', 'icon': 'fa-box'})
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data['slug'], 'sin-categoria-2')
        category = ProductCategory.objects.get(pk=response.data['id'])
        response = client.post('/api/v1/products/products/', {
            'name': 'Café', 'description': 'Desc', 'price': '1.00', 'category': category.id,
        })
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(Product.objects.get(business=self.profile, name='Café').slug, 'cafe')

    def test_category_move_keeps_slug_and_resuffixes_on_collision(self):
        pizzas = self._category('Pizzas')
        promos = self._category('Promos')
        self._product('Muzzarella', pizzas)
        moving = self._product('Muzzarella', promos)
        unique = self._product('Fugazzeta', promos)

        unique.category = pizzas
        unique.save()
        self.assertEqual(unique.slug, 'fugazzeta')

        moving.category = pizzas
        moving.save()
        moving.refresh_from_db()
        self.assertEqual(moving.slug, 'muzzarella-2')

        # Moving out to uncategorized also checks the uncategorized scope.
        self._product('Fugazzeta')
        unique.category = None
        unique.save()
        unique.refresh_from_db()
        self.assertEqual(unique.slug, 'fugazzeta-2')

    def test_concurrent_insert_collision_is_retried(self):
        """Simulate a concurrent insert: the first allocation sees a stale
        scope and returns a slug that is already taken (as if another request
        committed between our check and our insert). The DB constraint raises
        IntegrityError and save() re-allocates."""
        category = self._category('Pizzas')
        self._product('Muzzarella', category)
        real_allocate = slugs_module.allocate_unique_slug
        calls = []

        def stale_then_real(base, exists_qs, reserved=frozenset()):
            calls.append(base)
            if len(calls) == 1:
                return base  # stale view: doesn't see the committed 'muzzarella'
            return real_allocate(base, exists_qs, reserved)

        with mock.patch.object(slugs_module, 'allocate_unique_slug', side_effect=stale_then_real):
            product = self._product('Muzzarella', category)

        self.assertEqual(len(calls), 2)
        self.assertEqual(product.slug, 'muzzarella-2')
        self.assertTrue(Product.objects.filter(pk=product.pk, slug='muzzarella-2').exists())
        self.assertEqual(Product.objects.filter(category=category).count(), 2)

    def test_concurrent_collision_gives_up_after_max_attempts(self):
        category = self._category('Pizzas')
        self._product('Muzzarella', category)
        with mock.patch.object(slugs_module, 'allocate_unique_slug', side_effect=lambda base, *a, **k: base):
            with self.assertRaises(IntegrityError):
                self._product('Muzzarella', category)
        self.assertEqual(Product.objects.filter(category=category).count(), 1)

    def test_non_slug_integrity_errors_are_not_retried(self):
        category = self._category('Pizzas')
        self._product('Primero', category, stopper='FAVORITE')
        with mock.patch.object(
            slugs_module, 'allocate_unique_slug', wraps=slugs_module.allocate_unique_slug,
        ) as allocate:
            with self.assertRaises(IntegrityError):
                self._product('Segundo', category, stopper='FAVORITE')
        self.assertEqual(allocate.call_count, 1)

    def test_db_constraints_reject_duplicate_slugs(self):
        category = self._category('Pizzas')
        first = self._product('Muzzarella', category)
        second = self._product('Otra', category)
        with self.assertRaises(IntegrityError), transaction.atomic():
            Product.objects.filter(pk=second.pk).update(slug=first.slug)
        loose_a = self._product('Suelta')
        loose_b = self._product('Otra suelta')
        with self.assertRaises(IntegrityError), transaction.atomic():
            Product.objects.filter(pk=loose_b.pk).update(slug=loose_a.slug)
        other_category = self._category('Otra')
        with self.assertRaises(IntegrityError), transaction.atomic():
            ProductCategory.objects.filter(pk=other_category.pk).update(slug=category.slug)

    def _drop_slug_constraints(self):
        """Put the schema back in its 0018/0019 shape (nullable, no uniqueness)
        for the rest of this test; the TestCase transaction rolls it back."""
        with connection.cursor() as cursor:
            # Flush the deferred FK checks queued by setUp's inserts; PostgreSQL
            # refuses ALTER TABLE while trigger events are pending.
            cursor.execute('SET CONSTRAINTS ALL IMMEDIATE')
        with connection.schema_editor() as editor:
            for model in (ProductCategory, Product):
                for constraint in model._meta.constraints:
                    if 'slug' in constraint.name:
                        editor.remove_constraint(model, constraint)
                old_field = model._meta.get_field('slug')
                new_field = models.SlugField(max_length=SLUG_MAX_LENGTH, editable=False, db_index=False, null=True)
                new_field.set_attributes_from_name('slug')
                new_field.model = model
                editor.alter_field(model, old_field, new_field)

    @skipIf(connection.vendor == 'sqlite', 'needs transactional DDL inside the test transaction')
    def test_backfill_is_deterministic_and_collision_safe(self):
        pizzas = self._category('Pizzas')
        pizzas_dup = self._category('Pizzas')
        reserved = self._category('Sin categoría')
        a = self._product('Muzzarella', pizzas)
        b = self._product('Muzzarella', pizzas)
        c = self._product('Muzzarella', pizzas_dup)
        d = self._product('Suelto')
        e = self._product('Suelto')
        f = self._product('寿司')
        other = self._product('Muzzarella', business=self.other_profile)
        kept = self._product('Ya tenía slug', pizzas)

        # Simulate the pre-migration state: every row but one without a slug.
        self._drop_slug_constraints()
        Product.objects.exclude(pk=kept.pk).update(slug=None)
        ProductCategory.objects.update(slug='')

        self.assertEqual(backfill_missing_slugs(ProductCategory, Product), (3, 7))

        def slug_of(obj):
            obj.refresh_from_db()
            return obj.slug

        self.assertEqual(
            [slug_of(x) for x in (pizzas, pizzas_dup, reserved)],
            ['pizzas', 'pizzas-2', 'sin-categoria-2'],
        )
        self.assertEqual(
            [slug_of(x) for x in (a, b, c, d, e, f, other, kept)],
            ['muzzarella', 'muzzarella-2', 'muzzarella', 'suelto', 'suelto-2',
             f'p-{f.pk}', 'muzzarella', 'ya-tenia-slug'],
        )
        # Idempotent: nothing left to fill.
        self.assertEqual(backfill_missing_slugs(ProductCategory, Product), (0, 0))


class TestProductExtraFields(MarkyAPITestCase):
    """Featured ingredients, presentation, allergens and SIN TACC (celiac_info)."""

    URL = '/api/v1/products/products/'

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.user, cls.profile = cls.make_user('extras_user', 'extras@test.com')

    def _create(self, **extra):
        payload = {'name': 'P', 'description': 'D', 'price': '10.00'}
        payload.update(extra)
        return self.auth_client(self.user).post(self.URL, payload, format='multipart')

    def _patch(self, product, **data):
        return self.auth_client(self.user).patch(
            f'{self.URL}{product.id}/', data, format='multipart',
        )

    def _product(self, **kwargs):
        return Product.objects.create(
            name='P', description='D', price=Decimal('10.00'), business=self.profile, **kwargs
        )

    def test_create_without_new_fields_defaults_to_null(self):
        response = self._create()
        self.assertEqual(response.status_code, 201, response.data)
        product = Product.objects.latest('id')
        self.assertIsNone(product.featured_ingredients)
        self.assertIsNone(product.presentation)
        self.assertIsNone(product.allergens)
        self.assertIsNone(product.celiac_info)

    def test_existing_product_serializes_null_fields(self):
        product = self._product()
        response = self.auth_client(self.user).get(f'{self.URL}{product.id}/')
        self.assertEqual(response.status_code, 200)
        for field in ('featured_ingredients', 'presentation', 'allergens', 'celiac_info'):
            self.assertIsNone(response.data[field])

    def test_create_with_all_fields(self):
        presentation = {
            'version': 1,
            'amount': {'type': 'weight', 'value': 1.5, 'unit': 'kg'},
            'dimensions': {'shape': 'round', 'diameterCm': 22, 'lengthCm': None,
                           'widthCm': None, 'heightCm': 8},
            'approximateYield': {'minPeople': 12, 'maxPeople': 15},
        }
        celiac = {'version': 1, 'crossContaminationControl': True,
                  'glutenFreeGrains': False, 'certifiedProtocol': True}
        response = self._create(
            featured_ingredients='Tomate,Albahaca',
            allergens='egg,milk',
            presentation=json.dumps(presentation),
            celiac_info=json.dumps(celiac),
        )
        self.assertEqual(response.status_code, 201, response.data)
        product = Product.objects.latest('id')
        self.assertEqual(product.featured_ingredients, 'Tomate,Albahaca')
        self.assertEqual(product.allergens, 'milk,egg')  # canonical catalog order
        self.assertEqual(product.presentation, presentation)
        self.assertEqual(product.celiac_info, celiac)
        self.assertEqual(response.data['presentation']['amount']['value'], 1.5)

    def test_clear_with_empty_string(self):
        product = self._product(
            featured_ingredients='A', allergens='milk',
            presentation={'version': 1, 'amount': None, 'dimensions': None,
                          'approximateYield': {'minPeople': 2, 'maxPeople': None}},
            celiac_info={'version': 1, 'crossContaminationControl': True,
                         'glutenFreeGrains': True, 'certifiedProtocol': True},
        )
        response = self._patch(
            product, featured_ingredients='', allergens='', presentation='', celiac_info='',
        )
        self.assertEqual(response.status_code, 200, response.data)
        product.refresh_from_db()
        self.assertIsNone(product.featured_ingredients)
        self.assertIsNone(product.allergens)
        self.assertIsNone(product.presentation)
        self.assertIsNone(product.celiac_info)

    def test_omitted_fields_are_preserved_on_patch(self):
        product = self._product(featured_ingredients='A,B', allergens='soy')
        response = self._patch(product, price='12.00')
        self.assertEqual(response.status_code, 200, response.data)
        product.refresh_from_db()
        self.assertEqual(product.featured_ingredients, 'A,B')
        self.assertEqual(product.allergens, 'soy')

    def test_ingredients_rules(self):
        self.assertEqual(self._create(featured_ingredients='a,A').status_code, 400)
        self.assertEqual(self._create(featured_ingredients=','.join(str(i) for i in range(9))).status_code, 400)
        self.assertEqual(self._create(featured_ingredients='a,,b').status_code, 400)
        ok = self._create(featured_ingredients=','.join(str(i) for i in range(8)))
        self.assertEqual(ok.status_code, 201, ok.data)

    def test_unknown_allergen_rejected(self):
        response = self._create(allergens='milk,kryptonite')
        self.assertEqual(response.status_code, 400)
        self.assertIn('allergens', response.data)

    def _presentation(self, **overrides):
        base = {'version': 1, 'amount': None, 'dimensions': None, 'approximateYield': None}
        base.update(overrides)
        return json.dumps(base)

    def test_invalid_presentation_payloads(self):
        bad = [
            {'amount': {'type': 'units', 'value': 0, 'unit': None}},
            {'amount': {'type': 'units', 'value': 1.5, 'unit': None}},
            {'amount': {'type': 'units', 'value': 2, 'unit': 'g'}},
            {'amount': {'type': 'weight', 'value': 2, 'unit': 'ml'}},
            {'amount': {'type': 'weight', 'value': -1, 'unit': 'g'}},
            {'amount': {'type': 'bogus', 'value': 1, 'unit': None}},
            {'amount': {'type': 'weight', 'value': '1', 'unit': 'g'}},
            {'dimensions': {'shape': 'round', 'diameterCm': None, 'heightCm': None}},
            {'dimensions': {'shape': 'round', 'diameterCm': 10, 'lengthCm': 5}},
            {'dimensions': {'shape': 'rectangular', 'lengthCm': 10}},
            {'dimensions': {'shape': 'round', 'diameterCm': 10, 'heightCm': 0}},
            {'approximateYield': {'minPeople': 0, 'maxPeople': None}},
            {'approximateYield': {'minPeople': 5, 'maxPeople': 4}},
            {'approximateYield': {'minPeople': 5, 'extra': 1}},
        ]
        for overrides in bad:
            with self.subTest(overrides=overrides):
                response = self._create(presentation=self._presentation(**overrides))
                self.assertEqual(response.status_code, 400, overrides)
                self.assertIn('presentation', response.data)

    def test_presentation_malformed_json_and_unknown_keys(self):
        self.assertEqual(self._create(presentation='{not json').status_code, 400)
        self.assertEqual(self._create(presentation=json.dumps({'version': 2})).status_code, 400)
        self.assertEqual(
            self._create(presentation=json.dumps({'version': 1, 'foo': 1})).status_code, 400
        )

    def test_celiac_validation(self):
        full = {'version': 1, 'crossContaminationControl': False,
                'glutenFreeGrains': False, 'certifiedProtocol': False}
        ok = self._create(celiac_info=json.dumps(full))
        self.assertEqual(ok.status_code, 201, ok.data)  # enabled with zero declarations allowed
        for bad in (
            {**full, 'glutenFreeGrains': 'yes'},
            {**full, 'extra': True},
            {'version': 1, 'crossContaminationControl': True},
            {**full, 'version': 2},
        ):
            with self.subTest(bad=bad):
                self.assertEqual(self._create(celiac_info=json.dumps(bad)).status_code, 400)

    def test_presentation_hostile_payloads_return_400_not_500(self):
        hostile = [
            {'version': 1, 'amount': {'type': [], 'value': 1, 'unit': None}},
            {'version': 1, 'dimensions': {'shape': {}}},
            {'version': 1, 'amount': {'type': 'weight', 'value': 10 ** 400, 'unit': 'g'}},
            {'version': 1, 'amount': {'type': 'weight', 'value': 1e308, 'unit': 'g'}},
            {'version': 1, 'dimensions': {'shape': 'round', 'diameterCm': 1e-320}},
            {'version': 1, 'approximateYield': {'minPeople': 10 ** 30, 'maxPeople': None}},
            {'version': True},
        ]
        for payload in hostile:
            with self.subTest(payload=str(payload)[:60]):
                response = self._create(presentation=json.dumps(payload))
                self.assertEqual(response.status_code, 400)
        self.assertEqual(self._create(presentation='[' * 100000).status_code, 400)
        self.assertEqual(self._create(presentation='[]').status_code, 400)

    def test_ingredient_named_null_is_stored_not_cleared(self):
        response = self._create(featured_ingredients='Null')
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(Product.objects.latest('id').featured_ingredients, 'Null')

    def test_new_fields_work_when_request_also_has_nested_keys(self):
        # Bracket keys make drf_nested_forms coerce "true"/"123" etc.
        response = self._create(
            featured_ingredients='true',
            allergens='milk',
            presentation=json.dumps({
                'version': 1, 'amount': None, 'dimensions': None,
                'approximateYield': {'minPeople': 3, 'maxPeople': None},
            }),
            celiac_info='',
            **{'addons[0][name]': 'Extra', 'addons[0][price]': '1.00'},
        )
        self.assertEqual(response.status_code, 201, response.data)
        product = Product.objects.latest('id')
        self.assertEqual(product.featured_ingredients, 'true')
        self.assertEqual(product.allergens, 'milk')
        self.assertEqual(product.presentation['approximateYield']['minPeople'], 3)
        self.assertIsNone(product.celiac_info)

    def test_put_without_new_fields_is_accepted(self):
        product = self._product()
        response = self.auth_client(self.user).put(
            f'{self.URL}{product.id}/',
            {'name': 'P2', 'description': 'D', 'price': '10.00'},
            format='multipart',
        )
        self.assertEqual(response.status_code, 200, response.data)

    def test_category_empty_string_clears_category_on_patch(self):
        category = ProductCategory.objects.create(name='Bebidas', business=self.profile)
        product = self._product(category=category)
        response = self._patch(product, category='')
        self.assertEqual(response.status_code, 200, response.data)
        product.refresh_from_db()
        self.assertIsNone(product.category)


class TestProductManualOrder(MarkyAPITestCase):
    """Manual product ordering per category: display order, assignment on
    create/move, and the update_products_order endpoint."""

    PRODUCTS_URL = '/api/v1/products/products/'
    CATEGORIES_URL = '/api/v1/products/product-categories/'

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.user, cls.profile = cls.make_user('order_user', 'order_user@test.com')
        cls.other_user, cls.other_profile = cls.make_user('order_other', 'order_other@test.com')

    def setUp(self):
        self.category = ProductCategory.objects.create(business=self.profile, name='Cat', icon='icon', order=1)
        self.client_ = self.auth_client(self.user)

    def _product(self, name, order, category='default', stopper=None, profile=None, **kwargs):
        return Product.objects.create(
            name=name, description='D', price=Decimal('10.00'),
            business=profile or self.profile,
            category=self.category if category == 'default' else category,
            order=order, stopper=stopper, **kwargs,
        )

    def _with_products_ids(self, query='', client=None):
        response = (client or self.client_).get(f'{self.CATEGORIES_URL}with_products/{query}')
        self.assertEqual(response.status_code, 200, response.data)
        for category in response.data['results']:
            if category['id'] == self.category.id:
                return [p['id'] for p in category['products']]
        return None

    def _update_order(self, product_ids, category=None, client=None):
        category = category or self.category
        return (client or self.client_).post(
            f'{self.CATEGORIES_URL}{category.id}/update_products_order/',
            {'product_ids': product_ids}, format='json',
        )

    # --- display ordering -------------------------------------------------

    def test_with_products_orders_stoppers_first_then_manual_order(self):
        regular_b = self._product('B', order=2)
        regular_a = self._product('A', order=1)
        recommended = self._product('R', order=9, stopper='RECOMMENDED')
        favorite = self._product('F', order=8, stopper='FAVORITE')

        self.assertEqual(
            self._with_products_ids(),
            [favorite.id, recommended.id, regular_a.id, regular_b.id],
        )

    def test_ties_on_order_break_by_id(self):
        first = self._product('First', order=0)
        second = self._product('Second', order=0)
        self.assertEqual(self._with_products_ids(), [first.id, second.id])

    def test_ordering_holds_under_has_promotion_filter(self):
        later = self._product('Later', order=2, discount_percentage=Decimal('10'))
        earlier = self._product('Earlier', order=1, discount_percentage=Decimal('10'))
        favorite = self._product('Fav', order=5, stopper='FAVORITE', discount_percentage=Decimal('10'))
        self._product('NoPromo', order=0)

        self.assertEqual(
            self._with_products_ids('?has_promotion=true'),
            [favorite.id, earlier.id, later.id],
        )

    def test_ordering_holds_on_public_catalog(self):
        second = self._product('Second', order=2)
        first = self._product('First', order=1)
        favorite = self._product('Fav', order=3, stopper='FAVORITE')
        self._product('Hidden', order=0, is_active=False)

        response = self.client.get(f'/api/v1/public/business/{self.profile.business_id}/catalog/')
        self.assertEqual(response.status_code, 200)
        ids = [p['id'] for c in response.data['results'] if c['id'] == self.category.id for p in c['products']]
        self.assertEqual(ids, [favorite.id, first.id, second.id])

    def test_uncategorized_products_follow_order(self):
        second = self._product('Second', order=2, category=None)
        first = self._product('First', order=1, category=None)
        response = self.client_.get(f'{self.CATEGORIES_URL}with_products/')
        bucket = next(c for c in response.data['results'] if c['id'] is None)
        self.assertEqual([p['id'] for p in bucket['products']], [first.id, second.id])

    def test_losing_stopper_falls_back_to_stored_order(self):
        a = self._product('A', order=1)
        favorite = self._product('Fav', order=2, stopper='FAVORITE')
        b = self._product('B', order=3)
        self.assertEqual(self._with_products_ids(), [favorite.id, a.id, b.id])

        favorite.stopper = None
        favorite.save()
        self.assertEqual(self._with_products_ids(), [a.id, favorite.id, b.id])

    # --- assignment on create / move -------------------------------------

    def test_create_appends_to_end_of_category(self):
        self._product('A', order=1)
        self._product('B', order=7)
        response = self.client_.post(
            self.PRODUCTS_URL,
            {'name': 'New', 'description': 'D', 'price': '5.00', 'category': self.category.id},
            format='multipart',
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(Product.objects.latest('id').order, 8)

    def test_create_in_empty_category_starts_at_one(self):
        response = self.client_.post(
            self.PRODUCTS_URL,
            {'name': 'New', 'description': 'D', 'price': '5.00', 'category': self.category.id},
            format='multipart',
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(Product.objects.latest('id').order, 1)

    def test_moving_to_another_category_appends_to_end(self):
        other = ProductCategory.objects.create(business=self.profile, name='Other', icon='icon', order=2)
        self._product('Existing', order=4, category=other)
        moved = self._product('Moved', order=1)

        response = self.client_.patch(
            f'{self.PRODUCTS_URL}{moved.id}/', {'category': other.id}, format='multipart',
        )
        self.assertEqual(response.status_code, 200, response.data)
        moved.refresh_from_db()
        self.assertEqual(moved.order, 5)

    def test_edit_in_same_category_keeps_order(self):
        self._product('A', order=1)
        target = self._product('B', order=2)
        response = self.client_.patch(
            f'{self.PRODUCTS_URL}{target.id}/',
            {'name': 'Renamed', 'category': self.category.id}, format='multipart',
        )
        self.assertEqual(response.status_code, 200, response.data)
        target.refresh_from_db()
        self.assertEqual(target.order, 2)

    # --- update_products_order endpoint ----------------------------------

    def test_update_products_order_persists_and_is_reflected(self):
        a = self._product('A', order=1)
        b = self._product('B', order=2)
        c = self._product('C', order=3)

        response = self._update_order([c.id, a.id, b.id])
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(self._with_products_ids(), [c.id, a.id, b.id])
        self.assertEqual(
            list(Product.objects.filter(category=self.category).order_by('order').values_list('id', 'order')),
            [(c.id, 1), (a.id, 2), (b.id, 3)],
        )

    def test_update_products_order_rejects_duplicate_ids(self):
        a = self._product('A', order=1)
        b = self._product('B', order=2)
        response = self._update_order([a.id, a.id, b.id])
        self.assertEqual(response.status_code, 400)
        self.assertIn('error', response.data)

    def test_update_products_order_rejects_missing_id(self):
        a = self._product('A', order=1)
        self._product('B', order=2)
        response = self._update_order([a.id])
        self.assertEqual(response.status_code, 400)
        self.assertIn('error', response.data)

    def test_update_products_order_rejects_foreign_id(self):
        a = self._product('A', order=1)
        foreign = self._product('Theirs', order=1, category=None, profile=self.other_profile)
        response = self._update_order([a.id, foreign.id])
        self.assertEqual(response.status_code, 400)
        foreign.refresh_from_db()
        self.assertEqual(foreign.order, 1)

    def test_update_products_order_failure_leaves_order_untouched(self):
        a = self._product('A', order=1)
        b = self._product('B', order=2)
        self._update_order([b.id, b.id])
        a.refresh_from_db()
        b.refresh_from_db()
        self.assertEqual((a.order, b.order), (1, 2))

    def test_update_products_order_other_business_category_is_404(self):
        theirs = ProductCategory.objects.create(business=self.other_profile, name='Theirs', icon='icon')
        response = self._update_order([], category=theirs)
        self.assertEqual(response.status_code, 404)

    def test_update_products_order_empty_category_with_empty_list_is_ok(self):
        response = self._update_order([])
        self.assertEqual(response.status_code, 200)

    def test_update_products_order_empty_list_on_nonempty_category_is_400(self):
        self._product('A', order=1)
        response = self._update_order([])
        self.assertEqual(response.status_code, 400)

    def test_update_products_order_requires_authentication(self):
        response = self.client.post(
            f'{self.CATEGORIES_URL}{self.category.id}/update_products_order/',
            {'product_ids': []}, format='json',
        )
        self.assertIn(response.status_code, (401, 403))

    def test_update_products_order_rejects_malformed_body(self):
        response = self.client_.post(
            f'{self.CATEGORIES_URL}{self.category.id}/update_products_order/',
            {'product_ids': 'nope'}, format='json',
        )
        self.assertEqual(response.status_code, 400)

    # --- migration backfill ----------------------------------------------

    def test_next_product_order_helper(self):
        from products.models import next_product_order
        self.assertEqual(next_product_order(self.category), 1)
        self._product('A', order=4)
        self.assertEqual(next_product_order(self.category), 5)
        self.assertEqual(next_product_order(None), 1)


class TestPromotionTypeExclusivity(MarkyAPITestCase):
    """A single Product/ProductCategory row carries ONE promotion type:
    a positive discount and a multibuy option are mutually exclusive, and the
    effective API bundle never mixes the product's and category's types."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.user, cls.profile = cls.make_user('promo_excl_user', 'promo_excl@test.com')

    def setUp(self):
        self.category = ProductCategory.objects.create(business=self.profile, name='Excl Cat', icon='icon')
        self.client_api = self.auth_client(self.user)

    def _product(self, **overrides):
        defaults = dict(
            name='Excl Product', description='Desc', price=Decimal('10.00'),
            business=self.profile, category=self.category,
        )
        defaults.update(overrides)
        return Product.objects.create(**defaults)

    def _with_products_card(self, product):
        response = self.client_api.get('/api/v1/products/product-categories/with_products/')
        self.assertEqual(response.status_code, 200)
        data = response.data
        categories = data['results'] if isinstance(data, dict) and 'results' in data else data
        for category in categories:
            for card in category['products']:
                if card['id'] == product.id:
                    return card
        self.fail('product not found in with_products response')

    # --- product writes -------------------------------------------------

    def test_product_create_with_discount_and_multibuy_rejected(self):
        response = self.client_api.post(
            '/api/v1/products/products/',
            {'name': 'Both', 'description': 'Desc', 'price': '10.00',
             'discount_percentage': '30', 'multibuy_option': '3x2'},
            format='multipart',
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn('multibuy_option', response.data)

    def test_product_patch_adding_multibuy_without_clearing_discount_rejected(self):
        product = self._product(discount_percentage=Decimal('30.00'))
        response = self.client_api.patch(
            f'/api/v1/products/products/{product.id}/',
            {'multibuy_option': '3x2'}, format='multipart',
        )
        self.assertEqual(response.status_code, 400)
        product.refresh_from_db()
        self.assertEqual(product.discount_percentage, Decimal('30.00'))
        self.assertFalse(product.multibuy_option)

    def test_product_patch_adding_discount_without_clearing_multibuy_rejected(self):
        product = self._product(multibuy_option='3x2')
        response = self.client_api.patch(
            f'/api/v1/products/products/{product.id}/',
            {'discount_percentage': '30'}, format='multipart',
        )
        self.assertEqual(response.status_code, 400)

    def test_product_switch_discount_to_multibuy_with_clear_accepted(self):
        product = self._product(discount_percentage=Decimal('30.00'))
        response = self.client_api.patch(
            f'/api/v1/products/products/{product.id}/',
            {'multibuy_option': '3x2', 'discount_percentage': '0'}, format='multipart',
        )
        self.assertEqual(response.status_code, 200)
        product.refresh_from_db()
        self.assertEqual(product.multibuy_option, '3x2')
        self.assertEqual(product.discount_percentage, Decimal('0'))

    def test_product_switch_multibuy_to_discount_with_clear_accepted(self):
        product = self._product(multibuy_option='3x2')
        response = self.client_api.patch(
            f'/api/v1/products/products/{product.id}/',
            {'discount_percentage': '30', 'multibuy_option': ''}, format='multipart',
        )
        self.assertEqual(response.status_code, 200)
        product.refresh_from_db()
        self.assertFalse(product.multibuy_option)
        self.assertEqual(product.discount_percentage, Decimal('30'))

    def test_unrelated_save_of_legacy_conflicting_row_still_allowed(self):
        product = self._product(discount_percentage=Decimal('30.00'), multibuy_option='3x2')
        response = self.client_api.patch(
            f'/api/v1/products/products/{product.id}/', {'price': '12.00'}, format='multipart',
        )
        self.assertEqual(response.status_code, 200)

    def test_date_only_patch_of_legacy_conflicting_product_allowed(self):
        now = timezone.now()
        product = self._product(discount_percentage=Decimal('30.00'), multibuy_option='3x2')
        response = self.client_api.patch(
            f'/api/v1/products/products/{product.id}/',
            {'promotion_starts_at': (now - timedelta(days=2)).isoformat(),
             'promotion_ends_at': (now - timedelta(days=1)).isoformat()},
            format='multipart',
        )
        self.assertEqual(response.status_code, 200)

    def test_date_only_add_promotion_of_legacy_conflicting_category_allowed(self):
        now = timezone.now()
        self.category.discount_percentage = Decimal('30.00')
        self.category.multibuy_option = '3x2'
        self.category.save()
        response = self.client_api.post(
            f'/api/v1/products/product-categories/{self.category.id}/add_promotion/',
            {'promotion_starts_at': now.isoformat(),
             'promotion_ends_at': (now + timedelta(days=1)).isoformat()},
            format='json',
        )
        self.assertEqual(response.status_code, 200)

    # --- category writes ------------------------------------------------

    def test_category_add_promotion_mixing_types_rejected(self):
        self.category.discount_percentage = Decimal('30.00')
        self.category.save()
        response = self.client_api.post(
            f'/api/v1/products/product-categories/{self.category.id}/add_promotion/',
            {'multibuy_option': '3x2'}, format='json',
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn('multibuy_option', response.data)

    def test_category_add_promotion_switch_with_clear_accepted(self):
        self.category.discount_percentage = Decimal('30.00')
        self.category.save()
        response = self.client_api.post(
            f'/api/v1/products/product-categories/{self.category.id}/add_promotion/',
            {'multibuy_option': '3x2', 'discount_percentage': 0}, format='json',
        )
        self.assertEqual(response.status_code, 200)
        self.category.refresh_from_db()
        self.assertEqual(self.category.multibuy_option, '3x2')
        self.assertEqual(self.category.discount_percentage, Decimal('0'))

    def test_category_patch_mixing_types_rejected(self):
        self.category.multibuy_option = '3x2'
        self.category.save()
        response = self.client_api.patch(
            f'/api/v1/products/product-categories/{self.category.id}/',
            {'discount_percentage': '30'}, format='json',
        )
        self.assertEqual(response.status_code, 400)

    # --- effective response --------------------------------------------

    def test_effective_response_product_multibuy_over_category_discount(self):
        now = timezone.now()
        self.category.discount_percentage = Decimal('30.00')
        self.category.promotion_starts_at = now - timedelta(hours=1)
        self.category.promotion_ends_at = now + timedelta(days=1)
        self.category.save()
        product = self._product(
            multibuy_option='3x2',
            promotion_starts_at=now - timedelta(hours=1), promotion_ends_at=now + timedelta(days=1),
        )
        card = self._with_products_card(product)
        self.assertEqual(card['multibuy_option'], '3x2')
        self.assertEqual(Decimal(str(card['discount_percentage'])), Decimal('0'))

    def test_effective_response_product_discount_over_category_multibuy(self):
        now = timezone.now()
        self.category.multibuy_option = '3x2'
        self.category.promotion_starts_at = now - timedelta(hours=1)
        self.category.promotion_ends_at = now + timedelta(days=1)
        self.category.save()
        product = self._product(
            discount_percentage=Decimal('30.00'),
            promotion_starts_at=now - timedelta(hours=1), promotion_ends_at=now + timedelta(days=1),
        )
        card = self._with_products_card(product)
        self.assertEqual(Decimal(str(card['discount_percentage'])), Decimal('30'))
        self.assertFalse(card['multibuy_option'])
