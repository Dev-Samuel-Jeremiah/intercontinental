"""
Set up the school this installation runs. Safe to run again: anything that
already exists is left as it is.

    python manage.py setup_school

Creates, if missing:
  - the school record (name, contact details) from settings / the options below
  - the first administrator account (must change password at first sign-in)
  - the current academic session and its 1st term
  - the three sections: Nursery & Primary, Secondary and Darun
  - the starter help-centre articles and tours

Options override the defaults, e.g.

    python manage.py setup_school --admin-username ict --admin-email ict@school.edu.ng \
        --email info@school.edu.ng --phone 0803... --address "..., Kano"
"""
import secrets
from datetime import date

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from tenants.middleware import clear_current_tenant, set_current_tenant
from tenants.models import Tenant

SECTIONS = [
    ('Nursery & Primary', 'NP'),
    ('Secondary', 'SEC'),
    ('Darun', 'DAR'),
]


class Command(BaseCommand):
    help = 'Create the school, its first administrator, the current session and its sections.'

    def add_arguments(self, parser):
        parser.add_argument('--name', default=getattr(settings, 'SCHOOL_NAME', 'Intercontinental School Kano'))
        parser.add_argument('--slug', default=getattr(settings, 'SCHOOL_SLUG', 'intercontinental'),
                            help='Short code the school is stored under (letters, digits, hyphens).')
        parser.add_argument('--email', default='', help="The school's main email address.")
        parser.add_argument('--phone', default=getattr(settings, 'SCHOOL_PHONE', ''))
        parser.add_argument('--address', default='')
        parser.add_argument('--state', default='Kano')
        parser.add_argument('--motto', default='')
        parser.add_argument('--admin-username', default='admin')
        parser.add_argument('--admin-email', default='')
        parser.add_argument('--admin-name', default='School Administrator')
        parser.add_argument('--admin-password', default='',
                            help='Leave blank to generate one. It must be changed at first sign-in.')

    @transaction.atomic
    def handle(self, *args, **o):
        school = Tenant.objects.filter(subdomain=o['slug']).first()
        if school is None and Tenant.objects.exists():
            other = Tenant.objects.order_by('created_at').first()
            raise CommandError(
                f'A school already exists under "{other.subdomain}". Set SCHOOL_SLUG={other.subdomain} in .env '
                f'(or pass --slug {other.subdomain}) instead of creating a second one.')
        if school is None:
            school = Tenant.objects.create(
                name=o['name'], subdomain=o['slug'], school_type='secondary', status='ACTIVE',
                email=o['email'] or o['admin_email'] or f'info@{o["slug"]}.local',
                phone=o['phone'], address=o['address'], state=o['state'], motto=o['motto'],
                country='Nigeria', owner_name=o['admin_name'], owner_email=o['admin_email'])
            self.stdout.write(self.style.SUCCESS(f'School created: {school.name}'))
        else:
            self.stdout.write(f'School already set up: {school.name}')

        set_current_tenant(school)
        try:
            self._admin(school, o)
            self._session(school)
            self._sections(school)
        finally:
            clear_current_tenant()
        call_command('seed_help', verbosity=0)
        self.stdout.write(self.style.SUCCESS('Done. Sign in at ' + school.portal_url + '/staff/login/'))

    def _admin(self, school, o):
        from users.models import Staff, User
        if User.objects.filter(tenant=school, is_superuser=True).exists():
            self.stdout.write('Administrator already exists.')
            return
        if User.objects.filter(tenant=school, username=o['admin_username']).exists():
            raise CommandError(f'The username "{o["admin_username"]}" is taken; pass --admin-username.')
        password = o['admin_password'] or secrets.token_urlsafe(9)
        first, _, last = o['admin_name'].strip().partition(' ')
        user = User.objects.create_user(
            username=o['admin_username'], email=o['admin_email'], password=password,
            first_name=first, last_name=last, tenant=school,
            # is_superuser = full access inside this school (the menus are gated on it).
            # It does not open Django's /admin/ site, which also needs is_staff.
            is_staff_user=True, is_superuser=True, must_change_password=True)
        perms = {f.name: True for f in Staff._meta.fields if f.name.startswith('can_')}
        Staff.objects.create(tenant=school, user=user, full_name=o['admin_name'], role='ADMIN', **perms)
        school.owner = user
        school.save(update_fields=['owner', 'updated_at'])
        self.stdout.write(self.style.SUCCESS(
            f'Administrator created — username: {user.username}  temporary password: {password}\n'
            f'  (they will be asked to choose a new password at first sign-in)'))

    def _session(self, school):
        from academics.models import AcademicSession, Term
        if AcademicSession.objects.filter(tenant=school).exists():
            return
        today = date.today()
        year = today.year if today.month >= 9 else today.year - 1
        session = AcademicSession.objects.create(
            tenant=school, name=f'{year}/{year + 1}', is_active=True,
            start_date=date(year, 9, 1), end_date=date(year + 1, 7, 31))
        Term.objects.create(tenant=school, session=session, name='1st Term', is_active=True,
                            start_date=date(year, 9, 1), end_date=date(year, 12, 15))
        self.stdout.write(self.style.SUCCESS(f'Session {session.name}, 1st Term created.'))

    def _sections(self, school):
        from saas_admin.models import Campus
        for name, code in SECTIONS:
            _, created = Campus.objects.get_or_create(tenant=school, name=name, defaults={'code': code})
            if created:
                self.stdout.write(self.style.SUCCESS(f'Section created: {name}'))
