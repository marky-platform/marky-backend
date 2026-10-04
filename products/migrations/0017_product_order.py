from django.db import migrations, models


def backfill_product_order(apps, schema_editor):
    """Number products 1..n by id within each (business, category) so the
    effective order stays what it was before the field existed. Uncategorized
    products (category NULL) are grouped per business."""
    Product = apps.get_model('products', 'Product')
    counters = {}
    to_update = []
    for product in Product.objects.order_by('id').only('id', 'business_id', 'category_id'):
        key = (product.business_id, product.category_id)
        counters[key] = counters.get(key, 0) + 1
        product.order = counters[key]
        to_update.append(product)
    Product.objects.bulk_update(to_update, ['order'], batch_size=500)


class Migration(migrations.Migration):

    dependencies = [
        ('products', '0016_product_featured_presentation_allergens_celiac'),
    ]

    operations = [
        migrations.AddField(
            model_name='product',
            name='order',
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.RunPython(backfill_product_order, migrations.RunPython.noop),
    ]
