"""F-36: los campos de texto no se coercionan ("007", "true", "null") con claves anidadas."""
from products.models import Product, ProductAddon, ProductVariant
from products.tests_uploads import MediaRootMixin, URL, _png_bytes, _upload
from utils.tests_base import MarkyAPITestCase


class NestedTextCoercionTests(MediaRootMixin, MarkyAPITestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.user, cls.profile = cls.make_user('textowner', 'textowner@test.com')

    def setUp(self):
        super().setUp()
        self.client.force_authenticate(self.user)

    def _post(self, name, description='D'):
        return self.client.post(URL, {
            'name': name, 'description': description, 'price': '10.00',
            'addons[0][name]': name, 'addons[0][price]': '1.00',
            'variants[0][name]': name, 'variants[0][price]': '5.00',
            'variants[0][image]': _upload('v.png', _png_bytes()),
        }, format='multipart')

    def test_numeric_looking_text_keeps_leading_zeros(self):
        res = self._post('007', description='0042')
        self.assertEqual(res.status_code, 201, res.content)
        product = Product.objects.get()
        self.assertEqual((product.name, product.description), ('007', '0042'))
        self.assertEqual(ProductAddon.objects.get().name, '007')
        self.assertEqual(ProductVariant.objects.get().name, '007')

    def test_true_and_null_are_valid_text(self):
        for text in ('true', 'null'):
            res = self._post(text, description=text)
            self.assertEqual(res.status_code, 201, res.content)
            product = Product.objects.latest('id')
            self.assertEqual((product.name, product.description), (text, text))


class FreeTextCsvFieldsTests(MediaRootMixin, MarkyAPITestCase):
    """Guard: `allergens` / `featured_ingredients` llegan sin coerción con claves anidadas."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.user, cls.profile = cls.make_user('csvowner', 'csvowner@test.com')

    def setUp(self):
        super().setUp()
        self.client.force_authenticate(self.user)

    def _post(self, **extra):
        data = {
            'name': 'P', 'description': 'D', 'price': '10.00',
            'variants[0][name]': 'V', 'variants[0][price]': '5.00',
            'variants[0][image]': _upload('v.png', _png_bytes()),
        }
        data.update(extra)
        return self.client.post(URL, data, format='multipart')

    def test_featured_ingredients_keep_text_as_sent(self):
        for text in ('007', 'true', 'null'):
            res = self._post(featured_ingredients=text)
            self.assertEqual(res.status_code, 201, res.content)
            self.assertEqual(Product.objects.latest('id').featured_ingredients, text)

    def test_allergens_are_not_coerced(self):
        # "007"/"true" no son alérgenos válidos: deben rechazarse como texto (400), no coercionarse
        # (7 / True -> 500 o aceptación silenciosa); "null" tampoco debe convertirse en None.
        for text in ('007', 'true', 'null'):
            res = self._post(allergens=text)
            self.assertEqual(res.status_code, 400, (text, res.content))
            self.assertIn('allergens', res.data)
        self.assertEqual(Product.objects.count(), 0)
