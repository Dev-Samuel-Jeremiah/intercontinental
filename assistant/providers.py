"""
assistant/providers.py
──────────────────────
One way to ask an AI for text, whichever company provides it.

    from assistant.providers import complete
    result = complete(tenant, 'lesson_note', system, prompt, user=request.user, json_mode=True)
    result.text / result.data (parsed JSON) / result.provider

The school's AI settings pick the provider per feature (F175); if that
provider has no key or fails, the next one with a key is tried. Every call is
logged with its token count, and a school that has used its monthly
allowance is stopped before the call is made (F174).

Calls go straight to each provider's HTTPS API with `requests`, so no extra
SDK is needed. Keys come from .env: OPENAI_API_KEY, GEMINI_API_KEY,
ANTHROPIC_API_KEY.
"""
import json
import logging
from dataclasses import dataclass, field

import requests
from django.conf import settings
from django.db.models import Sum
from django.utils import timezone

from .models import PROVIDERS, AISettings, AIUsage

logger = logging.getLogger(__name__)
TIMEOUT = 60
NAMES = dict(PROVIDERS)


class AIError(Exception):
    """Something the user should be told in plain words."""


@dataclass
class AIResult:
    text: str
    provider: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    data: object = field(default=None)


# ─────────────────────────────────────────────────────────────────────────────
# Settings and allowance
# ─────────────────────────────────────────────────────────────────────────────

def school_settings(tenant):
    obj, _ = AISettings.objects.get_or_create(tenant=tenant)
    return obj


def keys():
    return {'openai': getattr(settings, 'OPENAI_API_KEY', ''), 'gemini': getattr(settings, 'GEMINI_API_KEY', ''),
            'claude': getattr(settings, 'ANTHROPIC_API_KEY', '')}


def available():
    """Providers that have a key on this server."""
    k = keys()
    return [p for p, _ in PROVIDERS if k.get(p)]


def order_for(tenant, feature):
    s = school_settings(tenant)
    first = (s.feature_providers or {}).get(feature) or s.default_provider or \
        getattr(settings, 'AI_DEFAULT_PROVIDER', 'openai')
    have = available()
    return ([first] if first in have else []) + [p for p in have if p != first]


def allowance(tenant):
    s = school_settings(tenant)
    return s.monthly_tokens if s.monthly_tokens is not None else getattr(settings, 'AI_MONTHLY_TOKENS', 300000)


def used_this_month(tenant):
    start = timezone.localtime().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    totals = AIUsage.objects.filter(tenant=tenant, created_at__gte=start).aggregate(
        i=Sum('input_tokens'), o=Sum('output_tokens'))
    return (totals['i'] or 0) + (totals['o'] or 0)


def check_allowed(tenant):
    s = school_settings(tenant)
    if not s.enabled:
        raise AIError('AI features are switched off for this school. An administrator can switch them on under '
                      'AI settings.')
    if not available():
        raise AIError('No AI provider is set up on this server yet. Add OPENAI_API_KEY (or GEMINI_API_KEY / '
                      'ANTHROPIC_API_KEY) to the .env file and restart.')
    cap = allowance(tenant)
    if cap and used_this_month(tenant) >= cap:
        raise AIError("This school has used this month's AI allowance. An administrator can raise it under "
                      'AI settings.')


# ─────────────────────────────────────────────────────────────────────────────
# Providers
# ─────────────────────────────────────────────────────────────────────────────

def _post(url, headers, payload):
    """The one place that talks to the outside world (tests replace this)."""
    resp = requests.post(url, headers=headers, json=payload, timeout=TIMEOUT)
    try:
        body = resp.json()
    except ValueError:
        body = {}
    if resp.status_code >= 400:
        detail = (body.get('error') or {}) if isinstance(body, dict) else {}
        message = detail.get('message') if isinstance(detail, dict) else str(detail)
        raise AIError(f'HTTP {resp.status_code}: {message or resp.text[:200]}')
    return body


