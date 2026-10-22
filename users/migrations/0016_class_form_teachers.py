"""A class can have several form teachers: the single form_teacher becomes form_teachers,
keeping every class's current form teacher."""
from django.db import migrations, models
import django.db.models.deletion


def copy_forward(apps, schema_editor):
    Class = apps.get_model('users', 'Class')
    for cls in Class.objects.exclude(form_teacher__isnull=True).only('pk', 'form_teacher_id'):
        cls.form_teachers.add(cls.form_teacher_id)


def copy_back(apps, schema_editor):
    Class = apps.get_model('users', 'Class')
    for cls in Class.objects.all():
        first = cls.form_teachers.order_by('pk').first()
        if first:
            cls.form_teacher_id = first.pk
            cls.save(update_fields=['form_teacher'])


class Migration(migrations.Migration):

    dependencies = [
        ('users', '0015_alter_class_arm'),
    ]

    operations = [
        migrations.AlterField(
            model_name='class',
            name='form_teacher',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                                    related_name='+', to='users.staff'),
        ),
        migrations.AddField(
            model_name='class',
            name='form_teachers',
            field=models.ManyToManyField(blank=True, help_text='A class can have more than one form teacher.',
                                         related_name='form_classes', to='users.staff'),
        ),
        migrations.RunPython(copy_forward, copy_back),
        migrations.RemoveField(
            model_name='class',
            name='form_teacher',
        ),
    ]
