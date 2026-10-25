"""
dashboard/menu.py
─────────────────
What each kind of staff sees: the school's administrators choose, per role
(teachers, account officers, librarians, proprietors), which sidebar sections
and links show, and which cards appear on their dashboard.

The list of sections and links is read from the sidebar template itself, so a
link added to the sidebar shows up on the settings page without more work.
A hidden section is also closed: its pages send the person back to their
dashboard, so hiding is not just cosmetic. Administrators always see everything.
"""
import re
from functools import lru_cache
from types import SimpleNamespace

from django.contrib import messages
from django.http import JsonResponse
from django.shortcuts import redirect
from django.template import Context, Template, TemplateSyntaxError
from django.template.loader import get_template
from django.urls import Resolver404, resolve

ROLES = [('TEACHER', 'Teachers'), ('ACCOUNT', 'Account officers'), ('LIB', 'Librarians'),
         ('PROPRIETOR', 'Proprietors')]

# Which dashboard each role lands on (see dashboard.views.dashboard_router)
DASHBOARD_OF = {'TEACHER': 'teacher', 'ACCOUNT': 'admin', 'LIB': 'admin', 'PROPRIETOR': 'proprietor'}

CARDS = {
    'teacher': [('kpis', 'Numbers at the top'), ('today', "Today's timetable"), ('marking', 'Marking & tasks'),
                ('classes', 'My classes today'), ('form', 'My form class — who needs me'), ('exams', 'My exams'),
                ('homework', 'Homework I set'), ('events', 'Coming up')],
    'admin': [('kpis', 'Numbers at the top'), ('today', 'Today (attendance and lessons)'),
              ('waiting', 'Waiting for you'), ('results', 'Results'), ('support', 'Students to support'),
              ('fees', 'Fees this term'), ('events', 'Coming up'), ('targets', 'School targets')],
    'proprietor': [('kpis', 'Numbers at the top'), ('money', 'Money this term'), ('debts', 'How long debts have waited'),
                   ('enrolment', 'Enrolment'), ('ai', 'School intelligence'), ('sections', 'Sections side by side'),
                   ('targets', 'Targets'), ('fee_classes', 'Fee collection by class'), ('results', 'Results by class')],
}

# Hidden until an administrator says otherwise
DEFAULT_HIDDEN_GROUPS = {'TEACHER': ['grp-admissions', 'grp-idcards-safety'], 'LIB': ['grp-admissions']}

# Links that can never be hidden or closed
LOCKED = {'dashboard:router'}
ALWAYS_OPEN_NAMESPACES = {'dashboard'}
ALWAYS_OPEN = {'users:change_password', 'users:logout', 'security:verify', 'security:setup', 'security:me',
               'security:backup_codes', 'idcards:my_id_card'}

PORTAL_GROUPS = {'grp-student', 'grp-parent'}        # the student and parent menus, not staff


# ─────────────────────────────────────────────────────────────────────────────
# Reading the sidebar
# ─────────────────────────────────────────────────────────────────────────────

class _Staff(SimpleNamespace):
    def __getattr__(self, name):                     # permission switches (can_…) may be on for anyone
        if name.startswith('can_'):
            return True
        raise AttributeError(name)


def _person(role):
    return SimpleNamespace(is_superuser=False, is_staff_user=True, is_student=False, is_parent=False,
                           is_authenticated=True, staff=_Staff(role=role))


def _holds(cond, role):
    """Would this {% if %} condition be true for a member of staff with this role?"""
    if 'menu_hidden' in cond or not re.search(r'\buser\b(?![s:])', cond):
        return True                                  # about the data or the menu settings, not the person
    try:
        return Template('{% if ' + cond + ' %}1{% endif %}').render(Context({'user': _person(role)})) == '1'
    except (TemplateSyntaxError, Exception):
        return True