def _openai(system, prompt, json_mode, max_tokens, temperature):
    model = getattr(settings, 'OPENAI_MODEL', 'gpt-4o-mini')
    payload = {'model': model, 'max_completion_tokens': max_tokens,
               'messages': [{'role': 'system', 'content': system}, {'role': 'user', 'content': prompt}]}
    if not model.startswith(('o1', 'o3', 'o4', 'gpt-5')):
        payload['temperature'] = temperature          # reasoning models only take the default
    if json_mode:
        payload['response_format'] = {'type': 'json_object'}
    body = _post('https://api.openai.com/v1/chat/completions',
                 {'Authorization': f'Bearer {keys()["openai"]}', 'Content-Type': 'application/json'}, payload)
    text = body['choices'][0]['message']['content'] or ''
    usage = body.get('usage') or {}
    return AIResult(text, 'openai', model, usage.get('prompt_tokens', 0), usage.get('completion_tokens', 0))


def _gemini(system, prompt, json_mode, max_tokens, temperature):
    model = getattr(settings, 'GEMINI_MODEL', 'gemini-2.0-flash')
    config = {'maxOutputTokens': max_tokens, 'temperature': temperature}
    if json_mode:
        config['responseMimeType'] = 'application/json'
    payload = {'systemInstruction': {'parts': [{'text': system}]},
               'contents': [{'role': 'user', 'parts': [{'text': prompt}]}], 'generationConfig': config}
    body = _post(f'https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent',
                 {'x-goog-api-key': keys()['gemini'], 'Content-Type': 'application/json'}, payload)
    parts = ((body.get('candidates') or [{}])[0].get('content') or {}).get('parts') or []
    usage = body.get('usageMetadata') or {}
    return AIResult(''.join(p.get('text', '') for p in parts), 'gemini', model,
                    usage.get('promptTokenCount', 0), usage.get('candidatesTokenCount', 0))


def _claude(system, prompt, json_mode, max_tokens, temperature):
    model = getattr(settings, 'ANTHROPIC_MODEL', 'claude-sonnet-4-20250514')
    if json_mode:
        system += '\nReply with one JSON object and nothing else.'
    payload = {'model': model, 'max_tokens': max_tokens, 'temperature': temperature, 'system': system,
               'messages': [{'role': 'user', 'content': prompt}]}
    body = _post('https://api.anthropic.com/v1/messages',
                 {'x-api-key': keys()['claude'], 'anthropic-version': '2023-06-01',
                  'Content-Type': 'application/json'}, payload)
    text = ''.join(b.get('text', '') for b in body.get('content') or [] if b.get('type') == 'text')
    usage = body.get('usage') or {}
    return AIResult(text, 'claude', model, usage.get('input_tokens', 0), usage.get('output_tokens', 0))


CALLERS = {'openai': _openai, 'gemini': _gemini, 'claude': _claude}


def parse_json(text):
    """JSON from a model reply, tolerating ```json fences and text around the object."""
    t = (text or '').strip()
    if t.startswith('```'):
        t = t.strip('`')
        if t.lower().startswith('json'):
            t = t[4:]
    try:
        return json.loads(t)
    except ValueError:
        start, end = t.find('{'), t.rfind('}')
        if start >= 0 and end > start:
            return json.loads(t[start:end + 1])
        raise


def complete(tenant, feature, system, prompt, user=None, json_mode=False, max_tokens=1200, temperature=0.7):
    """Ask the school's chosen AI; fall back to the next provider on failure. Raises AIError."""
    check_allowed(tenant)
    errors = []
    for provider in order_for(tenant, feature):
        try:
            result = CALLERS[provider](system, prompt, json_mode, max_tokens, temperature)
            if json_mode:
                result.data = parse_json(result.text)
            AIUsage.objects.create(tenant=tenant, feature=feature, provider=provider, model=result.model, user=user,
                                   input_tokens=result.input_tokens, output_tokens=result.output_tokens)
            return result
        except (AIError, requests.RequestException, ValueError, KeyError, IndexError, TypeError) as problem:
            logger.warning('AI %s via %s failed: %s', feature, provider, problem)
            errors.append(f'{NAMES[provider]}: {problem}')
            AIUsage.objects.create(tenant=tenant, feature=feature, provider=provider, user=user, ok=False,
                                   error=str(problem)[:255])
    raise AIError('The AI could not answer just now. ' + ' | '.join(errors))
