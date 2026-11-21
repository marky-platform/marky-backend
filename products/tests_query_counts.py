"""F-22: el número de consultas del catálogo y del listado admin no crece con N."""
from decimal import Decimal

from django.db import connection
from django.test.utils import CaptureQueriesContext

from products.models import Product, ProductAddon, ProductCategory, ProductMedia, ProductVariant
from utils.tests_base import MarkyAPITestCase

WITH_PRODUCTS_URL = '/api/v1/products/product-categories/with_products/'
PRODUCTS_URL = '/api/v1/products/products/'


class QueryCountTests(MarkyAPITestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.user, cls.profile = cls.make_user('qcount', 'qcount@test.com')
        cls.category = ProductCategory.objects.create(business=cls.profile, name='Cat', icon='x', order=1)

    def _seed(self, n, start=0):
        """n productos (categorizados y sueltos) con 2 imágenes, 1 video, variantes y adicionales."""
        products = [
            Product(name=f'P{start + i}', description='d', price=Decimal('10.00'), business=self.profile,
                    category=self.category if i % 2 == 0 else None, order=start + i + 1, is_active=True)
            for i in range(n)
        ]
        Product.objects.bulk_create(products)
        created = list(Product.objects.filter(name__in=[p.name for p in products]))
        ProductMedia.objects.bulk_create([
            ProductMedia(product=p, file=f'product_media/{p.pk}-{k}.png', media_type=t, order=k)
            for p in created for k, t in enumerate([ProductMedia.IMAGE, ProductMedia.IMAGE, ProductMedia.VIDEO])
        ])
        ProductVariant.objects.bulk_create([
            ProductVariant(product=p, name='V', price=Decimal('5.00'), image=f'product_variants/{p.pk}.png')
            for p in created
        ])
        ProductAddon.objects.bulk_create([
            ProductAddon(product=p, name='A', price=Decimal('1.00')) for p in created
        ])

    def _count(self, client, url):
        with CaptureQueriesContext(connection) as ctx:
            res = client.get(url)
        self.assertEqual(res.status_code, 200, res.content)
        return len(ctx), res

    def _assert_flat(self, url, results_of, client_factory):
        self._seed(3)
        small, res = self._count(client_factory(), url)
        self.assertEqual(results_of(res), 3)
        self._seed(12, start=3)
        large, res = self._count(client_factory(), url)
        self.assertEqual(results_of(res), 15)
        self.assertEqual(large, small, f'consultas crecen con N: {small} -> {large}')

    @staticmethod
    def _catalog_products(res):
        return sum(len(group['products']) for group in res.data['results'])

    def test_admin_with_products_is_flat(self):
        self._assert_flat(WITH_PRODUCTS_URL, self._catalog_products, lambda: self.auth_client(self.user))

    def test_public_catalog_is_flat(self):
        from rest_framework.test import APIClient
        self._assert_flat(
            f'/api/v1/public/business/{self.profile.business_id}/catalog/',
            self._catalog_products, APIClient,
        )

    def test_admin_product_list_is_flat(self):
        self._assert_flat(PRODUCTS_URL, lambda res: len(res.data['results']), lambda: self.auth_client(self.user))

    def test_catalog_image_is_first_image_not_video(self):
        self._seed(1)
        res = self.auth_client(self.user).get(WITH_PRODUCTS_URL)
        item = next(g['products'][0] for g in res.data['results'] if g['products'])
        self.assertTrue(item['image'].endswith('-0.png'))
