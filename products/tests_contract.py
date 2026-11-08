"""F-33 (claves de respuesta), F-37 (desempate de orden) y F-39 (add_promotion)."""
from decimal import Decimal

from products.models import Product, ProductCategory
from utils.tests_base import MarkyAPITestCase

CATEGORIES_URL = '/api/v1/products/product-categories/'
PRODUCTS_URL = '/api/v1/products/products/'

CATEGORY_BASIC_KEYS = {
    'id', 'name', 'slug', 'icon', 'multibuy_option', 'discount_percentage',
    'promotion_starts_at', 'promotion_ends_at', 'promotion_status', 'is_available',
}
PRODUCT_LITE_KEYS = {
    'id', 'name', 'slug', 'description', 'price', 'isFavorite', 'isRecommended', 'image', 'image_thumbnail',
    'multibuy_option', 'discount_percentage', 'promotion_starts_at', 'promotion_ends_at',
    'promotion_status', 'is_available', 'primary_price', 'secondary_price',
    'primary_price_with_discount', 'secondary_price_with_discount',
}
PRODUCT_DETAIL_KEYS = {
    'id', 'name', 'slug', 'description', 'price', 'category', 'is_active',
    'category_multibuy_option', 'category_discount_percentage', 'category_promotion_starts_at',
    'category_promotion_ends_at', 'category_promotion_status',
    'stopper', 'multibuy_option', 'discount_percentage', 'is_available',
    'promotion_starts_at', 'promotion_ends_at', 'promotion_status', 'business',
    'variants', 'addons', 'media', 'model_3d',
    'featured_ingredients', 'presentation', 'allergens', 'celiac_info',
    'primary_price', 'secondary_price', 'primary_price_with_discount', 'secondary_price_with_discount',
}
CATALOG_GROUP_KEYS = {
    'id', 'name', 'slug', 'icon', 'multibuy_option', 'discount_percentage',
    'promotion_starts_at', 'promotion_ends_at', 'promotion_status', 'is_available', 'products',
}


class ResponseKeySetTests(MarkyAPITestCase):
    """Fija las claves exactas que consume el frontend (snake_case salvo isFavorite/isRecommended)."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.user, cls.profile = cls.make_user('contract', 'contract@test.com')
        cls.category = ProductCategory.objects.create(business=cls.profile, name='C', icon='x', order=1)
        cls.product = Product.objects.create(
            name='P', description='d', price=Decimal('10.00'), business=cls.profile, category=cls.category,
        )

    def setUp(self):
        super().setUp()
        self.client = self.auth_client(self.user)

    def test_category_list_item_keys(self):
        res = self.client.get(CATEGORIES_URL)
        self.assertEqual(set(res.data['results'][0]), CATEGORY_BASIC_KEYS)

    def test_with_products_keys(self):
        res = self.client.get(CATEGORIES_URL + 'with_products/')
        self.assertEqual({'products_count', 'results', 'count', 'next', 'previous'} - set(res.data), set())
        group = res.data['results'][0]
        self.assertEqual(set(group), CATALOG_GROUP_KEYS)
        self.assertEqual(set(group['products'][0]), PRODUCT_LITE_KEYS)

    def test_product_list_and_detail_keys(self):
        res = self.client.get(PRODUCTS_URL)
        self.assertEqual(set(res.data['results'][0]), PRODUCT_DETAIL_KEYS)
        res = self.client.get(f'{PRODUCTS_URL}{self.product.pk}/')
        self.assertEqual(set(res.data), PRODUCT_DETAIL_KEYS)

    def test_public_catalog_keys_match_admin(self):
        res = self.client.get(f'/api/v1/public/business/{self.profile.business_id}/catalog/')
        group = res.data['results'][0]
        self.assertEqual(set(group), CATALOG_GROUP_KEYS)
        self.assertEqual(set(group['products'][0]), PRODUCT_LITE_KEYS)


class AddPromotionResponseTests(MarkyAPITestCase):
    """F-39: la respuesta es la categoría completa (el modal lee id/name/icon)."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.user, cls.profile = cls.make_user('promoresp', 'promoresp@test.com')
        cls.category = ProductCategory.objects.create(business=cls.profile, name='Bebidas', icon='fa-x', order=1)

    def test_add_promotion_returns_basic_category(self):
        res = self.auth_client(self.user).post(
            f'{CATEGORIES_URL}{self.category.pk}/add_promotion/',
            {'discount_percentage': '10.00'}, format='json',
        )
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(set(res.data), CATEGORY_BASIC_KEYS)
        self.assertEqual(res.data['id'], self.category.pk)
        self.assertEqual(res.data['name'], 'Bebidas')
        self.assertEqual(Decimal(res.data['discount_percentage']), Decimal('10.00'))


class OrderingTieBreakerTests(MarkyAPITestCase):
    """F-37: empates de `order` se resuelven por id."""

    def test_category_default_ordering_has_id_tiebreaker(self):
        self.assertEqual(list(ProductCategory._meta.ordering), ['order', 'id'])

    def test_admin_product_queryset_is_ordered(self):
        from products.views import ProductViewSet
        user, _ = self.make_user('ordowner', 'ordowner@test.com')
        view = ProductViewSet()
        view.request = type('R', (), {'user': user})()
        self.assertTrue(view.get_queryset().ordered)
