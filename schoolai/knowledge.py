"""
schoolai/knowledge.py
─────────────────────
What the school knows, and answering questions from it.

Knowledge comes from the school itself and stays in step automatically:
announcements, the calendar, fees, school facts (classes, subjects, terms,
contacts), the privacy notice, help articles — plus what staff write and what
they teach it by answering questions it could not.

Answering: the question is matched against the knowledge the asker may see
(BM25 ranking, nudged by how helpful each item has been). With an AI provider
configured, the best passages are given to it with strict instructions to
answer only from them; without one, the best passage is quoted. If nothing
matches well enough, the school AI says so honestly, and the question goes to
the administrators — whose answer becomes new knowledge for next time. Every
"helpful / not helpful" vote changes how items rank.
"""
import math
import re
from collections import Counter

from django.core.cache import cache
from django.db.models import Max
from django.utils import timezone

from .models import AskedQuestion, KnowledgeItem

TOP_K = 4
CONFIDENT = 2.2           # BM25 score of the best match above which we answer
STOP = set('''a an and are as at be but by can do does did for from has have how i if in is it its me my of on or
our so that the their them there these this to was we what when where which who why will with you your
please tell know about school'''.split())
SYNONYMS = {'fees': 'fee', 'charges': 'fee', 'cost': 'fee', 'price': 'fee', 'resume': 'resumption',
            'resumes': 'resumption', 'reopen': 'resumption', 'reopening': 'resumption', 'holiday': 'holiday',
            'vacation': 'holiday', 'break': 'holiday', 'phone': 'contact', 'call': 'contact', 'email': 'contact',
            'principal': 'head', 'headteacher': 'head', 'exam': 'exam', 'exams': 'exam', 'examination': 'exam',
            'test': 'exam', 'uniform': 'uniform', 'uniforms': 'uniform'}


class KnowledgeError(ValueError):
    pass


# ─────────────────────────────────────────────────────────────────────────────
# Words
# ─────────────────────────────────────────────────────────────────────────────

def _stem(w):
    for suffix in ('ings', 'ing', 'ies', 'es', 's', 'ed'):
        if len(w) > len(suffix) + 3 and w.endswith(suffix):
            return w[:-len(suffix)] + ('y' if suffix == 'ies' else '')
    return w


def tokens(text):
    words = re.findall(r"[a-z0-9₦]+", (text or '').lower())
    out = []
    for w in words:
        if w in STOP or len(w) < 2:
            continue
        w = SYNONYMS.get(w, w)
        out.append(_stem(w))
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Keeping knowledge in step with the school
# ─────────────────────────────────────────────────────────────────────────────

def _upsert(tenant, key, title, body, source, audience, seen):
    seen.add(key)
    item = KnowledgeItem.objects.filter(tenant=tenant, source_key=key).first()
    if item is None:
        KnowledgeItem.objects.create(tenant=tenant, source_key=key, title=title[:200], body=body, source=source,
                                     audience=audience)
        return 1
    if (item.title, item.body, item.audience) != (title[:200], body, audience):
        item.title, item.body, item.audience = title[:200], body, audience
        item.save(update_fields=['title', 'body', 'audience', 'updated_at'])
    return 0


