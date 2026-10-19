"""
schoolai/ml.py
──────────────
The learning algorithms, in plain numpy so they run on the school's own server
with no data sent anywhere:

  • ridge regression (closed form) — for numbers, e.g. next term's average;
  • logistic regression (gradient descent, L2, class-balanced) — for yes/no
    risks, e.g. falling below the pass mark;
  • a linear trend with a prediction interval — for enrolment.

Features are standardised; missing values take the training mean. Every
model can explain a prediction: each feature's contribution is its weight
times how far the student is from the average, so the reasons shown to staff
are the model's real reasons.
"""
import math

import numpy as np


class Scaler:
    def __init__(self, means=None, stds=None):
        self.means = None if means is None else np.asarray(means, float)
        self.stds = None if stds is None else np.asarray(stds, float)

    def fit(self, X):
        X = np.asarray(X, float)
        self.means = np.nanmean(np.where(np.isnan(X), np.nan, X), axis=0)
        self.means = np.where(np.isnan(self.means), 0.0, self.means)
        filled = np.where(np.isnan(X), self.means, X)
        self.stds = filled.std(axis=0)
        self.stds = np.where(self.stds < 1e-9, 1.0, self.stds)
        return self

    def transform(self, X):
        X = np.asarray(X, float)
        X = np.where(np.isnan(X), self.means, X)
        return (X - self.means) / self.stds


def ridge_fit(Z, y, alpha=1.0):
    """Ridge regression on standardised Z. Returns (weights, intercept)."""
    y = np.asarray(y, float)
    b = y.mean()
    n_features = Z.shape[1]
    w = np.linalg.solve(Z.T @ Z + alpha * np.eye(n_features), Z.T @ (y - b))
    return w, float(b)


def _sigmoid(t):
    return 1.0 / (1.0 + np.exp(-np.clip(t, -30, 30)))


def logistic_fit(Z, y, l2=1.0, iters=3000, lr=0.2):
    """Class-balanced logistic regression by gradient descent. Returns (weights, intercept)."""
    y = np.asarray(y, float)
    n, d = Z.shape
    pos = max(y.sum(), 1.0)
    neg = max(n - y.sum(), 1.0)
    sample_w = np.where(y == 1, n / (2 * pos), n / (2 * neg))
    w = np.zeros(d)
    b = math.log(pos / neg)
    for _ in range(iters):
        p = _sigmoid(Z @ w + b)
        err = (p - y) * sample_w
        grad_w = Z.T @ err / n + l2 * w / n
        grad_b = err.mean()
        w -= lr * grad_w
        b -= lr * grad_b
    return w, float(b)


def logistic_predict(Z, w, b):
    return _sigmoid(Z @ np.asarray(w) + b)


# ── Metrics ──────────────────────────────────────────────────────────────────

def regression_metrics(y, pred):
    y, pred = np.asarray(y, float), np.asarray(pred, float)
    err = pred - y
    ss_res = float((err ** 2).sum())
    ss_tot = float(((y - y.mean()) ** 2).sum()) or 1e-9
    return {'mae': round(float(np.abs(err).mean()), 2), 'rmse': round(math.sqrt(ss_res / len(y)), 2),
            'r2': round(1 - ss_res / ss_tot, 3), 'n': int(len(y))}


def auc(y, scores):
    """Area under the ROC curve (the chance a random 'yes' scores above a random 'no')."""
    y, scores = np.asarray(y), np.asarray(scores, float)
    pos, neg = scores[y == 1], scores[y == 0]
    if not len(pos) or not len(neg):
        return None
    order = scores.argsort()
    ranks = np.empty(len(scores))
    ranks[order] = np.arange(1, len(scores) + 1)
    for v in np.unique(scores):                       # average ranks of ties
        idx = scores == v
        ranks[idx] = ranks[idx].mean()
    return round(float((ranks[y == 1].sum() - len(pos) * (len(pos) + 1) / 2) / (len(pos) * len(neg))), 3)


def classification_metrics(y, p, threshold=0.5):
    y, p = np.asarray(y, float), np.asarray(p, float)
    hits = (p >= threshold) == (y == 1)
    tp = float(((p >= threshold) & (y == 1)).sum())
    return {'auc': auc(y, p), 'accuracy': round(float(hits.mean()), 3),
            'recall': round(tp / max(float(y.sum()), 1.0), 3),
            'brier': round(float(((p - y) ** 2).mean()), 3), 'n': int(len(y)), 'positives': int(y.sum())}


# ── Explanations ─────────────────────────────────────────────────────────────

def contributions(z_row, w, names):
    """[(name, contribution)] sorted by size: weight × standardised value."""
    parts = [(n, float(wi * zi)) for n, wi, zi in zip(names, w, z_row)]
    return sorted(parts, key=lambda kv: -abs(kv[1]))


# ── Trend ────────────────────────────────────────────────────────────────────

def trend(xs, ys):
    """Least-squares line through (x, y); returns slope, intercept and the residual spread."""
    xs, ys = np.asarray(xs, float), np.asarray(ys, float)
    if len(xs) < 2:
        return 0.0, float(ys[0]) if len(ys) else 0.0, 0.0
    slope, intercept = np.polyfit(xs, ys, 1)
    resid = ys - (slope * xs + intercept)
    spread = float(resid.std(ddof=1)) if len(xs) > 2 else 0.0
    return float(slope), float(intercept), spread
