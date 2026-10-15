import security.crypto
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("hostel", "0006_boarding_life"),
    ]

    operations = [
        migrations.AlterField(
            model_name="boarderprofile",
            name="medical_conditions",
            field=security.crypto.EncryptedTextField(blank=True, verbose_name='Medical Conditions / Allergies'),
        ),
    ]
