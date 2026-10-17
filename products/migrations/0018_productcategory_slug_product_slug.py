from django.db import migrations, models


class Migration(migrations.Migration):
    """Step 1 of 3 for canonical public URLs: nullable, non-unique slug columns."""

    dependencies = [
        ('products', '0017_product_order'),
    ]

    operations = [
        migrations.AddField(
            model_name='productcategory',
            name='slug',
            field=models.SlugField(blank=True, db_index=False, editable=False, max_length=255, null=True),
        ),
        migrations.AddField(
            model_name='product',
            name='slug',
            field=models.SlugField(blank=True, db_index=False, editable=False, max_length=255, null=True),
        ),
    ]
