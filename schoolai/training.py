"""
schoolai/training.py
────────────────────
How the school's models learn, get better, and are used.

Every night (and on request) `learn()`:

  1. records today's snapshot and brings the term-by-term memory up to date;
  2. keeps the knowledge base in step with announcements, the calendar, fees…;
  3. trains a challenger for each model on all the history except the most
     recent term, and tests both the challenger and the active model on that
     most recent term — data neither has learnt from. The challenger replaces
     the active model only if it does better, so the school's models can only
     get better as years of data build up;
  4. refits the winner on everything and predicts for this term's students,
     with the reasons behind each prediction;
  5. tells administrators when students are newly at high risk.

Until a school has enough history (MIN_ROWS student-terms across at least two
terms) the models stay in "collecting" mode and nothing is predicted.
"""
import logging
from collections import defaultdict

import numpy as np
from django.db import transaction
from django.utils import timezone

from users.models import ENROLLED_STATUSES, Student

from . import memory, ml
from .models import FeatureRow, LearningRun, ModelVersion, Prediction

log = logging.getLogger(__name__)
MIN_ROWS = 40
MIN_TERMS = 2
HIGH_RISK, MEDIUM_RISK = 0.6, 0.35


class LearningError(RuntimeError):
    pass


# ─────────────────────────────────────────────────────────────────────────────
# Data
# ─────────────────────────────────────────────────────────────────────────────

def _matrix(rows, keys):
    return np.array([[np.nan if r.features.get(k) is None else float(r.features[k]) for k in keys] for r in rows], float)


