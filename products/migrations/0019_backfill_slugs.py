from django.db import migrations

from products.migrations._slug_backfill import backfill_missing_slugs


def backfill_slugs(apps, schema_editor):
    """Step 2 of 3: fill every empty slug in deterministic (business, category, id) order."""
    backfill_missing_slugs(
        apps.get_model('products', 'ProductCategory'),
        apps.get_model('products', 'Product'),
    )


class Migration(migrations.Migration):

    dependencies = [
        ('products', '0018_productcategory_slug_product_slug'),
    ]

    operations = [
        # Reverse is a no-op: populated slugs are harmless on a rolled-back schema
        # (0018's reverse drops the columns anyway).
        migrations.RunPython(backfill_slugs, migrations.RunPython.noop),
    ]
