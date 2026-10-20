"""
Groq runs the platform's AI: it is the default provider for every feature
when its key is set, and it does the safety check on what students post using
its policy-following safeguard model (OpenAI's moderation check remains the
fallback without a Groq key).
"""
import json
from unittest import mock

from django.test import override_settings

from assistant import providers, safety
from assistant.models import FEATURES, AIUsage

from .factories import TenantTestCase, make_school


def chat(content):
    return {'choices': [{'message': {'content': content}}], 'usage': {'prompt_tokens': 50, 'completion_tokens': 9}}


class GroqTests(TenantTestCase):

    def setUp(self):
        super().setUp()
        self.school = make_school('alpha')

    @override_settings(GROQ_API_KEY='g-key', OPENAI_API_KEY='o-key', AI_DEFAULT_PROVIDER='groq')
    def test_groq_comes_first_for_every_feature(self):
        for feature, _label in FEATURES:
            self.assertEqual(providers.order_for(self.school, feature), ['groq', 'openai'], feature)
        with mock.patch('assistant.providers._post', return_value=chat('Well done, Ada.')) as post:
            result = providers.complete(self.school, 'report_comments', 'system', 'prompt')
        self.assertEqual((result.provider, result.text), ('groq', 'Well done, Ada.'))
        self.assertIn('api.groq.com', post.call_args[0][0])

    @override_settings(GROQ_API_KEY='g-key', OPENAI_API_KEY='')
    def test_safety_check_uses_groq_safeguard(self):
        replies = iter([chat('{"flagged": true, "category": "self-harm"}'), chat('{"flagged": false, "category": ""}')])
        with mock.patch('assistant.providers._post', side_effect=lambda url, h, payload: next(replies)) as post:
            urgent = safety.check(self.school, 'I feel like nothing matters any more')
            fine = safety.check(self.school, 'Who can help me with fractions?')
        self.assertTrue(urgent.urgent)
        self.assertFalse(fine.flagged)
        url, _headers, payload = post.call_args_list[0][0]
        self.assertIn('api.groq.com', url)
        self.assertEqual(payload['model'], 'openai/gpt-oss-safeguard-20b')
        self.assertIn('self-harm', payload['messages'][0]['content'])
        self.assertEqual(AIUsage.objects.filter(feature='safety', provider='groq', ok=True).count(), 2)

    @override_settings(GROQ_API_KEY='g-key')
    def test_unknown_labels_and_failures(self):
        with mock.patch('assistant.providers._post', return_value=chat(json.dumps({'flagged': True, 'category': 'spam'}))):
            self.assertEqual(safety.ai_check(self.school, 'buy now').category, 'harmful content')
        with mock.patch('assistant.providers._post', side_effect=providers.AIError('HTTP 500')):
            self.assertIsNone(safety.ai_check(self.school, 'hello'))          # never blocks a post
        self.assertTrue(AIUsage.objects.filter(feature='safety', provider='groq', ok=False).exists())
