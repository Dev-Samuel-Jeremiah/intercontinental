"""
AI assistants: provider choice and fallback, usage limits, lesson-note and
question drafts, parent messages that wait for a person's approval,
announcement translations and varied report comments.

No real AI is called: `assistant.providers._post` is replaced by FakeAI,
which answers in each provider's own response format.
"""
import json
from datetime import timedelta
from unittest import mock

from django.core import mail
from django.test import override_settings
from django.utils import timezone

from academics.models import AcademicSession, Term
from announcement.models import Announcement
from assistant import providers
from assistant.models import AIDraft, AISettings, AIUsage, AnnouncementTranslation
from cbt.models import BankQuestion
from curriculum.models import LessonNote
from results.ai_comments import generate_comments_for_student, opening
from tenants.middleware import set_current_tenant
from users.models import ClassSubject, Staff, StaffSubjectClass, Subject, User

from .factories import TenantTestCase, make_admin, make_class, make_school, make_student, make_teacher


class FakeAI:
    """Stands in for the providers' HTTPS APIs. Queue replies (text or dicts); each call pops one."""

    def __init__(self, *replies, fail=()):
        self.replies = list(replies)
        self.fail = set(fail)          # provider names that answer with an error
        self.calls = []

    def __call__(self, url, headers, payload):
        if 'moderations' in url:                       # the safety check: nothing flagged, not counted as a call
            return {'results': [{'flagged': False, 'categories': {}}]}
        provider = 'openai' if 'openai' in url else 'gemini' if 'google' in url else 'claude'
        self.calls.append((provider, payload))
        if provider in self.fail:
            raise providers.AIError('HTTP 500: provider down')
        reply = self.replies.pop(0) if self.replies else {}
        text = reply if isinstance(reply, str) else json.dumps(reply)
        if provider == 'openai':
            return {'choices': [{'message': {'content': text}}], 'usage': {'prompt_tokens': 100, 'completion_tokens': 50}}
        if provider == 'gemini':
            return {'candidates': [{'content': {'parts': [{'text': text}]}}],
                    'usageMetadata': {'promptTokenCount': 80, 'candidatesTokenCount': 40}}
        return {'content': [{'type': 'text', 'text': text}], 'usage': {'input_tokens': 90, 'output_tokens': 45}}

    def prompt(self, n=-1):
        payload = self.calls[n][1]
        if 'messages' in payload:
            return payload['messages'][-1]['content']
        return payload['contents'][0]['parts'][0]['text']


KEYS = dict(OPENAI_API_KEY='sk-test', GEMINI_API_KEY='', ANTHROPIC_API_KEY='', AI_DEFAULT_PROVIDER='openai',
            AI_MONTHLY_TOKENS=100000, RUN_TASKS_EAGERLY=True)