def _usable_keys(rows):
    """Features with at least some values and some variation in this school's data."""
    keys = []
    for k in memory.FEATURE_KEYS:
        vals = [r.features.get(k) for r in rows if r.features.get(k) is not None]
        if len(vals) >= max(5, len(rows) // 10) and len(set(round(float(v), 3) for v in vals)) > 1:
            keys.append(k)
    return keys


def training_rows(tenant):
    rows = list(FeatureRow.objects.filter(tenant=tenant).exclude(labels={}).order_by('order', 'id'))
    return rows


def split(rows):
    """Train on all but the latest labelled term; validate on the latest. Falls back to a fixed 80/20 split."""
    orders = sorted({r.order for r in rows})
    if len(orders) >= MIN_TERMS:
        last = orders[-1]
        train = [r for r in rows if r.order != last]
        val = [r for r in rows if r.order == last]
        if len(train) >= 10 and len(val) >= 5:
            return train, val, 'latest term held out'
    rng = np.random.default_rng(7)
    idx = rng.permutation(len(rows))
    cut = max(1, int(len(rows) * 0.8))
    return [rows[i] for i in idx[:cut]], [rows[i] for i in idx[cut:]], 'random 80/20 split'


# ─────────────────────────────────────────────────────────────────────────────
# Fitting and scoring
# ─────────────────────────────────────────────────────────────────────────────

def _fit(kind, rows, keys):
    X = _matrix(rows, keys)
    scaler = ml.Scaler().fit(X)
    Z = scaler.transform(X)
    if kind == 'NEXT_AVG':
        y = np.array([r.labels['next_avg'] for r in rows], float)
        w, b = ml.ridge_fit(Z, y, alpha=max(1.0, len(rows) * 0.01))
    else:
        y = np.array([r.labels['fail'] for r in rows], float)
        if y.sum() == 0 or y.sum() == len(y):
            raise LearningError('Every student in the history had the same outcome — nothing to learn from yet.')
        w, b = ml.logistic_fit(Z, y, l2=1.0)
    return {'keys': keys, 'means': scaler.means.tolist(), 'stds': scaler.stds.tolist(), 'w': np.asarray(w).tolist(),
            'b': b}


def _apply(kind, params, rows):
    keys = params['keys']
    scaler = ml.Scaler(params['means'], params['stds'])
    Z = scaler.transform(_matrix(rows, keys))
    raw = Z @ np.asarray(params['w']) + params['b']
    if kind == 'NEXT_AVG':
        return np.clip(raw, 0, 100), Z
    return ml.logistic_predict(Z, params['w'], params['b']), Z


def _score(kind, params, rows):
    pred, _ = _apply(kind, params, rows)
    if kind == 'NEXT_AVG':
        m = ml.regression_metrics([r.labels['next_avg'] for r in rows], pred)
        base = ml.regression_metrics([r.labels['next_avg'] for r in rows], [r.features['avg'] for r in rows])
        m['baseline_mae'] = base['mae']                         # "same as this term" — what a guess would do
        return m
    return ml.classification_metrics([r.labels['fail'] for r in rows], pred)


def better(kind, new, old):
    if old is None:
        return True
    if kind == 'NEXT_AVG':
        return new['mae'] < old['mae'] - 0.05
    if new.get('auc') is None:
        return False
    if old.get('auc') is None:
        return True
    return new['auc'] > old['auc'] + 0.005 or (new['auc'] >= old['auc'] - 0.005 and new['brier'] < old['brier'] - 0.005)


def active(tenant, kind):
    return ModelVersion.objects.filter(tenant=tenant, kind=kind, status='ACTIVE').first()


def train_kind(tenant, kind, rows):
    """Train a challenger; promote it if it beats the active model on the latest term. Returns the ModelVersion."""
    if len(rows) < MIN_ROWS or len({r.order for r in rows}) < 1:
        raise LearningError(f'Needs at least {MIN_ROWS} student-terms with a following term (has {len(rows)}).')
    keys = _usable_keys(rows)
    if 'avg' not in keys:
        raise LearningError('Not enough results yet.')
    train, val, how = split(rows)
    challenger = _fit(kind, train, keys)
    new_metrics = _score(kind, challenger, val)
    champion = active(tenant, kind)
    old_metrics = None
    if champion:
        try:
            old_metrics = _score(kind, champion.params, val)
        except Exception:
            old_metrics = None
    version = (ModelVersion.objects.filter(tenant=tenant, kind=kind).order_by('-version')
               .values_list('version', flat=True).first() or 0) + 1
    promote = better(kind, new_metrics, old_metrics)
    final = _fit(kind, rows, keys) if promote else challenger     # the winner learns from everything
    with transaction.atomic():
        if promote and champion:
            champion.status = 'RETIRED'
            champion.save(update_fields=['status'])
        mv = ModelVersion.objects.create(
            tenant=tenant, kind=kind, version=version, status='ACTIVE' if promote else 'REJECTED',
            samples=len(rows), terms=len({r.order for r in rows}), params=final,
            metrics={'validation': new_metrics, 'previous': old_metrics, 'tested_on': how,
                     'importance': importance(final)},
            note=('First model' if not champion else
                  f'Better than version {champion.version}' if promote else f'Not better than version {champion.version}'))
    return mv


def importance(params):
    """{feature: share of influence} from the standardised weights."""
    w = np.abs(np.asarray(params['w']))
    total = w.sum() or 1.0
    return {k: round(float(v / total), 3) for k, v in sorted(zip(params['keys'], w), key=lambda kv: -kv[1])}


def train_enrolment(tenant):
    history = memory.enrolment_history(tenant)
    if len(history) < 6:
        raise LearningError('Needs at least six months of enrolment history.')
    xs = np.arange(len(history))
    ys = np.array([c for _, c in history], float)
    slope, intercept, spread = ml.trend(xs, ys)
    ahead = len(history) - 1 + 12
    forecast = max(0.0, slope * ahead + intercept)
    version = (ModelVersion.objects.filter(tenant=tenant, kind='ENROLMENT').order_by('-version')
               .values_list('version', flat=True).first() or 0) + 1
    with transaction.atomic():
        ModelVersion.objects.filter(tenant=tenant, kind='ENROLMENT', status='ACTIVE').update(status='RETIRED')
        return ModelVersion.objects.create(
            tenant=tenant, kind='ENROLMENT', version=version, status='ACTIVE', samples=len(history),
            params={'slope_per_month': round(slope, 3), 'intercept': round(intercept, 2),
                    'history': [[d.isoformat(), c] for d, c in history]},
            metrics={'now': int(ys[-1]), 'in_12_months': round(forecast), 'low': max(0, round(forecast - 2 * spread)),
                     'high': round(forecast + 2 * spread), 'per_year': round(slope * 12, 1)},
            note='Trend of the last {} months'.format(len(history)))


# ─────────────────────────────────────────────────────────────────────────────
# Predicting
# ─────────────────────────────────────────────────────────────────────────────

def _reasons(kind, z_row, row, params):
    """Plain-English reasons, strongest first, pointing the same way as the prediction."""
    out = []
    sign = -1 if kind == 'NEXT_AVG' else 1          # for NEXT_AVG a negative contribution is the worrying one
    for key, contrib in ml.contributions(z_row, params['w'], params['keys'])[:6]:
        value = row.features.get(key)
        if value is None or abs(contrib) < 0.05:
            continue
        label, fmt, _ = memory.FEATURE_INFO[key][1:]
        worse = (contrib * sign) > 0
        out.append({'feature': key, 'label': label, 'value': fmt.format(float(value)),
                    'effect': 'raises concern' if worse else 'helps', 'weight': round(abs(contrib), 2)})
        if len(out) == 3:
            break
    return out


def predict(tenant):
    """Predictions for each enrolled student from their most recent term on record. Returns {kind: count}."""
    latest_order = {}
    for sid, order in FeatureRow.objects.filter(tenant=tenant).values_list('student_id', 'order'):
        latest_order[sid] = max(order, latest_order.get(sid, 0))
    enrolled = set(Student.objects.filter(tenant=tenant, status__in=ENROLLED_STATUSES).values_list('pk', flat=True))
    rows = [r for r in FeatureRow.objects.filter(tenant=tenant).select_related('term')
            if r.student_id in enrolled and r.order == latest_order.get(r.student_id)]
    done = {}
    for kind in ('NEXT_AVG', 'FAIL_RISK'):
        mv = active(tenant, kind)
        if mv is None or not rows:
            continue
        values, Z = _apply(kind, mv.params, rows)
        keep = []
        for row, value, z in zip(rows, values, Z):
            band = ''
            if kind == 'FAIL_RISK':
                band = 'HIGH' if value >= HIGH_RISK else 'MEDIUM' if value >= MEDIUM_RISK else 'LOW'
            Prediction.objects.update_or_create(
                tenant=tenant, student_id=row.student_id, kind=kind,
                defaults={'model': mv, 'term': row.term, 'value': round(float(value), 3), 'band': band,
                          'reasons': _reasons(kind, z, row, mv.params)})
            keep.append(row.student_id)
        Prediction.objects.filter(tenant=tenant, kind=kind).exclude(student_id__in=keep).delete()
        done[kind] = len(keep)
    return done


# ─────────────────────────────────────────────────────────────────────────────
# Maturity
# ─────────────────────────────────────────────────────────────────────────────

LEVELS = [
    ('COLLECTING', 'Collecting', 'Building the school\'s memory. Predictions start once two terms of results are in.'),
    ('LEARNING', 'Learning', 'Models are trained but have little history; treat predictions as early hints.'),
    ('RELIABLE', 'Reliable', 'Tested on terms it had not seen, with a good record. Use predictions to plan support.'),
    ('EXPERT', 'Expert', 'Years of the school\'s own history, consistently accurate. It knows the school well.'),
]


def maturity(tenant):
    stats = memory.memory_stats(tenant)
    risk, avg = active(tenant, 'FAIL_RISK'), active(tenant, 'NEXT_AVG')
    auc = (risk.metrics.get('validation') or {}).get('auc') if risk else None
    mae = (avg.metrics.get('validation') or {}).get('mae') if avg else None
    years = stats['terms'] / 3
    if not (risk or avg):
        level = 0
    elif stats['labelled'] >= 600 and years >= 3 and (auc or 0) >= 0.8:
        level = 3
    elif stats['labelled'] >= 150 and ((auc or 0) >= 0.7 or (mae is not None and mae <= 8)):
        level = 2
    else:
        level = 1
    key, label, text = LEVELS[level]
    # progress within the journey: data (years) and quality
    progress = min(100, round(25 * level + min(25, 25 * min(1, stats['labelled'] / [40, 150, 600, 600][level]))))
    return {'level': level, 'key': key, 'label': label, 'text': text, 'progress': progress, 'stats': stats,
            'auc': auc, 'mae': mae, 'years': round(years, 1), 'levels': LEVELS}


# ─────────────────────────────────────────────────────────────────────────────
# The nightly run
# ─────────────────────────────────────────────────────────────────────────────

def learn(tenant, trigger='SCHEDULE', user=None):
    from . import knowledge
    run = LearningRun.objects.create(tenant=tenant, trigger=trigger, started_by=user if getattr(user, 'pk', None) else None)
    steps = []

    def step(name, fn):
        started = timezone.now()
        try:
            result = fn()
            steps.append({'step': name, 'ok': True, 'result': result,
                          'seconds': round((timezone.now() - started).total_seconds(), 2)})
            return result
        except LearningError as problem:
            steps.append({'step': name, 'ok': None, 'result': str(problem)})
        except Exception as problem:
            log.exception('School AI step %s failed', name)
            steps.append({'step': name, 'ok': False, 'result': str(problem)[:300]})
        return None

    before_high = set(Prediction.objects.filter(tenant=tenant, kind='FAIL_RISK', band='HIGH')
                      .values_list('student_id', flat=True))
    step('Remember today', lambda: str(memory.snapshot(tenant).metrics.get('enrolled', 0)) + ' students on the roll')
    step('Update the term-by-term memory', lambda: f'{memory.collect(tenant)} student-terms')
    step('Learn from school content', lambda: knowledge.sync(tenant))
    rows = training_rows(tenant)
    for kind, label in (('NEXT_AVG', 'Train: next-term average'), ('FAIL_RISK', 'Train: risk of falling below 50%')):
        step(label, lambda kind=kind: _describe(train_kind(tenant, kind, rows)))
    step('Train: enrolment forecast', lambda: _describe(train_enrolment(tenant)))
    step('Predict for this term', lambda: ', '.join(f'{n} {k.replace("_", " ").lower()}' for k, n in predict(tenant).items())
         or 'no active models yet')
    new_high = set(Prediction.objects.filter(tenant=tenant, kind='FAIL_RISK', band='HIGH')
                   .values_list('student_id', flat=True)) - before_high
    if new_high:
        from notifications import services as notes
        notes.to_admins(tenant, 'report', f'School AI: {len(new_high)} student{"s" if len(new_high) != 1 else ""} newly at high risk',
                        body='Students the school\'s model now expects to fall below 50% next term, with the reasons.',
                        url='/school-ai/students/?band=HIGH', level='warning',
                        dedupe=f'schoolai-risk:{timezone.localdate()}')
    run.steps = steps
    run.ok = all(s['ok'] is not False for s in steps)
    run.finished_at = timezone.now()
    run.save()
    return run


def maybe_auto_learn(tenant):
    """Called now and then by the site: learn in the background if the last good run is over a day old."""
    from datetime import timedelta
    from django.conf import settings
    from django.core.cache import cache
    if tenant is None or not getattr(settings, 'SCHOOL_AI_AUTO', True):
        return False
    if not cache.add(f'schoolai:auto:{tenant.pk}', 1, 900):
        return False
    last = LearningRun.objects.filter(tenant=tenant).first()
    if last and (last.started_at > timezone.now() - timedelta(hours=24)
                 or (last.finished_at is None and last.started_at > timezone.now() - timedelta(hours=1))):
        return False
    from lms_project.tasks import run_in_background
    run_in_background(learn, tenant, 'AUTO', tenant=tenant)
    return True


def _describe(mv):
    m = mv.metrics.get('validation') or mv.metrics
    if mv.kind == 'NEXT_AVG':
        detail = f'off by {m.get("mae")} points on average'
    elif mv.kind == 'FAIL_RISK':
        detail = f'AUC {m.get("auc")}'
    else:
        detail = f'{m.get("in_12_months")} students expected in 12 months'
    return f'version {mv.version} — {mv.get_status_display().lower()} ({detail})'
