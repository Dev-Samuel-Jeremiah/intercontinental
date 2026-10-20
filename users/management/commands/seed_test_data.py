"""
Fill a school with realistic test data: 10 subjects, 10 teachers and 10
students (with their parents), plus everything the portals need to show real
numbers: classes, form teachers, houses, departments, the timetable,
attendance since the term began, last session's published results, this
term's CA scores, fees and payments, homework, events and announcements.

    python manage.py seed_test_data                 # the only school
    python manage.py seed_test_data --school kano   # by subdomain

Run it once; a second run finds the seeded teachers and stops. Nothing is
emailed or texted while it runs.
"""
import random
from contextlib import ExitStack
from datetime import date, time, timedelta
from decimal import Decimal
from unittest import mock

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.test.utils import override_settings
from django.utils import timezone

from academics.models import AcademicSession, Term
from curriculum.models import CalendarEvent, Department, Homework
from tenants.middleware import set_current_tenant
from tenants.models import Tenant
from users.models import (Attendance, Class, ClassSubject, Guardian, House, Staff, StaffSubjectClass,
                          Student, StudentSubject, Subject, User)

TEACHER_PASSWORD = 'Teacher@2026'
STUDENT_PASSWORD = 'Student@2026'
PARENT_PASSWORD = 'Parent@2026'

# name, code, department, description
SUBJECTS = [
    ('Mathematics', 'MTH', 'Sciences', 'Number, shape, measurement and problem solving.'),
    ('English', 'ENG', 'Languages', 'Reading, writing, grammar, spelling and speaking.'),
    ('Basic Science', 'BSC', 'Sciences', 'Living things, materials, energy and the environment.'),
    ('Social Studies', 'SST', 'Humanities', 'Family, community, culture and Nigeria.'),
    ('Computer', 'ICT', 'Sciences', 'Using computers, typing, safety online and simple coding.'),
    ('Civic Education', 'CVE', 'Humanities', 'Values, rights, duties and good citizenship.'),
    ('Agricultural Science', 'AGR', 'Vocational', 'Crops, animals, soil and the school garden.'),
    ('Hausa', 'HAU', 'Languages', 'Hausa reading, writing and oral language.'),
    ('Cultural & Creative Arts', 'CCA', 'Vocational', 'Drawing, music, drama and craft.'),
    ('Physical & Health Education', 'PHE', 'Vocational', 'Games, athletics, fitness and hygiene.'),
]

# username, full name, gender, job title (each teaches SUBJECTS[i])
TEACHERS = [
    ('amina.bello', 'Amina Bello', 'F', 'Senior Mathematics Teacher'),
    ('chinedu.okafor', 'Chinedu Okafor', 'M', 'English Teacher'),
    ('tunde.adeyemi', 'Tunde Adeyemi', 'M', 'Basic Science Teacher'),
    ('ngozi.eze', 'Ngozi Eze', 'F', 'Social Studies Teacher'),
    ('ibrahim.musa', 'Ibrahim Musa', 'M', 'ICT Teacher'),
    ('funke.balogun', 'Funke Balogun', 'F', 'Civic Education Teacher'),
    ('emeka.okonkwo', 'Emeka Okonkwo', 'M', 'Agricultural Science Teacher'),
    ('hauwa.abdullahi', 'Hauwa Abdullahi', 'F', 'Hausa Teacher'),
    ('grace.johnson', 'Grace Johnson', 'F', 'Creative Arts Teacher'),
    ('yusuf.danjuma', 'Yusuf Danjuma', 'M', 'Games Master'),
]

