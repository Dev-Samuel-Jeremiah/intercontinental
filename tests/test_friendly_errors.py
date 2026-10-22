"""
A form that fails on what was typed (too long, already there, empty, wrong format) sends the
person back with a plain message; opening a page that fails still shows the server error page.
"""
import json

from django.contrib.messages import get_messages
from django.contrib.messages.storage.cookie import CookieStorage
from django.core.exceptions import ValidationError
from django.db import DataError, IntegrityError
from django.test import RequestFactory, SimpleTestCase

from lms_project.error_views import ERRORS
from lms_project.friendly_errors import FriendlyFormErrorsMiddleware, explain


class FriendlyErrorTests(SimpleTestCase):

    def setUp(self):
        self.mw = FriendlyFormErrorsMiddleware(lambda r: None)

    def request(self, method='post', **extra):
        req = getattr(RequestFactory(), method)('/classes/arms/', HTTP_REFERER='http://testserver/classes/arms/', **extra)
        req._messages = CookieStorage(req)
        return req

    def test_messages_for_each_kind_of_mistake(self):
        cases = [
            (DataError('value too long for type character varying(10)'), 'at most 10 characters'),
            (IntegrityError('duplicate key value violates unique constraint'), 'already exists'),
            (IntegrityError('UNIQUE constraint failed: users_class.name'), 'already exists'),
            (IntegrityError('null value in column "name" violates not-null constraint'), 'required box'),
            (ValidationError('Term start date must be before end date.'), 'Term start date must be before end date.'),
            (ValueError("invalid literal for int() with base 10: 'abc'"), 'not in the right format'),
        ]
        for exc, text in cases:
            self.assertIn(text, explain(exc), exc)
        self.assertIsNone(explain(KeyError('x')))           # a real fault stays a server error

    def test_a_form_goes_back_with_the_message(self):
        req = self.request()
        resp = self.mw.process_exception(req, DataError('value too long for type character varying(10)'))
        self.assertEqual((resp.status_code, resp['Location']), (302, 'http://testserver/classes/arms/'))
        self.assertIn('at most 10 characters', str(list(get_messages(req))[0]))

    def test_scripts_get_json(self):
        req = self.request(HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        resp = self.mw.process_exception(req, IntegrityError('duplicate key'))
        self.assertEqual(resp.status_code, 400)
        self.assertIn('already exists', json.loads(resp.content)['message'])

    def test_opening_a_page_is_left_alone(self):
        self.assertIsNone(self.mw.process_exception(self.request('get'), DataError('value too long')))
        self.assertIsNone(self.mw.process_exception(self.request(), RuntimeError('bug')))

    def test_the_error_page_says_the_portal_is_still_running(self):
        self.assertIn('still running', ERRORS[500]['message'])