@override_settings(**KEYS)
class Base(TenantTestCase):

    def setUp(self):
        super().setUp()
        self.school = make_school('alpha')
        self.admin = make_admin(self.school)
        self.teacher_user = make_teacher(self.school, username='alphat', full_name='Mrs Ade')
        self.jss2 = make_class(self.school, 'JSS 2A')
        set_current_tenant(self.school)
        self.teacher = Staff.objects.get(user=self.teacher_user)
        self.maths = Subject.objects.create(tenant=self.school, name='Mathematics')
        self.ada = make_student(self.school, 'A/001', full_name='Ada Obi', school_class=self.jss2,
                                parent_email='mum@example.com')

    def fake(self, *replies, **kw):
        fake = FakeAI(*replies, **kw)
        patcher = mock.patch('assistant.providers._post', side_effect=fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        return fake

    def _get(self, path, **kw):
        return self.client.get(path, HTTP_HOST=self.host(self.school), **kw)

    def _post(self, path, data=None, **kw):
        return self.client.post(path, data or {}, HTTP_HOST=self.host(self.school), **kw)


class ProviderTests(Base):

    def test_openai_call_is_logged_with_tokens(self):
        fake = self.fake({'ok': True})
        result = providers.complete(self.school, 'questions', 'sys', 'hello', user=self.admin, json_mode=True)
        self.assertEqual((result.provider, result.data), ('openai', {'ok': True}))
        self.assertEqual(fake.calls[0][1]['response_format'], {'type': 'json_object'})
        u = AIUsage.objects.get()
        self.assertEqual((u.tokens, u.feature, u.ok, u.user), (150, 'questions', True, self.admin))

    @override_settings(GEMINI_API_KEY='g-test')
    def test_falls_back_when_the_first_provider_fails(self):
        fake = self.fake('fine', fail={'openai'})
        result = providers.complete(self.school, 'translation', 'sys', 'hi')
        self.assertEqual(result.provider, 'gemini')
        self.assertEqual([c[0] for c in fake.calls], ['openai', 'gemini'])
        self.assertEqual(list(AIUsage.objects.order_by('id').values_list('ok', flat=True)), [False, True])

    @override_settings(GEMINI_API_KEY='g-test', ANTHROPIC_API_KEY='a-test')
    def test_school_can_pick_a_provider_per_feature(self):
        AISettings.objects.create(tenant=self.school, feature_providers={'questions': 'claude'})
        fake = self.fake('x', 'y')
        providers.complete(self.school, 'questions', 'sys', 'hi')
        providers.complete(self.school, 'translation', 'sys', 'hi')
        self.assertEqual([c[0] for c in fake.calls], ['claude', 'openai'])

    def test_monthly_allowance_stops_calls(self):
        AISettings.objects.create(tenant=self.school, monthly_tokens=1000)
        AIUsage.objects.create(tenant=self.school, feature='questions', provider='openai', input_tokens=900,
                               output_tokens=200)
        fake = self.fake('x')
        with self.assertRaises(providers.AIError) as raised:
            providers.complete(self.school, 'questions', 'sys', 'hi')
        self.assertIn('allowance', str(raised.exception))
        self.assertEqual(fake.calls, [])
        # Last month's use doesn't count.
        AIUsage.objects.update(created_at=timezone.now() - timedelta(days=40))
        providers.complete(self.school, 'questions', 'sys', 'hi')

    def test_switched_off_or_no_key(self):
        AISettings.objects.create(tenant=self.school, enabled=False)
        with self.assertRaises(providers.AIError):
            providers.complete(self.school, 'questions', 'sys', 'hi')
        AISettings.objects.update(enabled=True)
        with override_settings(OPENAI_API_KEY=''):
            with self.assertRaises(providers.AIError) as raised:
                providers.complete(self.school, 'questions', 'sys', 'hi')
            self.assertIn('OPENAI_API_KEY', str(raised.exception))

    def test_json_in_fences_is_read(self):
        self.assertEqual(providers.parse_json('```json\n{"a": 1}\n```'), {'a': 1})
        self.assertEqual(providers.parse_json('Here you go: {"a": 2} thanks'), {'a': 2})

    def test_settings_page_for_admins_only(self):
        self.client.force_login(self.teacher_user)
        self.assertEqual(self._get('/assistant/settings/').status_code, 302)
        self.client.force_login(self.admin)
        self._post('/assistant/settings/', {'enabled': '1', 'default_provider': 'gemini', 'p_questions': 'claude',
                                            'monthly_tokens': '50,000'})
        s = AISettings.objects.get(tenant=self.school)
        self.assertEqual((s.default_provider, s.feature_providers, s.monthly_tokens),
                         ('gemini', {'questions': 'claude'}, 50000))
        self.assertContains(self._get('/assistant/settings/'), 'AI settings')
        self.assertContains(self._get('/assistant/'), 'first draft')


class LessonNoteTests(Base):

    def test_ai_draft_fills_the_form_without_saving(self):
        session = AcademicSession.objects.create(tenant=self.school, name='2026/2027', is_active=True)
        Term.objects.create(tenant=self.school, session=session, name="1st Term", is_active=True,
                                   start_date=timezone.localdate() - timedelta(days=20))
        ClassSubject.objects.create(tenant=self.school, school_class=self.jss2, subject=self.maths)
        StaffSubjectClass.objects.create(tenant=self.school, staff=self.teacher, subject=self.maths,
                                         school_class=self.jss2)
        fake = self.fake({'title': 'Adding fractions', 'objectives': 'Students should be able to add fractions',
                          'content': 'Previous knowledge\nIntroduction (5 min)\nPresentation\nStep 1: common denominators'})
        self.client.force_login(self.teacher_user)
        r = self._post('/curriculum/notes/new/', {'ai': '1', 'subject': self.maths.pk, 'school_class': self.jss2.pk,
                                                  'week': '3', 'title': 'Fractions', 'minutes': '40'})
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'Step 1: common denominators')
        self.assertContains(r, 'nothing is saved yet')
        self.assertFalse(LessonNote.objects.exists())
        self.assertIn('Topic: Fractions', fake.prompt())
        self.assertIn('JSS 2A', fake.prompt())


