import security.media
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("finance", "0011_alter_feestructure_category_and_more"),
    ]

    operations = [
        migrations.AlterField(
            model_name="payment",
            name="proof",
            field=models.ImageField(blank=True, null=True, storage=security.media.signed_storage, upload_to='payment_proofs/'),
        ),
        migrations.AlterField(
            model_name="topuprequest",
            name="payment_proof",
            field=models.ImageField(blank=True, null=True, storage=security.media.signed_storage, upload_to='topup_proofs/'),
        ),
        migrations.AlterField(
            model_name="expense",
            name="receipt",
            field=models.FileField(blank=True, null=True, storage=security.media.signed_storage, upload_to='expense_receipts/'),
        ),
        migrations.AlterField(
            model_name="whtremittance",
            name="receipt",
            field=models.FileField(blank=True, null=True, storage=security.media.signed_storage, upload_to='wht_receipts/'),
        ),
    ]