_TAG = re.compile(r"\{%\s*(if|elif|else|endif)\b\s*(.*?)\s*%\}"
                  r"|<div class=\"nav-group\" id=\"(grp-[\w-]+)\">"
                  r"|<a href=\"\{% url '([\w:]+)'[^>]*class=\"nav-item\"(.*?)</a>", re.S)


@lru_cache(maxsize=1)
def sidebar():
    """[{'id', 'label', 'items': [{'name', 'label', 'roles'}]}] in sidebar order, staff menus only.
    'roles' are the roles that can see the link at all (some links are for administrators only)."""
    source = get_template('dashboard/base.html').template.source
    groups, stack, current = [], [], None        # stack: one list of (condition, wanted) per open {% if %}
    labels = dict(re.findall(r'id="(grp-[\w-]+)">\s*<div class="nav-section-hdr".*?class="sec-label">([^<]+)<',
                             source, re.S))
    for m in _TAG.finditer(source):
        tag, cond, gid, name, body = m.groups()
        if tag == 'if':
            stack.append([(cond, True)])
        elif tag == 'elif' and stack:
            stack[-1] = [(c, False) for c, _ in stack[-1]] + [(cond, True)]
        elif tag == 'else' and stack:
            stack[-1] = [(c, False) for c, _ in stack[-1]]
        elif tag == 'endif' and stack:
            stack.pop()
        elif gid:
            current = {'id': gid, 'label': _clean(labels.get(gid, gid)), 'items': []}
            if gid not in PORTAL_GROUPS:
                groups.append(current)
        elif name and current is not None:
            label = re.search(r'class="ni-label">([^<]+)<', body)
            conds = [c for level in stack for c in level]
            roles = [r for r, _ in ROLES if all(_holds(c, r) == wanted for c, wanted in conds)]
            current['items'].append({'name': name, 'label': _clean(label.group(1) if label else name), 'roles': roles})
    return groups


def _clean(text):
    return re.sub(r'\s+', ' ', text.replace('&amp;', '&')).strip()


def sections_for(role):
    """The sections (and links) this role can see at all — what the settings page offers to hide."""
    from dashboard.access import ADMIN_ONLY
    out = []
    for g in sidebar():
        items = [i for i in g['items'] if role in i['roles'] and i['name'] not in LOCKED and i['name'] not in ADMIN_ONLY]
        if items:
            out.append({'id': g['id'], 'label': g['label'], 'items': items})
    return out


@lru_cache(maxsize=1)
def _owned_namespaces():
    """{namespace: group id} for namespaces whose staff links all sit in one section,
    e.g. every 'admissions:' link is under Admissions."""
    where = {}
    for g in sidebar():
        for i in g['items']:
            where.setdefault(i['name'].split(':')[0], set()).add(g['id'])
    return {ns: next(iter(gs)) for ns, gs in where.items() if len(gs) == 1 and ns not in ALWAYS_OPEN_NAMESPACES}


# ─────────────────────────────────────────────────────────────────────────────
# The school's choices
# ─────────────────────────────────────────────────────────────────────────────

def default_hidden(role):
    hidden = set()
    for g in sections_for(role):
        if g['id'] in DEFAULT_HIDDEN_GROUPS.get(role, []):
            hidden.update(i['name'] for i in g['items'])
    return hidden


def person_rule(tenant, staff):
    """A person's own settings, or None when they follow their role's."""
    from security.models import MenuRule
    if tenant is None or staff is None:
        return None
    return MenuRule._default_manager.filter(tenant=tenant, staff=staff).first()


def saved_hidden(tenant, role, staff=None):
    """What is hidden: the person's own settings if they have some, else their role's (or the recommended)."""
    from security.models import MenuRule
    own = person_rule(tenant, staff)
    if own:
        return set(own.hidden)
    rule = MenuRule._default_manager.filter(tenant=tenant, role=role, staff__isnull=True).first() if tenant else None
    return set(rule.hidden) if rule else default_hidden(role)