class QuestionTests(Base):

    def test_generate_check_and_save_to_the_bank(self):
        self.fake({'questions': [
            {'text': 'What is 2 + 3?', 'options': ['4', '5', '6', '7'], 'correct': 1, 'explanation': '2 + 3 = 5'},
            {'text': 'Broken', 'options': ['only one'], 'correct': 0},
            {'text': 'What is 3 x 3?', 'options': ['6', '9', '12', '3'], 'correct': 9},
        ]})
        self.client.force_login(self.teacher_user)
        r = self._post('/assistant/questions/', {'subject': self.maths.pk, 'level': 'JSS 2', 'topic': 'Arithmetic',
                                                  'count': '3', 'difficulty': 'EASY', 'qtype': 'MCQ'})
        self.assertContains(r, 'What is 2 + 3?')
        self.assertNotContains(r, 'Broken')                    # one option only
        self.assertNotContains(r, 'What is 3 x 3?')            # answer index out of range
        r = self._post('/assistant/questions/', {
            'action': 'save', 'subject': self.maths.pk, 'level': 'JSS 2', 'topic': 'Arithmetic', 'difficulty': 'EASY',
            'qtype': 'MCQ', 'keep': ['0'], 'q0_text': 'What is 2 + 3?', 'q0_correct': '1', 'q0_option_0': '4',
            'q0_option_1': '5', 'q0_option_2': '6', 'q0_option_3': '7', 'q0_explanation': '2 + 3 = 5'})
        self.assertEqual(r.status_code, 302)
        q = BankQuestion.objects.get()
        self.assertEqual((q.topic, q.difficulty, q.level), ('Arithmetic', 'EASY', 'JSS 2'))
        self.assertEqual(q.options.get(is_correct=True).text, '5')


class ParentMessageTests(Base):

    def _draft(self, user):
        self.fake({'subject': 'School fees for Ada', 'body': 'Dear Parent,\n\nAda has ₦45,000 outstanding.'})
        self.client.force_login(user)
        self._post('/assistant/messages/new/', {'student': self.ada.pk, 'purpose': 'FEES', 'channel': 'EMAIL',
                                                 'points': '₦45,000 outstanding', 'tone': 'formal', 'language': 'en'})
        return AIDraft.objects.get()

    def test_nothing_is_sent_until_approved(self):
        d = self._draft(self.teacher_user)
        self.assertEqual(d.status, 'PENDING')
        self.assertEqual(mail.outbox, [])
        # A teacher without messaging rights can't send it.
        self._post(f'/assistant/messages/{d.pk}/', {'action': 'send', 'subject': d.subject, 'body': d.body})
        d.refresh_from_db()
        self.assertEqual(d.status, 'PENDING')
        self.assertEqual(mail.outbox, [])
        # An administrator edits and sends.
        self.client.force_login(self.admin)
        self._post(f'/assistant/messages/{d.pk}/', {'action': 'send', 'subject': 'Fees', 'body': 'Edited text'})
        d.refresh_from_db()
        self.assertEqual((d.status, d.reviewed_by, d.body), ('SENT', self.admin, 'Edited text'))
        self.assertEqual(mail.outbox[0].to, ['mum@example.com'])
        self.assertIn('Edited text', mail.outbox[0].alternatives[0][0])

    def test_second_reviewer_rule(self):
        AISettings.objects.create(tenant=self.school, second_reviewer=True)
        d = self._draft(self.admin)
        self._post(f'/assistant/messages/{d.pk}/', {'action': 'send'})
        d.refresh_from_db()
        self.assertEqual(d.status, 'PENDING')
        other = make_admin(self.school, username='alphahead')
        self.client.force_login(other)
        self._post(f'/assistant/messages/{d.pk}/', {'action': 'send'})
        d.refresh_from_db()
        self.assertEqual(d.status, 'SENT')

    def test_writer_can_reject_and_others_cannot_peek(self):
        d = self._draft(self.teacher_user)
        self._post(f'/assistant/messages/{d.pk}/', {'action': 'reject', 'note': 'Wrong amount'})
        d.refresh_from_db()
        self.assertEqual(d.status, 'REJECTED')
        nosy = make_teacher(self.school, username='alphanosy', full_name='Nosy')
        self.client.force_login(nosy)
        self.assertEqual(self._get(f'/assistant/messages/{d.pk}/').status_code, 302)