# admission no, full name, gender, class, family (siblings share one), parent name
STUDENTS = [
    ('ISK26001', 'Aisha Abubakar', 'Female', 'Grade 1', 'abubakar', 'Alhaji Sani Abubakar'),
    ('ISK26002', 'David Okoro', 'Male', 'Grade 1', 'okoro', 'Mr Peter Okoro'),
    ('ISK26003', 'Fatima Lawal', 'Female', 'Grade 1', 'lawal', 'Mrs Zainab Lawal'),
    ('ISK26004', 'Samuel Adebayo', 'Male', 'Grade 1A', 'adebayo', 'Mr Kunle Adebayo'),
    ('ISK26005', 'Maryam Abubakar', 'Female', 'Grade 1A', 'abubakar', 'Alhaji Sani Abubakar'),
    ('ISK26006', 'Chiamaka Nwosu', 'Female', 'Grade 2', 'nwosu', 'Dr Ifeanyi Nwosu'),
    ('ISK26007', 'Umar Garba', 'Male', 'Grade 2', 'garba', 'Mallam Bashir Garba'),
    ('ISK26008', 'Esther Ojo', 'Female', 'Grade 2', 'ojo', 'Mrs Ruth Ojo'),
    ('ISK26009', 'Ahmed Yakubu', 'Male', 'Grade 3', 'yakubu', 'Engr Musa Yakubu'),
    ('ISK26010', 'Blessing Udo', 'Female', 'Grade 3', 'udo', 'Mrs Comfort Udo'),
]

CLASS_AGE = {'Grade 1': 6, 'Grade 1A': 6, 'Grade 2': 7, 'Grade 3': 8}
STREETS = ['Zaria Road', 'Murtala Mohammed Way', 'Ahmadu Bello Way', 'Club Road', 'Bompai Road',
           'Gidan Murtala', 'Race Course Road', 'Audu Bako Way', 'Maiduguri Road', 'Hotoro GRA']
HOUSES = [('Red House', '#dc2626', 'Courage and honour'), ('Blue House', '#2563eb', 'Wisdom and truth'),
          ('Green House', '#16a34a', 'Growth through effort'), ('Yellow House', '#ca8a04', 'Shine with kindness')]
GRADES = [('A', 70, 'Excellent'), ('B', 60, 'Very good'), ('C', 50, 'Good'), ('D', 45, 'Fair'),
          ('E', 40, 'Pass'), ('F', 0, 'Fail')]
PERIODS = [(time(8, 0), time(8, 40)), (time(8, 40), time(9, 20)), (time(9, 20), time(10, 0)),
           (time(10, 0), time(10, 40)), (time(11, 0), time(11, 40)), (time(11, 40), time(12, 20)),
           (time(12, 20), time(13, 0)), (time(13, 0), time(13, 40))]
DAYS = ['MON', 'TUE', 'WED', 'THU', 'FRI']


