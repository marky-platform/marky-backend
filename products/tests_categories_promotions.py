"""Fase G2a: F-28 (categorías sin paginar + tope por negocio) y F-29 (ventanas de promoción)."""
from datetime import timedelta
from decimal import Decimal
from io import StringIO
from unittest import mock

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db.models.query import QuerySet
from django.utils import timezone

from notifications.models import Notification
from products.filters import ProductCategoryFilter, product_has_active_promotion_q
from products.models import Product, ProductCategory
from utils.tests_base import MarkyAPITestCase

CATEGORIES_URL = '/api/v1/products/product-categories/'


def make_categories(profile, count, start=1):
    ProductCategory.objects.bulk_create([
        ProductCategory(business=profile, name=f'Cat {i:03d}', icon='fa-pizza', order=i)
        for i in range(start, start + count)
    ])


class CategoriesNotPaginatedTests(MarkyAPITestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.user, cls.profile = cls.make_user('bigcats', 'bigcats@test.com')

    def _with_products_for_all(self):
        cats = list(ProductCategory.objects.filter(business=self.profile))
        Product.objects.bulk_create([
            Product(name=f'P{c.id}', description='d', price=Decimal('1.00'),
                    business=self.profile, category=c, is_active=True)
            for c in cats
        ])

    def test_admin_list_returns_all_categories_in_envelope(self):
        make_categories(self.profile, 75)
        data = self.auth_client(self.user).get(CATEGORIES_URL).json()
        self.assertEqual(len(data['results']), 75)
        self.assertEqual(data['count'], 75)
        self.assertIsNone(data['next'])

    def test_admin_list_ignores_page_size_param(self):
        make_categories(self.profile, 60)
        data = self.auth_client(self.user).get(CATEGORIES_URL, {'page_size': 10}).json()
        self.assertEqual(len(data['results']), 60)

    def test_with_products_returns_all_and_sin_categoria_once(self):
        make_categories(self.profile, 75)
        self._with_products_for_all()
        Product.objects.create(name='Suelto', description='d', price=Decimal('1.00'), business=self.profile)
        data = self.auth_client(self.user).get(CATEGORIES_URL + 'with_products/').json()
        names = [c['name'] for c in data['results']]
        self.assertEqual(len(names), 76)
        self.assertEqual(names.count('Sin categoría'), 1)
        self.assertEqual(data['products_count'], 76)
        self.assertEqual(data['count'], len(data['results']))
        self.assertEqual(data['count'], 76)

    def test_public_catalog_returns_all_and_sin_categoria_once(self):
        make_categories(self.profile, 75)
        self._with_products_for_all()
        Product.objects.create(name='Suelto', description='d', price=Decimal('1.00'),
                               business=self.profile, is_active=True)
        data = self.client.get(f'/api/v1/public/business/{self.profile.business_id}/catalog/').json()
        names = [c['name'] for c in data['results']]
        self.assertEqual(len(names), 76)
        self.assertEqual(names.count('Sin categoría'), 1)
        self.assertEqual(data['count'], len(data['results']))
        self.assertEqual(data['count'], 76)

    def test_public_categories_returns_all(self):
        make_categories(self.profile, 75)
        self._with_products_for_all()
        data = self.client.get(f'/api/v1/public/business/{self.profile.business_id}/categories/').json()
        self.assertEqual(len(data['results']), 75)

    def test_products_remain_paginated(self):
        Product.objects.bulk_create([
            Product(name=f'P{i}', description='d', price=Decimal('1.00'), business=self.profile)
            for i in range(55)
        ])
        data = self.auth_client(self.user).get('/api/v1/products/products/').json()
        self.assertEqual(len(data['results']), 50)
        self.assertEqual(data['count'], 55)
        self.assertIsNotNone(data['next'])


class CategoryCapTests(MarkyAPITestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.user, cls.profile = cls.make_user('capcats', 'capcats@test.com')
        cls.other_user, cls.other_profile = cls.make_user('othercap', 'othercap@test.com')

    def _create(self, name='Nueva'):
        return self.auth_client(self.user).post(CATEGORIES_URL, {'name': name, 'icon': 'fa-pizza'}, format='json')

    def test_default_cap_is_100(self):
        self.assertEqual(settings.MAX_CATEGORIES_PER_BUSINESS, 100)

    def test_create_below_cap_allowed(self):
        make_categories(self.profile, settings.MAX_CATEGORIES_PER_BUSINESS - 1)
        self.assertEqual(self._create().status_code, 201)
        self.assertEqual(ProductCategory.objects.filter(business=self.profile).count(), 100)

    def test_create_at_cap_rejected_with_spanish_message(self):
        make_categories(self.profile, settings.MAX_CATEGORIES_PER_BUSINESS)
        response = self._create()
        self.assertEqual(response.status_code, 400)
        self.assertIn('100', response.json()['error'])
        self.assertIn('categorías', response.json()['error'])
        self.assertEqual(ProductCategory.objects.filter(business=self.profile).count(), 100)

    def test_boundary_99_to_100_ok_then_100_rejected(self):
        make_categories(self.profile, 99)
        self.assertEqual(self._create('Cien').status_code, 201)
        self.assertEqual(self._create('Ciento uno').status_code, 400)
        self.assertEqual(ProductCategory.objects.filter(business=self.profile).count(), 100)

    def test_create_locks_business_row(self):
        # El chequeo del tope + la creación se serializan por negocio (select_for_update).
        with mock.patch.object(QuerySet, 'select_for_update', autospec=True,
                               side_effect=QuerySet.select_for_update) as locked:
            self.assertEqual(self._create().status_code, 201)
        self.assertTrue(locked.called)

    def test_cap_is_per_business(self):
        make_categories(self.other_profile, settings.MAX_CATEGORIES_PER_BUSINESS)
        self.assertEqual(self._create().status_code, 201)


class CategoryWindowValidationTests(MarkyAPITestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.user, cls.profile = cls.make_user('catwin', 'catwin@test.com')
        cls.category = ProductCategory.objects.create(business=cls.profile, name='Platos', icon='x', order=1)

    def _promo(self, starts, ends, **extra):
        body = {'discount_percentage': '10.00', 'promotion_starts_at': starts, 'promotion_ends_at': ends}
        body.update(extra)
        return self.auth_client(self.user).post(
            f'{CATEGORIES_URL}{self.category.id}/add_promotion/', body, format='json')

    def test_add_promotion_end_before_start_rejected(self):
        now = timezone.now()
        r = self._promo((now + timedelta(days=2)).isoformat(), (now + timedelta(days=1)).isoformat())
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()['promotion_ends_at'], ['La fecha de fin debe ser posterior a la fecha de inicio.'])

    def test_add_promotion_equal_dates_rejected(self):
        now = timezone.now().isoformat()
        self.assertEqual(self._promo(now, now).status_code, 400)

    def test_add_promotion_valid_window_ok(self):
        now = timezone.now()
        r = self._promo(now.isoformat(), (now + timedelta(days=1)).isoformat())
        self.assertEqual(r.status_code, 200)

    def test_partial_update_checked_against_stored_start(self):
        now = timezone.now()
        self.category.discount_percentage = Decimal('10.00')
        self.category.promotion_starts_at = now + timedelta(days=2)
        self.category.save()
        r = self.auth_client(self.user).post(
            f'{CATEGORIES_URL}{self.category.id}/add_promotion/',
            {'promotion_ends_at': (now + timedelta(days=1)).isoformat()}, format='json')
        self.assertEqual(r.status_code, 400)

    def test_patch_category_end_before_start_rejected(self):
        now = timezone.now()
        r = self.auth_client(self.user).patch(
            f'{CATEGORIES_URL}{self.category.id}/',
            {'discount_percentage': '10.00',
             'promotion_starts_at': (now + timedelta(days=2)).isoformat(),
             'promotion_ends_at': (now + timedelta(days=1)).isoformat()}, format='json')
        self.assertEqual(r.status_code, 400)

    def test_legacy_naive_datetime_is_read_in_asuncion_time(self):
        # Cobertura de entrada legada/naive (el front actual envía ISO con offset):
        # un cliente antiguo envía "YYYY-MM-DDTHH:mm" sin offset y el servidor lo
        # interpreta en America/Asuncion (TIME_ZONE) y lo guarda como instante UTC.
        r = self._promo('2030-01-15T10:00', '2030-01-16T10:00')
        self.assertEqual(r.status_code, 200)
        self.category.refresh_from_db()
        self.assertTrue(timezone.is_aware(self.category.promotion_starts_at))
        self.assertEqual(
            self.category.promotion_starts_at,
            timezone.make_aware(timezone.datetime(2030, 1, 15, 10, 0)),
        )


class ProductWindowValidationTests(MarkyAPITestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.user, cls.profile = cls.make_user('prodwin', 'prodwin@test.com')

    def test_product_end_before_start_rejected(self):
        now = timezone.now()
        r = self.auth_client(self.user).post('/api/v1/products/products/', {
            'name': 'X', 'description': 'd', 'price': '10.00', 'discount_percentage': '10.00',
            'promotion_starts_at': (now + timedelta(days=2)).isoformat(),
            'promotion_ends_at': (now + timedelta(days=1)).isoformat(),
        }, format='multipart')
        self.assertEqual(r.status_code, 400)
        self.assertIn('promotion_ends_at', r.json())


class PromotionBoundaryTests(MarkyAPITestCase):
    """Regla única: inicio inclusivo, fin EXCLUSIVO (now >= ends => finalizada)."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.user, cls.profile = cls.make_user('bound', 'bound@test.com')
        cls.now = timezone.now().replace(microsecond=0)
        cls.category = ProductCategory.objects.create(
            business=cls.profile, name='Cat', icon='x', order=1,
            discount_percentage=Decimal('10.00'), promotion_ends_at=cls.now,
        )
        cls.product = Product.objects.create(
            name='P', description='d', price=Decimal('1.00'), business=cls.profile,
            discount_percentage=Decimal('10.00'), promotion_ends_at=cls.now,
        )
        cls.starting = Product.objects.create(
            name='S', description='d', price=Decimal('1.00'), business=cls.profile,
            discount_percentage=Decimal('10.00'), promotion_starts_at=cls.now,
        )

    def test_product_filter_end_exactly_now_is_expired(self):
        self.assertFalse(Product.objects.filter(pk=self.product.pk).filter(
            product_has_active_promotion_q(self.now)).exists())

    def test_product_filter_end_one_second_later_is_active(self):
        self.assertTrue(Product.objects.filter(pk=self.product.pk).filter(
            product_has_active_promotion_q(self.now - timedelta(seconds=1))).exists())

    def test_product_filter_start_exactly_now_is_active(self):
        self.assertTrue(Product.objects.filter(pk=self.starting.pk).filter(
            product_has_active_promotion_q(self.now)).exists())

    def test_product_filter_start_one_second_ahead_is_not_active(self):
        self.assertFalse(Product.objects.filter(pk=self.starting.pk).filter(
            product_has_active_promotion_q(self.now - timedelta(seconds=1))).exists())

    def test_category_has_promotion_end_boundary(self):
        from unittest import mock
        from products import filters as filters_module

        def ids(now):
            with mock.patch.object(filters_module.timezone, 'now', return_value=now):
                f = ProductCategoryFilter({'has_promotion': 'true'},
                                          queryset=ProductCategory.objects.filter(pk=self.category.pk))
                return list(f.qs.values_list('pk', flat=True))

        self.assertEqual(ids(self.now), [])
        self.assertEqual(ids(self.now - timedelta(seconds=1)), [self.category.pk])

    def test_status_matches_filter_rule_for_category_and_product(self):
        from products.promotions import compute_promotion_status, EXPIRED, ACTIVE
        for obj in (self.category, self.product):
            args = (obj.multibuy_option, obj.discount_percentage, obj.promotion_starts_at, obj.promotion_ends_at)
            self.assertEqual(compute_promotion_status(*args, now=self.now), EXPIRED)
            self.assertEqual(compute_promotion_status(*args, now=self.now - timedelta(seconds=1)), ACTIVE)


class ExpirePromotionsCommandTests(MarkyAPITestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.user, cls.profile = cls.make_user('expcmd', 'expcmd@test.com')
        cls.user2, cls.profile2 = cls.make_user('expcmd2', 'expcmd2@test.com')

    def test_command_clears_expired_for_all_businesses(self):
        past = timezone.now() - timedelta(days=1)
        p1 = Product.objects.create(name='A', description='d', price=Decimal('1'), business=self.profile,
                                    discount_percentage=Decimal('10'), promotion_ends_at=past)
        c2 = ProductCategory.objects.create(business=self.profile2, name='C', icon='x', order=1,
                                            multibuy_option='2x1', promotion_ends_at=past)
        out = StringIO()
        call_command('expire_promotions', stdout=out)
        p1.refresh_from_db()
        c2.refresh_from_db()
        self.assertIsNone(p1.promotion_ends_at)
        self.assertEqual(p1.discount_percentage, Decimal('0'))
        self.assertIsNone(c2.multibuy_option)
        self.assertEqual(Notification.objects.count(), 2)

    def test_one_failing_business_does_not_stop_the_others(self):
        past = timezone.now() - timedelta(days=1)
        c2 = ProductCategory.objects.create(business=self.profile2, name='C', icon='x', order=1,
                                            multibuy_option='2x1', promotion_ends_at=past)
        from products.services import handle_expired_promotions_for_business as real

        def flaky(business, now=None):
            if business.pk == self.profile.pk:
                raise RuntimeError('boom')
            return real(business, now=now)

        out = StringIO()
        with mock.patch('products.management.commands.expire_promotions.handle_expired_promotions_for_business',
                        side_effect=flaky):
            with self.assertLogs('products.management.commands.expire_promotions', level='ERROR'):
                with self.assertRaises(CommandError) as ctx:
                    call_command('expire_promotions', stdout=out)
        self.assertIn('1', str(ctx.exception))
        c2.refresh_from_db()
        self.assertIsNone(c2.multibuy_option)