def sync(tenant):
    """Bring automatic knowledge in line with the school's content. Returns a short summary."""
    seen, added = set(), 0
    today = timezone.localdate()
    try:
        from announcement.models import Announcement
        for a in Announcement.objects.filter(tenant=tenant).order_by('-id')[:200]:
            added += _upsert(tenant, f'announcement:{a.pk}', a.title, a.message, 'ANNOUNCEMENT', 'ALL', seen)
    except Exception:
        pass
    try:
        from curriculum.models import CalendarEvent
        aud = {'ALL': 'ALL', 'STAFF': 'STAFF', 'PARENTS': 'ALL'}
        for e in CalendarEvent.objects.filter(tenant=tenant, start_date__gte=today.replace(year=today.year - 1)):
            when = f'{e.start_date:%A %d %B %Y}' + (f' to {e.end_date:%A %d %B %Y}' if e.end_date and e.end_date != e.start_date else '')
            if e.start_time:
                when += f', {e.start_time:%H:%M}' + (f'–{e.end_time:%H:%M}' if e.end_time else '')
            body = f'{e.title}: {when}.' + (f' Venue: {e.venue}.' if e.venue else '') + (f' {e.description}' if e.description else '')
            added += _upsert(tenant, f'calendar:{e.pk}', f'{e.title} ({e.get_kind_display()})', body, 'CALENDAR',
                             aud.get(e.audience, 'ALL'), seen)
    except Exception:
        pass
    try:
        from academics.models import AcademicSession, Term
        for t in Term.objects.filter(tenant=tenant).select_related('session').order_by('-start_date')[:6]:
            if t.start_date:
                body = (f'{t.name} of the {t.session.name} session runs from {t.start_date:%A %d %B %Y}'
                        + (f' to {t.end_date:%A %d %B %Y}' if t.end_date else '') + '.'
                        + (' This is the current term.' if t.is_active else ''))
                added += _upsert(tenant, f'term:{t.pk}', f'{t.name} {t.session.name} dates (resumption and closing)',
                                 body, 'SCHOOL', 'ALL', seen)
    except Exception:
        pass
    try:
        from finance.models import FeeStructure
        fees = list(FeeStructure.objects.filter(tenant=tenant, is_active=True).select_related('school_class', 'term'))
        if fees:
            lines = [f'- {f.name}: ₦{f.amount:,.0f}' + (f' ({f.school_class.name})' if f.school_class else ' (all classes)')
                     + (f', {f.term}' if f.term else '') + (' — compulsory' if f.is_compulsory else '') for f in fees]
            added += _upsert(tenant, 'fees:current', 'School fees and charges', 'Current fees:\n' + '\n'.join(lines)
                             + '\nFees can be paid from the family wallet on the parent portal.', 'FEES', 'ALL', seen)
    except Exception:
        pass
    try:
        from users.models import Class, Subject, Staff, ENROLLED_STATUSES, Student
        t = tenant
        contact = ' '.join(x for x in (f'Address: {t.address}.' if t.address else '', f'Phone: {t.phone}.' if t.phone else '',
                                       f'Email: {t.email}.' if t.email else '') if x)
        added += _upsert(tenant, 'school:contact', f'Contacting {t.name}', f'{t.name}. {contact}', 'SCHOOL', 'ALL', seen)
        classes = ', '.join(Class.objects.filter(tenant=tenant).order_by('name').values_list('name', flat=True))
        if classes:
            added += _upsert(tenant, 'school:classes', 'Classes at the school', f'Classes: {classes}.', 'SCHOOL', 'ALL', seen)
        subjects = ', '.join(Subject.objects.filter(tenant=tenant).order_by('name').values_list('name', flat=True))
        if subjects:
            added += _upsert(tenant, 'school:subjects', 'Subjects offered', f'Subjects: {subjects}.', 'SCHOOL', 'ALL', seen)
        heads = [f'{s.full_name} ({s.get_role_display()})' for s in Staff.objects.filter(tenant=tenant, role='ADMIN')[:10]]
        if heads:
            added += _upsert(tenant, 'school:admins', 'School administrators', 'Administrators: ' + ', '.join(heads) + '.',
                             'SCHOOL', 'STAFF', seen)
        enrolled = Student.objects.filter(tenant=tenant, status__in=ENROLLED_STATUSES).count()
        staff = Staff.objects.filter(tenant=tenant).count()
        added += _upsert(tenant, 'school:numbers', 'School at a glance (numbers)',
                         f'{enrolled} students on the roll and {staff} members of staff, as of {today:%d %B %Y}.',
                         'SCHOOL', 'STAFF', seen)
    except Exception:
        pass
    try:
        from security.models import PrivacyNotice
        n = PrivacyNotice.objects.filter(tenant=tenant).first()
        if n and n.notice:
            added += _upsert(tenant, 'policy:privacy', 'Privacy notice — how the school uses personal data', n.notice,
                             'POLICY', 'ALL', seen)
    except Exception:
        pass
    try:
        from saas_admin.models import Article
        for art in Article.objects.filter(published=True):
            added += _upsert(tenant, f'help:{art.pk}', f'How to: {art.title}', f'{art.summary}\n{art.body}', 'HELP',
                             'STAFF', seen)
    except Exception:
        pass
    # Automatic items whose source has gone are switched off (never staff-written ones)
    gone = (KnowledgeItem.objects.filter(tenant=tenant, active=True).exclude(source__in=('MANUAL', 'ANSWER'))
            .exclude(source_key='').exclude(source_key__in=seen))
    removed = gone.update(active=False)
    reactivated = KnowledgeItem.objects.filter(tenant=tenant, active=False, source_key__in=seen).exclude(
        source__in=('MANUAL', 'ANSWER')).update(active=True)
    cache.delete(_index_key(tenant))
    return f'{len(seen)} automatic items ({added} new, {removed} retired{", " + str(reactivated) + " back" if reactivated else ""})'