class TranslationTests(Base):

    def test_parents_only_see_approved_translations(self):
        ann = Announcement.objects.create(tenant=self.school, title='Mid-term break', message='School closes Friday.',
                                          published=True, published_at=timezone.now(), audience='both')
        self.fake({'title': 'Hutun tsakiyar zangon karatu', 'message': 'Makaranta za ta rufe ranar Juma\'a.'})
        self.client.force_login(self.admin)
        self._post(f'/assistant/announcements/{ann.pk}/translations/', {'action': 'translate', 'languages': ['ha']})
        t = AnnouncementTranslation.objects.get()
        self.assertFalse(t.approved)
        parent = User.objects.create_user(username='mum', password='x', tenant=self.school, is_parent=True,
                                          email='mum@example.com')
        self.client.force_login(parent)
        self.assertNotContains(self._get(f'/announcement/{ann.pk}/?lang=ha'), 'Hutun')
        self.client.force_login(self.admin)
        self._post(f'/assistant/announcements/{ann.pk}/translations/', {'action': 'approve', 'id': t.pk,
                                                                         'title': t.title, 'message': t.message})
        self.client.force_login(parent)
        self.assertContains(self._get(f'/announcement/{ann.pk}/?lang=ha'), 'Hutun tsakiyar')


class ReportCommentTests(Base):

    def test_repeated_openings_are_asked_again_and_remembered(self):
        session = AcademicSession.objects.create(tenant=self.school, name='2026/2027', is_active=True)
        term = Term.objects.create(tenant=self.school, session=session, name='1st Term', is_active=True)
        taken = 'Ada has worked very hard this term.'
        fake = self.fake(
            {'teacher_comment': taken, 'hos_comment': 'A fine term.', 'overall_summary': 'Good.'},
            {'teacher_comment': 'What a term for Ada!', 'hos_comment': 'Keep it up.', 'overall_summary': 'Good.'})
        avoid = {opening(taken)}
        c = generate_comments_for_student(self.ada, term, session, [], 70.0, tone='formal', avoid=avoid)
        self.assertTrue(c.generation_ok)
        self.assertEqual(c.teacher_comment, 'What a term for Ada!')
        self.assertEqual(len(fake.calls), 2)
        self.assertIn('formal and measured', fake.prompt(0))
        self.assertIn(opening(taken), fake.prompt(0))
        self.assertIn(opening('What a term for Ada!'), avoid)

    def test_failure_is_recorded_not_raised(self):
        session = AcademicSession.objects.create(tenant=self.school, name='2026/2027')
        term = Term.objects.create(tenant=self.school, session=session, name='1st Term')
        self.fake(fail={'openai'})
        c = generate_comments_for_student(self.ada, term, session, [], 50.0)
        self.assertFalse(c.generation_ok)
        self.assertIn('could not answer', c.error_message)
