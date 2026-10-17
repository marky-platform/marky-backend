from django.db import migrations, models

from products.migrations._slug_backfill import backfill_missing_slugs


def lock_and_backfill(apps, schema_editor):
    """Step 3 of 3: catch rows inserted (with a NULL slug) by still-running old
    code since 0019, then add the unique constraints below in the same
    transaction. On PostgreSQL the tables are write-locked first so no row can
    slip in between. The columns stay nullable here: old code keeps inserting
    NULL slugs until the new release is live (NULLs never collide in a unique
    constraint, and save() allocates a slug the next time such a row is saved).
    NOT NULL ships in a follow-up migration after this release."""
    ProductCategory = apps.get_model('products', 'ProductCategory')
    Product = apps.get_model('products', 'Product')
    if schema_editor.connection.vendor == 'postgresql':
        with schema_editor.connection.cursor() as cursor:
            cursor.execute(
                'LOCK TABLE {}, {} IN SHARE ROW EXCLUSIVE MODE'.format(
                    schema_editor.quote_name(ProductCategory._meta.db_table),
                    schema_editor.quote_name(Product._meta.db_table),
                )
            )
    backfill_missing_slugs(ProductCategory, Product)
    if schema_editor.connection.vendor == 'postgresql':
        with schema_editor.connection.cursor() as cursor:
            # Fire any deferred FK checks now; ALTER TABLE below refuses to run
            # while trigger events are pending in this transaction.
            cursor.execute('SET CONSTRAINTS ALL IMMEDIATE')


class Migration(migrations.Migration):

    dependencies = [
        ('products', '0019_backfill_slugs'),
    ]

    operations = [
        migrations.RunPython(lock_and_backfill, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name='productcategory',
            constraint=models.UniqueConstraint(fields=('business', 'slug'), name='unique_category_slug_per_business'),
        ),
        migrations.AddConstraint(
            model_name='product',
            constraint=models.UniqueConstraint(condition=models.Q(('category__isnull', False)), fields=('business', 'category', 'slug'), name='unique_product_slug_per_category'),
        ),
        migrations.AddConstraint(
            model_name='product',
            constraint=models.UniqueConstraint(condition=models.Q(('category__isnull', True)), fields=('business', 'slug'), name='unique_uncategorized_product_slug_per_business'),
        ),
    ]