# ─────────────────────────────────────────────────────────────────────────────
# Search (BM25)
# ─────────────────────────────────────────────────────────────────────────────

def _index_key(tenant):
    return f'schoolai:index:{tenant.pk}'


def _visible(audience):
    return ('ALL', 'STAFF') if audience == 'STAFF' else ('ALL', 'FAMILY')


def _index(tenant):
    stamp = KnowledgeItem.objects.filter(tenant=tenant).aggregate(m=Max('updated_at'))['m']
    count = KnowledgeItem.objects.filter(tenant=tenant).count()
    key = _index_key(tenant)
    cached = cache.get(key)
    if cached and cached['stamp'] == (stamp, count):
        return cached
    docs = []
    for item in KnowledgeItem.objects.filter(tenant=tenant, active=True):
        title_tokens = tokens(item.title + ' ' + item.keywords)
        body_tokens = tokens(item.body)
        tf = Counter(body_tokens)
        for t in title_tokens:                       # titles and keywords count double
            tf[t] += 2
        docs.append({'id': item.pk, 'audience': item.audience, 'tf': tf, 'len': sum(tf.values()),
                     'boost': 1 + 0.08 * max(-5, min(10, item.helpful - item.unhelpful))})
    df = Counter()
    for d in docs:
        df.update(d['tf'].keys())
    avg_len = (sum(d['len'] for d in docs) / len(docs)) if docs else 1
    index = {'stamp': (stamp, count), 'docs': docs, 'df': df, 'n': len(docs), 'avg': avg_len}
    cache.set(key, index, 3600)
    return index


def search(tenant, query, audience='ALL', k=TOP_K):
    """[(KnowledgeItem, score)] best first, among what this audience may see."""
    index = _index(tenant)
    q = tokens(query)
    if not q or not index['docs']:
        return []
    allowed = _visible(audience)
    k1, b = 1.4, 0.75
    scores = []
    for d in index['docs']:
        if d['audience'] not in allowed:
            continue
        s = 0.0
        for term in q:
            f = d['tf'].get(term)
            if not f:
                continue
            idf = math.log(1 + (index['n'] - index['df'][term] + 0.5) / (index['df'][term] + 0.5))
            s += idf * f * (k1 + 1) / (f + k1 * (1 - b + b * d['len'] / index['avg']))
        if s > 0:
            scores.append((d['id'], s * d['boost']))
    scores.sort(key=lambda x: -x[1])
    top = scores[:k]
    items = {i.pk: i for i in KnowledgeItem.objects.filter(pk__in=[i for i, _ in top])}
    return [(items[i], round(s, 3)) for i, s in top if i in items]


# ─────────────────────────────────────────────────────────────────────────────
# Answering
# ─────────────────────────────────────────────────────────────────────────────

def audience_of(user, tenant):
    from users.models import Staff
    if user.is_superuser or Staff.objects.filter(tenant=tenant, user=user).exists():
        return 'STAFF'
    return 'FAMILY'


def _best_sentences(item, query, limit=3):
    """The sentences of an item that share most words with the question."""
    q = set(tokens(query))
    parts = [p.strip() for p in re.split(r'(?<=[.!?])\s+|\n+', item.body) if p.strip()]
    if len(parts) <= limit:
        return item.body.strip()
    scored = sorted(enumerate(parts), key=lambda ip: (-len(q & set(tokens(ip[1]))), ip[0]))[:limit]
    return ' '.join(p for _, p in sorted(scored))


def ask(tenant, user, text):
    text = (text or '').strip()
    if len(text) < 3:
        raise KnowledgeError('Type a question.')
    if len(text) > 500:
        raise KnowledgeError('Keep questions under 500 characters.')
    audience = audience_of(user, tenant)
    hits = search(tenant, text, audience)
    best = hits[0][1] if hits else 0.0
    confident = best >= CONFIDENT
    q = AskedQuestion(tenant=tenant, user=user, audience=audience, text=text, confidence=round(best, 3),
                      sources=[i.pk for i, _ in hits])
    if confident:
        answer, used_ai = _compose(tenant, user, text, hits)
        q.answer, q.used_ai, q.status = answer, used_ai, 'ANSWERED'
        KnowledgeItem.objects.filter(pk__in=[i.pk for i, _ in hits[:2]]).update(uses=models_F('uses') + 1)
    else:
        q.answer = ("I don't know that yet. I've passed your question to the school, and once someone answers it "
                    "I'll know for next time." + (' You may find something useful below.' if hits else ''))
        q.status = 'UNSURE'
        _tell_admins(tenant, text)
    q.save()
    return q, [i for i, _ in hits]


