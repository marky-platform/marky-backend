"""Hallazgos de revisión: lock al cambiar de categoría y consultas de ProductLiteSerializer."""
from decimal import Decimal

from django.db import connection
from django.test.utils import CaptureQueriesContext

from products.models import Product, ProductCategory, ProductMedia
from products.serializers import ProductLiteSerializer
from products.tests_uploads import MediaRootMixin, _png_bytes, _upload
from utils.tests_base import MarkyAPITestCase


class ProductUpdateCategoryOrderTests(MarkyAPITestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.user, cls.profile = cls.make_user('moveowner', 'moveowner@test.com')
        cls.cat_a = ProductCategory.objects.create(business=cls.profile, name='A', icon='i', order=1)
        cls.cat_b = ProductCategory.objects.create(business=cls.profile, name='B', icon='i', order=2)

    def setUp(self):
        super().setUp()
        self.client.force_authenticate(self.user)

    def _product(self, name, category, order):
        return Product.objects.create(
            name=name, description='D', price=Decimal('10.00'), business=self.profile,
            category=category, order=order,
        )

    def _patch(self, product, data):
        return self.client.patch(f'/api/v1/products/products/{product.id}/', data, format='multipart')

    def test_category_change_assigns_max_plus_one_under_lock(self):
        moved = self._product('moved', self.cat_a, 1)
        self._product('x', self.cat_b, 4)
        with CaptureQueriesContext(connection) as ctx:
            res = self._patch(moved, {'category': self.cat_b.id})
        self.assertEqual(res.status_code, 200, res.content)
        moved.refresh_from_db()
        self.assertEqual((moved.category_id, moved.order), (self.cat_b.id, 5))
        if connection.features.has_select_for_update:
            self.assertTrue(any('FOR UPDATE' in q['sql'] for q in ctx.captured_queries))

    def test_same_category_keeps_order_and_takes_no_lock(self):
        product = self._product('p', self.cat_a, 3)
        with CaptureQueriesContext(connection) as ctx:
            res = self._patch(product, {'category': self.cat_a.id, 'name': 'nuevo'})
        self.assertEqual(res.status_code, 200, res.content)
        product.refresh_from_db()
        self.assertEqual((product.name, product.order), ('nuevo', 3))
        self.assertFalse(any('FOR UPDATE' in q['sql'] for q in ctx.captured_queries))


class ProductLiteFirstImageTests(MediaRootMixin, MarkyAPITestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.user, cls.profile = cls.make_user('liteowner', 'liteowner@test.com')

    def _product_with_media(self, name):
        product = Product.objects.create(name=name, description='D', price=Decimal('1.00'), business=self.profile)
        ProductMedia.objects.create(product=product, file=_upload('v.mp4', b'\x00\x00\x00\x18ftypmp42' + b'0' * 40),
                                    media_type=ProductMedia.VIDEO, order=0)
        second = ProductMedia.objects.create(product=product, file=_upload('b.png', _png_bytes()),
                                             media_type=ProductMedia.IMAGE, order=2)
        first = ProductMedia.objects.create(product=product, file=_upload('a.png', _png_bytes()),
                                            media_type=ProductMedia.IMAGE, order=1)
        return product, first, second

    def test_unprefetched_returns_first_image_with_limit_one_query(self):
        product, first, _ = self._product_with_media('p')
        product = Product.objects.get(pk=product.pk)  # sin prefetch
        with CaptureQueriesContext(connection) as ctx:
            data = ProductLiteSerializer(product, context={}).data
        media_sql = [q['sql'] for q in ctx.captured_queries if 'productmedia' in q['sql']]
        self.assertEqual(len(media_sql), 1, media_sql)
        self.assertIn('LIMIT 1', media_sql[0])
        self.assertTrue(data['image'].endswith(first.file.name))

    def test_prefetched_uses_cache_without_extra_queries(self):
        for n in range(3):
            self._product_with_media(f'p{n}')
        products = list(Product.objects.prefetch_related('media'))
        with CaptureQueriesContext(connection) as ctx:
            data = ProductLiteSerializer(products, many=True, context={}).data
        self.assertEqual([q for q in ctx.captured_queries if 'productmedia' in q['sql']], [])
        self.assertTrue(all(d['image'] for d in data))
