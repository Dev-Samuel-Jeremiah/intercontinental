"""
assistant/safety.py
───────────────────
Content safety for what students write (F179): discussion posts and
questions to the AI tutor.

Two layers:
  1. A short list of phrases checked on the server — always on, no AI needed.
  2. OpenAI's moderation check, when an OpenAI key is set and AI is switched
     on for the school. It is free to use; if it can't be reached the post is
     judged on the phrase list alone (it is never blocked because the check
     failed).

A flagged post is held — only its writer and the teacher see it — until the
teacher releases or deletes it. Anything suggesting a student may hurt
themselves is marked urgent and the teacher is emailed at once.
"""
import logging
import re
from dataclasses import dataclass

from django.conf import settings

logger = logging.getLogger(__name__)

URGENT = 'self-harm'
PHRASES = {
    URGENT: [r'\bkill(ing)? myself\b', r'\bwant(s)? to die\b', r'\bend (my|it) (life|all)\b', r'\bsuicid',
             r'\bcut(ting)? myself\b', r'\bhurt(ing)? myself\b', r'\bno reason to live\b'],
    'threats': [r"\b(i('ll| will)|gonna|going to) (kill|beat|stab|hurt) (you|him|her|them)\b",
                r'\bwatch your back\b'],
    'bullying': [r"\byou('re| are) (so )?(stupid|ugly|useless|worthless|dumb|fat)\b", r'\bnobody likes you\b',
                 r'\bgo die\b', r'\bkill yourself\b', r'\bkys\b'],
    'sexual content': [r'\bnudes?\b', r'\bsend (me )?pics\b', r'\bsex(y|ual)? ?(chat|video)\b'],
}
COMPILED = {cat: [re.compile(p, re.I) for p in pats] for cat, pats in PHRASES.items()}
# OpenAI category → our label
OPENAI_CATEGORIES = {'self-harm': URGENT, 'self-harm/intent': URGENT, 'self-harm/instructions': URGENT,
                     'harassment': 'bullying', 'harassment/threatening': 'threats', 'violence': 'threats',
                     'hate': 'hateful language', 'hate/threatening': 'threats', 'sexual': 'sexual content',
                     'sexual/minors': 'sexual content'}


@dataclass
class Verdict:
    flagged: bool = False
    category: str = ''

    @property
    def urgent(self):
        return self.category == URGENT

    @property
    def reason(self):
        if not self.flagged:
            return ''
        return ('May be about a student hurting themselves — please check on them.' if self.urgent
                else f'Held for a teacher to check: possible {self.category}.')


def local_check(text):
    for cat, patterns in COMPILED.items():
        if any(p.search(text or '') for p in patterns):
            return Verdict(True, cat)
    return Verdict()


def ai_check(tenant, text, user=None):
    """OpenAI's moderation check. Returns a Verdict, or None if it couldn't run."""
    from . import providers
    from .models import AIUsage
    if not getattr(settings, 'OPENAI_API_KEY', ''):
        return None
    if not providers.school_settings(tenant).enabled:
        return None
    try:
        body = providers._post('https://api.openai.com/v1/moderations',
                               {'Authorization': f'Bearer {settings.OPENAI_API_KEY}',
                                'Content-Type': 'application/json'},
                               {'model': 'omni-moderation-latest', 'input': text[:8000]})
        result = (body.get('results') or [{}])[0]
    except Exception as problem:   # never block a post because the check failed
        logger.warning('Moderation check failed: %s', problem)
        AIUsage.objects.create(tenant=tenant, feature='safety', provider='openai', user=user, ok=False,
                               error=str(problem)[:255])
        return None
    AIUsage.objects.create(tenant=tenant, feature='safety', provider='openai', model='omni-moderation-latest',
                           user=user)
    if not result.get('flagged'):
        return Verdict()
    cats = [c for c, on in (result.get('categories') or {}).items() if on]
    labels = [OPENAI_CATEGORIES.get(c, 'harmful content') for c in cats]
    return Verdict(True, URGENT if URGENT in labels else (labels[0] if labels else 'harmful content'))


def check(tenant, text, user=None):
    verdict = local_check(text)
    if verdict.urgent:
        return verdict
    ai = ai_check(tenant, text, user)
    if ai and ai.flagged and (ai.urgent or not verdict.flagged):
        return ai
    return verdict


def tell_teacher(course_id, what, link_path):
    """Email the course teacher about an urgent post. Runs in the background."""
    from classroom.models import Course
    from communications.services import send_email
    from django.utils.html import escape
    course = Course.objects.filter(pk=course_id).select_related('teacher', 'tenant').first()
    if not course or not course.teacher.email:
        return
    url = course.tenant.portal_url + link_path
    send_email(course.teacher.email, f'Please check on a student — {course.name}',
               f'<p>Something a student wrote in <b>{escape(course.name)}</b> may mean they are thinking of hurting '
               f'themselves:</p><blockquote>{escape(what[:500])}</blockquote>'
               f'<p>It has been hidden from other students. Please <a href="{url}">look at it</a> and follow the '
               f'school\'s safeguarding steps.</p>')