def models_F(name):
    from django.db.models import F
    return F(name)


def _compose(tenant, user, question, hits):
    """An answer from the best passages — written by the AI provider if one is set up, otherwise quoted."""
    from assistant import providers
    passages = [(i, item) for i, (item, _) in enumerate(hits[:TOP_K], 1)]
    if providers.available():
        context = '\n\n'.join(f'[{n}] {item.title}\n{item.body[:1500]}' for n, item in passages)
        system = (f'You are the helpful assistant of {tenant.name}, a school. Answer the question using ONLY the '
                  'numbered school information provided. Be brief, warm and clear. Cite the numbers you used like [1]. '
                  'If the information does not contain the answer, reply exactly: NOT_FOUND')
        try:
            result = providers.complete(tenant, 'school_ai', system, f'School information:\n{context}\n\nQuestion: {question}',
                                        user=user, max_tokens=400, temperature=0.2)
            text = (result.text or '').strip()
            if text and 'NOT_FOUND' not in text:
                return text, True
        except Exception:
            pass                                                   # fall back to quoting
    item = hits[0][0]
    return f'{_best_sentences(item, question)}\n\n— from “{item.title}”', False


def _tell_admins(tenant, text):
    from notifications import services as notes
    notes.to_admins(tenant, 'report', 'The school AI has questions it could not answer',
                    body=f'Latest: "{text[:120]}". Answer them once and it will know for next time.',
                    url='/school-ai/questions/', dedupe=f'schoolai-unsure:{timezone.localdate()}:{tenant.pk}')


def rate(question, user, helpful):
    if question.user_id != user.pk:
        raise KnowledgeError('You can only rate your own questions.')
    if question.rating:
        return question
    question.rating = 1 if helpful else -1
    question.save(update_fields=['rating'])
    field = 'helpful' if helpful else 'unhelpful'
    KnowledgeItem.objects.filter(pk__in=question.sources[:2]).update(**{field: models_F(field) + 1})
    if not helpful and question.status == 'ANSWERED':
        _tell_admins(question.tenant, question.text)
    return question


def teach(question, user, answer, title='', audience=None, keywords=''):
    """An administrator answers a question: the answer becomes knowledge and the asker is told."""
    answer = (answer or '').strip()
    if len(answer) < 3:
        raise KnowledgeError('Write the answer.')
    item = KnowledgeItem.objects.create(
        tenant=question.tenant, title=(title or question.text)[:200], body=answer, source='ANSWER',
        audience=audience or ('STAFF' if question.audience == 'STAFF' else 'ALL'), keywords=keywords[:255],
        created_by=user)
    question.status, question.resolved_by, question.resolved_at = 'RESOLVED', user, timezone.now()
    question.answer = answer
    question.sources = [item.pk]
    question.save(update_fields=['status', 'resolved_by', 'resolved_at', 'answer', 'sources'])
    # Same question asked by others, still waiting: they get the answer too
    others = AskedQuestion.objects.filter(tenant=question.tenant, status='UNSURE', text__iexact=question.text)
    targets = {question.user_id} | set(others.values_list('user_id', flat=True))
    others.update(status='RESOLVED', answer=answer, resolved_by=user, resolved_at=timezone.now())
    from notifications import services as notes
    from users.models import User
    for u in User.objects.filter(pk__in=[t for t in targets if t]):
        notes.to_user(u, 'message', 'The school answered your question', body=f'"{question.text[:100]}" — {answer[:160]}',
                      url='/school-ai/ask/', tenant=question.tenant, admin_feed=False)
    cache.delete(_index_key(question.tenant))
    return item


def suggestions(tenant, audience, n=6):
    """Questions people often ask that the school can answer."""
    common = (AskedQuestion.objects.filter(tenant=tenant, audience=audience, status__in=('ANSWERED', 'RESOLVED'))
              .exclude(rating=-1).values_list('text', flat=True)[:200])
    counts = Counter(t.strip().rstrip('?').capitalize() for t in common)
    out = [f'{t}?' for t, _ in counts.most_common(n)]
    defaults = ['When does the term resume?', 'How much are the school fees?', 'How do I contact the school?',
                'What subjects are offered?', 'When are the exams?', 'What events are coming up?']
    for d in defaults:
        if len(out) >= n:
            break
        if d not in out:
            out.append(d)
    return out
