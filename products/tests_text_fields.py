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