def save(tenant, role, hidden, user=None, staff=None):
    """Store a role's choices, or one person's (staff given), keeping only names that exist."""
    from security.models import MenuRule
    allowed = {i['name'] for g in sections_for(role) for i in g['items']}
    dash = DASHBOARD_OF.get(role)
    allowed |= {f'{dash}:{k}' for k, _ in CARDS.get(dash, [])}
    clean = sorted(h for h in set(hidden) if h in allowed)
    if staff is not None:
        MenuRule._default_manager.update_or_create(tenant=tenant, staff=staff,
                                                   defaults={'role': role, 'hidden': clean, 'updated_by': user})
    else:
        MenuRule._default_manager.update_or_create(tenant=tenant, role=role, staff=None,
                                                   defaults={'hidden': clean, 'updated_by': user})
    return clean


def expand(role, hidden):
    """Add the ids of sections whose every link is hidden (so the whole section disappears)."""
    out = set(hidden)
    for g in sections_for(role):
        if all(i['name'] in out for i in g['items']):
            out.add(g['id'])
    return out


def restricted_role(user, tenant):
    """The role whose menu is limited, or None (administrators, students, parents, platform staff)."""
    if not getattr(user, 'is_authenticated', False) or user.is_superuser or tenant is None:
        return None
    staff = getattr(user, 'staff', None)
    if staff is None or staff.role not in dict(ROLES):
        return None
    return staff.role


def hidden_for(request):
    """What the school has hidden from the signed-in person's role (these pages are closed too)."""
    if not hasattr(request, '_menu_hidden'):
        tenant = getattr(request, 'tenant', None)
        role = restricted_role(getattr(request, 'user', None), tenant)
        request._menu_hidden = expand(role, saved_hidden(tenant, role, request.user.staff)) if role else frozenset()
        request._menu_role = role
    return request._menu_hidden


def not_shown(request):
    """Everything left out of the person's sidebar: what the school hid, plus links they could only be
    turned away from (admin-only pages, a permission switch they do not have, a job they do not do)."""
    if not hasattr(request, '_menu_not_shown'):
        hidden = hidden_for(request)
        role = request._menu_role
        if role:
            from dashboard.access import closed_to
            user, tenant = request.user, request.tenant
            hidden = expand(role, set(hidden) | closed_to(user, tenant, user.staff))
        request._menu_not_shown = hidden
    return request._menu_not_shown


def blocked(role, hidden, view_name):
    """Is this page closed to the role? Hidden links are closed, and so is any page of a
    section that is hidden completely (e.g. every admissions page when Admissions is hidden)."""
    if not role or not view_name or view_name in ALWAYS_OPEN:
        return False
    ns = view_name.split(':')[0]
    if ns in ALWAYS_OPEN_NAMESPACES:
        return False
    visible = {i['name'] for g in sections_for(role) for i in g['items'] if i['name'] not in hidden}
    if view_name in visible:
        return False
    if view_name in hidden:
        return True
    group = _owned_namespaces().get(ns)
    return bool(group) and group in hidden


# ─────────────────────────────────────────────────────────────────────────────
# Wiring
# ─────────────────────────────────────────────────────────────────────────────

def context(request):
    return {'menu_hidden': not_shown(request)}


class MenuAccessMiddleware:
    """Closes the pages of hidden sections and links for the roles they are hidden from."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        hidden = hidden_for(request)
        if hidden:
            try:
                view_name = resolve(request.path_info).view_name
            except Resolver404:
                view_name = None
            if blocked(request._menu_role, hidden, view_name):
                text = 'The school has not given your role access to that page.'
                if request.headers.get('x-requested-with') == 'XMLHttpRequest' or \
                        'application/json' in request.headers.get('Accept', ''):
                    return JsonResponse({'ok': False, 'message': text}, status=403)
                messages.error(request, text)
                return redirect('dashboard:router')
        return self.get_response(request)