class Command(BaseCommand):
    help = 'Fill a school with 10 subjects, 10 teachers and 10 students (with parents) and their records.'

    def add_arguments(self, parser):
        parser.add_argument('--school', help='Subdomain of the school (defaults to the only school).')

    def handle(self, *args, **opts):
        self.t = self._school(opts.get('school'))
        set_current_tenant(self.t)
        if User.objects.filter(tenant=self.t, username=TEACHERS[0][0]).exists():
            self.stdout.write(self.style.WARNING('Test data is already in this school; nothing to do.'))
            return
        self.term = Term.objects.filter(tenant=self.t, is_active=True).select_related('session').first()
        if not self.term or not self.term.start_date:
            raise CommandError('Set an active term with a start date first (Academics → Sessions & terms).')
        self.session = self.term.session
        self.admin = User.objects.filter(tenant=self.t, is_superuser=True).order_by('id').first()
        self.rng = random.Random(2026)
        self.today = timezone.localdate()

        # Nothing leaves the machine: email goes to memory, SMS and WhatsApp are skipped.
        with ExitStack() as stack:
            stack.enter_context(override_settings(EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend',
                                                  SMS_ENABLED=False))
            stack.enter_context(mock.patch('communications.services.send_sms', return_value=(False, 'seed')))
            stack.enter_context(mock.patch('communications.connect.try_whatsapp', return_value=(False, '')))
            with transaction.atomic():
                self._grading()
                self._subjects()
                self._classes()
                self._teachers()
                self._houses()
                self._students()
                self._assignments()
                self._timetable()
                self._attendance()
                self._results()
                self._fees()
                self._homework_and_events()
        self._report()

    # ── setup ────────────────────────────────────────────────────────────
    def _school(self, subdomain):
        if subdomain:
            school = Tenant.objects.filter(subdomain=subdomain).first()
            if not school:
                raise CommandError(f'No school with subdomain "{subdomain}".')
            return school
        schools = list(Tenant.objects.all()[:2])
        if len(schools) != 1:
            raise CommandError('Several schools exist; choose one with --school <subdomain>.')
        return schools[0]

    def _grading(self):
        from results.models import Grading
        if not Grading.objects.filter(tenant=self.t).exists():
            for grade, low, text in GRADES:
                Grading.objects.create(tenant=self.t, grade=grade, min_score=low, description=text)

    def _subjects(self):
        self.subjects, departments = [], {}
        for name, code, dept, text in SUBJECTS:
            subject, _ = Subject.objects.get_or_create(tenant=self.t, name=name)
            subject.code, subject.description = code, text
            subject.save()
            self.subjects.append(subject)
            departments.setdefault(dept, []).append(subject)
        self.departments = {}
        for name, subjects in departments.items():
            dept, _ = Department.objects.get_or_create(tenant=self.t, name=name,
                                                       defaults={'description': f'{name} department'})
            dept.subjects.add(*subjects)
            self.departments[name] = dept

    def _classes(self):
        self.classes = {}
        for name in CLASS_AGE:
            cls, _ = Class.objects.get_or_create(tenant=self.t, name=name)
            if not cls.level:
                cls.level, cls.arm = name.rstrip('A'), 'A' if name.endswith('A') else ''
                cls.save()
            self.classes[name] = cls

    # ── people ───────────────────────────────────────────────────────────
    def _teachers(self):
        from hr.models import StaffRecord
        from payroll.models import SalaryGrade, StaffPayroll
        grade, _ = SalaryGrade.objects.get_or_create(tenant=self.t, name='Teacher II',
                                                     defaults={'base_salary': Decimal('180000'),
                                                               'description': 'Graduate teacher'})
        self.teachers = []
        for i, (username, name, sex, title) in enumerate(TEACHERS):
            first, last = name.split()
            user = User.objects.create_user(username=username, password=TEACHER_PASSWORD, tenant=self.t,
                                            first_name=first, last_name=last, email=f'{username}@example.com',
                                            is_staff_user=True, can_create_course=True, can_create_exam=True)
            staff = Staff.objects.create(tenant=self.t, user=user, full_name=name, role='TEACHER',
                                         can_enter_scores=True, can_mark_attendance=True, can_manage_exams=True,
                                         can_send_messages=True)
            dept = self.departments[SUBJECTS[i][2]]
            StaffRecord.objects.update_or_create(staff=staff, defaults=dict(
                tenant=self.t, job_title=title, department=dept, phone=f'0803{5550100 + i:07d}',
                extension=f'{200 + i}', date_joined=date(2019 + i % 6, 9, 2),
                date_of_birth=date(1980 + i * 2 % 15, 1 + i, 5 + i), address=f'{12 + i} {STREETS[i]}, Kano',
                emergency_name=f'{"Mr" if sex == "F" else "Mrs"} {last}',
                emergency_phone=f'0806{5550200 + i:07d}'))
            StaffPayroll.objects.update_or_create(staff=staff, defaults=dict(
                tenant=self.t, grade=grade, basic_salary=Decimal(160000 + i * 5000), housing=Decimal('40000'),
                transport=Decimal('20000'), bank_name=['Access Bank', 'GTBank', 'Zenith Bank'][i % 3],
                account_no=f'01{23456780 + i:08d}', account_name=name))
            if not dept.hod:
                dept.hod = staff
                dept.save(update_fields=['hod'])
            self.teachers.append(staff)
        for staff, cls in zip(self.teachers, self.classes.values()):   # first four are form teachers
            cls.form_teacher = staff
            cls.save(update_fields=['form_teacher'])

    def _houses(self):
        self.houses = []
        for i, (name, colour, motto) in enumerate(HOUSES):
            house, _ = House.objects.get_or_create(tenant=self.t, name=name, defaults={
                'colour': colour, 'motto': motto, 'house_master': self.teachers[6 + i % 4]})
            self.houses.append(house)

    def _students(self):
        from welfare.models import Immunisation, MedicalProfile
        rng = self.rng
        self.students, family_email = [], {}
        for i, (adm, name, sex, cls_name, family, parent) in enumerate(STUDENTS):
            email = family_email.setdefault(family, f'{family}.family@example.com')
            phone = f'0802{5550300 + len(family_email):07d}'
            age = CLASS_AGE[cls_name]
            user = User.objects.create_user(username=adm, password=STUDENT_PASSWORD, tenant=self.t, is_student=True,
                                            first_name=name.split()[0], last_name=name.split()[1])
            user.term_password = STUDENT_PASSWORD
            user.save(update_fields=['term_password'])
            student = Student.objects.create(
                tenant=self.t, user=user, full_name=name, admission_number=adm, gender=sex,
                date_of_birth=date(self.today.year - age, 1 + i, 3 + 2 * i), class_assigned=self.classes[cls_name],
                current_session=self.session, current_term=self.term.name, parent_name=parent, parent_phone=phone,
                parent_email=email, parent_term_password=PARENT_PASSWORD,
                address=f'{5 + i * 3} {STREETS[i]}, Kano', nin=f'{70000000000 + i * 1234567:011d}',
                house=self.houses[i % 4])
            parent_user, _ = User.objects.get_or_create(username=f'parent_{student.id}', defaults=dict(
                tenant=self.t, email=email, is_parent=True, first_name=parent))
            parent_user.set_password(PARENT_PASSWORD)
            parent_user.save()
            title, surname = parent.split()[0], parent.split()[-1]
            mother = title.startswith('Mrs')
            Guardian.objects.create(tenant=self.t, student=student, full_name=parent,
                                    relationship='MOTHER' if mother else 'FATHER', phone=phone, email=email,
                                    address=student.address, occupation=['Trader', 'Civil servant', 'Engineer',
                                                                         'Doctor', 'Banker'][i % 5],
                                    gets_fee_messages=True, can_pick_up=True, emergency_contact=True)
            Guardian.objects.create(tenant=self.t, student=student,
                                    full_name=f'{"Mr" if mother else "Mrs"} {surname}',
                                    relationship='FATHER' if mother else 'MOTHER', phone=f'0805{5550400 + i:07d}',
                                    address=student.address, occupation='Business owner', can_pick_up=True)
            MedicalProfile.objects.update_or_create(student=student, defaults=dict(
                tenant=self.t, blood_group=rng.choice(['O+', 'O+', 'A+', 'B+', 'AB+']),
                genotype=rng.choice(['AA', 'AA', 'AA', 'AS']),
                allergies='Peanuts' if i == 2 else ('Penicillin' if i == 7 else ''),
                conditions='Mild asthma' if i == 5 else '',
                emergency_medication='Inhaler in school bag' if i == 5 else '',
                doctor_name='Dr Halima Sule', doctor_phone='08035550999', hospital='Aminu Kano Teaching Hospital',
                consent_first_aid=True, consent_hospital=True))
            for vaccine, years in (('BCG', age), ('MEASLES', age - 1)):
                Immunisation.objects.create(tenant=self.t, student=student, vaccine=vaccine, dose='Dose 1',
                                            given_on=date(self.today.year - years, 3, 1), verified=True)
            StudentSubject.objects.bulk_create([StudentSubject(tenant=self.t, student=student, subject=s)
                                                for s in self.subjects], ignore_conflicts=True)
            student._ability = rng.gauss(62, 12)
            student._attend = rng.uniform(84, 99)
            self.students.append(student)

    # ── teaching ─────────────────────────────────────────────────────────
    def _assignments(self):
        rows, links = [], []
        for cls in self.classes.values():
            for subject, staff in zip(self.subjects, self.teachers):
                rows.append(ClassSubject(tenant=self.t, school_class=cls, subject=subject))
                links.append(StaffSubjectClass(tenant=self.t, staff=staff, subject=subject, school_class=cls))
        ClassSubject.objects.bulk_create(rows, ignore_conflicts=True)
        StaffSubjectClass.objects.bulk_create(links, ignore_conflicts=True)

    def _timetable(self):
        from timetable.models import (GeneratedTimetable, TeacherSubjectClass, TimeSlot, TimetableConfiguration,
                                      TimetableSlot)
        slots = []
        for n, (start, end) in enumerate(PERIODS, start=1):
            slot, _ = TimeSlot.objects.get_or_create(tenant=self.t, name=f'Period {n}', defaults={
                'start_time': start, 'end_time': end, 'order': n if n <= 4 else n + 1})
            slots.append(slot)
        brk, _ = TimeSlot.objects.get_or_create(tenant=self.t, name='Break', defaults={
            'start_time': time(10, 40), 'end_time': time(11, 0), 'order': 5, 'is_break': True})
        config = TimetableConfiguration(tenant=self.t, name=f'{self.session.name} {self.term.name}',
                                        active_days=DAYS, is_active=True)
        config.save()
        config.time_slots.add(*slots, brk)
        for cls in self.classes.values():
            for subject, staff in zip(self.subjects, self.teachers):
                TeacherSubjectClass.objects.create(tenant=self.t, config=config, staff=staff, subject=subject,
                                                   school_class=cls, periods_per_week=4)
        timetable = GeneratedTimetable.objects.create(tenant=self.t, config=config, is_published=True,
                                                      notes='Seeded test timetable')
        # At any period the four classes take four different subjects, so no teacher is double-booked,
        # and over the week every class meets every subject four times.
        cells = []
        for d, day in enumerate(DAYS):
            for p, slot in enumerate(slots):
                for c, cls in enumerate(self.classes.values()):
                    k = (d * len(slots) + p + c * 3) % len(self.subjects)
                    cells.append(TimetableSlot(tenant=self.t, timetable=timetable, school_class=cls, day=day,
                                               time_slot=slot, subject=self.subjects[k], teacher=self.teachers[k]))
        TimetableSlot.objects.bulk_create(cells)

    def _school_days(self, start, end):
        day = start
        while day <= end:
            if day.weekday() < 5:
                yield day
            day += timedelta(days=1)

    def _attendance(self):
        rng, rows = self.rng, []
        reasons = ['SICK', 'MEDICAL', 'FAMILY', 'TRAVEL']
        end = min(self.today, self.term.end_date or self.today)
        marked_today = {'Grade 1', 'Grade 2'}            # the other registers are still to be taken today
        for day in self._school_days(self.term.start_date, end):
            for st in self.students:
                if day == self.today and st.class_assigned.name not in marked_today:
                    continue
                present = rng.random() * 100 < st._attend
                late = present and rng.random() < 0.07
                rows.append(Attendance(
                    tenant=self.t, student=st, school_class=st.class_assigned, date=day,
                    status='P' if present else 'A', minutes_late=rng.choice([5, 10, 15]) if late else 0,
                    arrived_at=time(8, rng.choice([5, 10, 15])) if late else None,
                    absence_reason='' if present else rng.choice(reasons),
                    reason_from='' if present else 'PARENT', excused='' if present else 'EXCUSED'))
        Attendance.objects.bulk_create(rows, batch_size=1000)
        self.attendance_count = len(rows)

    def _results(self):
        from results.models import TermResult
        rng = self.rng
        start_year = int(self.session.name[:4]) - 1 if self.session.name[:4].isdigit() else self.today.year - 1
        last, _ = AcademicSession.objects.get_or_create(tenant=self.t, name=f'{start_year}/{start_year + 1}',
                                                        defaults={'start_date': date(start_year, 9, 8)})
        past = []
        for name, start in (('1st Term', date(start_year, 9, 8)), ('2nd Term', date(start_year + 1, 1, 5)),
                            ('3rd Term', date(start_year + 1, 4, 20))):
            term, _ = Term.objects.get_or_create(tenant=self.t, session=last, name=name, defaults={
                'start_date': start, 'end_date': start + timedelta(days=84), 'is_active': False})
            past.append(term)
        self.result_count = 0
        for st in self.students:
            ability = st._ability - 4
            for term in past:
                ability += rng.gauss(1.5, 2)
                for subject in self.subjects:
                    score = max(25, min(97, ability + rng.gauss(0, 8)))
                    TermResult.objects.create(
                        tenant=self.t, student=st, class_assigned=st.class_assigned, subject=subject,
                        session=last, term=term, ca1_score=round(score * .1), ca2_score=round(score * .1),
                        ca3_score=round(score * .1), exam_score=round(score * .7), published=True)
                    self.result_count += 1
            for subject in self.subjects:                 # this term: two tests in, exams to come
                score = max(25, min(97, ability + rng.gauss(0, 8)))
                TermResult.objects.create(tenant=self.t, student=st, class_assigned=st.class_assigned,
                                          subject=subject, session=self.session, term=self.term,
                                          ca1_score=round(score * .1), ca2_score=round(score * .1))
                self.result_count += 1

    # ── money ────────────────────────────────────────────────────────────
    def _fees(self):
        from finance.models import FeePayment, FeeStructure, Payment, Wallet
        from finance.services.payment_service import approve_payment, create_payment, ensure_child_billing
        due = self.term.start_date + timedelta(days=21)
        tuition = {'Grade 1': 85000, 'Grade 1A': 85000, 'Grade 2': 90000, 'Grade 3': 95000}
        fees = [FeeStructure.objects.create(tenant=self.t, name=f'{name} tuition', amount=Decimal(amount),
                                            category='FEE', term=self.term, session=self.session,
                                            school_class=self.classes[name], is_compulsory=True, due_date=due)
                for name, amount in tuition.items()]
        fees += [FeeStructure.objects.create(tenant=self.t, name=name, amount=Decimal(amount), category=cat,
                                             term=self.term, session=self.session, is_compulsory=True, due_date=due)
                 for name, amount, cat in (('Development levy', 10000, 'FEE'), ('PTA levy', 5000, 'FEE'),
                                           ('Textbooks', 12000, 'TEXTBOOK'))]
        for st in self.students:
            for fee in fees:
                if fee.school_class_id in (None, st.class_assigned_id):
                    FeePayment.objects.create(tenant=self.t, student=st, fee_structure=fee,
                                              balance_due=fee.amount, status='UNPAID')
            ensure_child_billing(st, self.term, self.session)

        # One wallet per family (the eldest child's). Families pay in instalments over the term: some in full, some part, two not yet.
        share = [1.0, 1.0, 0.6, 1.0, 0.5, 0.0, 1.0, 0.3, 0.0, 0.8]
        methods = ['BANK_TRANSFER', 'POS', 'CASH', 'PAYSTACK']
        weeks_in = max(1, (self.today - self.term.start_date).days // 7)
        self.paid_total = Decimal('0')
        for wallet in Wallet.objects.filter(tenant=self.t, student__in=self.students).select_related('student'):
            family = [s for s in self.students if s.parent_email == wallet.student.parent_email]
            owed = sum(Decimal(str(ensure_child_billing(s, self.term, self.session)[1].total_fee)) for s in family)
            pay = (owed * Decimal(str(min(share[self.students.index(s)] for s in family)))).quantize(Decimal('1'))
            parts = [pay] if pay < 60000 else [(pay * Decimal('0.6')).quantize(Decimal('1')),
                                              pay - (pay * Decimal('0.6')).quantize(Decimal('1'))]
            for n, amount in enumerate(p for p in parts if p > 0):
                payment = create_payment(wallet, amount, self.rng.choice(methods),
                                         description=f'{self.term.name} school fees', created_by=self.admin)
                approve_payment(payment, self.admin)
                when = timezone.now() - timedelta(days=7 * self.rng.randint(0, weeks_in - 1) + n * 3, hours=3)
                Payment.objects.filter(pk=payment.pk).update(created_at=when, approved_at=when)
                self.paid_total += amount
        for st in self.students:
            ensure_child_billing(st, self.term, self.session)

    # ── calendar ─────────────────────────────────────────────────────────
    def _homework_and_events(self):
        from announcement.models import Announcement
        tasks = ['Workbook exercise', 'Reading and questions', 'Project: make a chart', 'Spelling practice',
                 'Revision worksheet']
        for c, cls in enumerate(self.classes.values()):
            for j in range(3):
                k = (c * 3 + j) % len(self.subjects)
                Homework.objects.create(tenant=self.t, school_class=cls, subject=self.subjects[k],
                                        set_by=self.teachers[k], title=f'{self.subjects[k].name}: {tasks[(c + j) % 5]}',
                                        details='Complete in your exercise book and bring it to the next lesson.',
                                        set_on=self.today - timedelta(days=1 + j), due_on=self.today + timedelta(days=2 + j * 3))
        t = self.today
        for title, kind, start, days, audience, venue in (
                ('Mid-term break', 'HOLIDAY', t + timedelta(days=10), 2, 'ALL', ''),
                ('PTA general meeting', 'PTA', t + timedelta(days=5), 0, 'PARENTS', 'School hall'),
                ('Inter-house sports', 'SPORTS', t + timedelta(days=17), 0, 'ALL', 'Sports field'),
                ('Staff meeting', 'MEETING', t + timedelta(days=3), 0, 'STAFF', 'Staff room'),
                ('Science and arts fair', 'CULTURAL', t + timedelta(days=24), 0, 'ALL', 'Assembly ground'),
                ('First term examinations', 'EXAM', (self.term.end_date or t + timedelta(days=50)) - timedelta(days=14),
                 9, 'ALL', '')):
            CalendarEvent.objects.create(tenant=self.t, title=title, kind=kind, start_date=start,
                                         end_date=start + timedelta(days=days) if days else None, audience=audience,
                                         venue=venue, created_by=self.admin, start_time=None if days else time(10, 0),
                                         description=f'{title} — see the school calendar for details.')
        for title, message in (
                ('Welcome back to the new term', 'We are glad to welcome every pupil back. Please make sure '
                 'your child comes to school in full uniform and on time (7:45am).'),
                ('Inter-house sports', 'Our inter-house sports competition holds soon. Parents are warmly invited '
                 'to cheer their children on.')):
            Announcement.objects.create(tenant=self.t, title=title, message=message, audience='both',
                                        term=self.term.name, session=self.session.name, created_by=self.admin,
                                        published=True, published_at=timezone.now(), notify_parents=False)

    def _report(self):
        w = self.stdout.write
        w(self.style.SUCCESS(f'Seeded {self.t.name}:'))
        w(f'  10 subjects, 10 teachers, 10 students, {len({s.parent_email for s in self.students})} families')
        w(f'  {self.attendance_count} attendance marks, {self.result_count} results, '
          f'₦{self.paid_total:,.0f} in approved fee payments, timetable published')
        w('')
        w('Sign in with:')
        w(f'  Teachers  username e.g. {TEACHERS[0][0]} (all {len(TEACHERS)}: '
          f'{", ".join(u for u, *_ in TEACHERS)})  password {TEACHER_PASSWORD}')
        w(f'  Students  admission number ISK26001 … ISK26010  password {STUDENT_PASSWORD}')
        w(f'  Parents   child\'s admission number ISK26001 … ISK26010  password {PARENT_PASSWORD}')
        w('  Form teachers: ' + ', '.join(f'{c.name} → {c.form_teacher.full_name}' for c in self.classes.values()))
