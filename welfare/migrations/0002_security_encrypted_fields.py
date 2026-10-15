import security.crypto
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("welfare", "0001_initial"),
    ]

    operations = [
        migrations.AlterField(
            model_name="medicalprofile",
            name="conditions",
            field=security.crypto.EncryptedTextField(blank=True, help_text='Asthma, epilepsy, diabetes, sickle cell…'),
        ),
        migrations.AlterField(
            model_name="medicalprofile",
            name="notes",
            field=security.crypto.EncryptedTextField(blank=True, help_text='Nurses only'),
        ),
        migrations.AlterField(
            model_name="counsellingnote",
            name="text",
            field=security.crypto.EncryptedTextField(),
        ),
        migrations.AlterField(
            model_name="concern",
            name="description",
            field=security.crypto.EncryptedTextField(help_text="What you saw or heard — the child's own words where you can"),
        ),
        migrations.AlterField(
            model_name="concernaction",
            name="text",
            field=security.crypto.EncryptedTextField(),
        ),
    ]
