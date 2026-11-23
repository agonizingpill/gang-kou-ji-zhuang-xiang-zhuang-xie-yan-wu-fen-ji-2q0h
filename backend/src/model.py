"""Shared delay-severity model.

The API and the Redis worker both import this module so a re-hang trial and a
regular delay classification always use exactly the same feature definition and
the same fitted pipeline (fixed seed, fixed training rows).
"""

from __future__ import annotations

import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

MODEL_VERSION = "logreg-seed42-v1"

FEATURE_KEYS = [
    "container_count",
    "avg_gate_dwell_min",
    "max_gate_dwell_min",
    "reefer_ratio",
    "hazmat_ratio",
    "customs_open_count",
    "gate_event_count",
    "dry_count",
    "reefer_count",
    "hazmat_count",
    "port_call_hour",
    "window_minutes",
    "dwell_x_reefer",
    "gate_x_count",
]

# A shipment is treated as severely delayed when the model puts its highest
# weight on major/critical.  The probability is the sum of those two classes.
SEVERE_LEVELS = ("major", "critical")

_TRAINING_ROWS = [
    ([4, 8, 12, .0, .0, 0, 4, 4, 0, 0, 6, 480], "normal"),
    ([6, 16, 22, .17, .0, 0, 6, 5, 1, 0, 8, 420], "watch"),
    ([8, 28, 40, .25, .12, 0, 8, 5, 2, 1, 10, 360], "major"),
    ([9, 42, 66, .33, .22, 1, 9, 4, 3, 2, 13, 300], "critical"),
    ([5, 10, 18, .2, .0, 0, 5, 4, 1, 0, 7, 520], "normal"),
    ([7, 22, 31, .29, .0, 0, 7, 4, 2, 0, 9, 390], "watch"),
    ([10, 35, 51, .3, .1, 0, 10, 6, 3, 1, 11, 330], "major"),
    ([12, 50, 78, .42, .17, 0, 12, 6, 5, 2, 14, 280], "critical"),
    ([3, 7, 10, .0, .0, 0, 3, 3, 0, 0, 5, 600], "normal"),
    ([8, 25, 36, .38, .0, 0, 8, 5, 3, 0, 12, 360], "major"),
    ([5, 18, 24, .2, .2, 0, 5, 3, 1, 1, 9, 440], "watch"),
    ([11, 45, 70, .45, .25, 0, 11, 4, 5, 2, 15, 260], "critical"),
]


def train_model():
    x = []
    y = []
    for base, label in _TRAINING_ROWS:
        features = dict(zip(FEATURE_KEYS[:12], base))
        features["dwell_x_reefer"] = features["avg_gate_dwell_min"] * features["reefer_ratio"]
        features["gate_x_count"] = features["gate_event_count"] * features["container_count"]
        x.append([features[key] for key in FEATURE_KEYS])
        y.append(label)
    model = Pipeline([
        ("scale", StandardScaler()),
        ("lr", LogisticRegression(max_iter=500, random_state=42, multi_class="auto")),
    ])
    model.fit(x, y)
    return model


MODEL = train_model()


def prepare_features(features: dict) -> dict:
    """Add the two manual interaction features and coerce values to float."""
    prepared = {key: float(features.get(key, 0) or 0) for key in FEATURE_KEYS[:12]}
    prepared["dwell_x_reefer"] = round(
        prepared["avg_gate_dwell_min"] * prepared["reefer_ratio"], 3
    )
    prepared["gate_x_count"] = prepared["gate_event_count"] * prepared["container_count"]
    return prepared


def vector(features: dict) -> np.ndarray:
    prepared = prepare_features(features)
    return np.array([[prepared[key] for key in FEATURE_KEYS]], dtype=float)


def severe_probability(probabilities: dict) -> float:
    return round(sum(float(probabilities.get(level, 0.0)) for level in SEVERE_LEVELS), 6)


def predict(features: dict) -> dict:
    prepared = prepare_features(features)
    row = np.array([[prepared[key] for key in FEATURE_KEYS]], dtype=float)
    label = str(MODEL.predict(row)[0])
    raw = MODEL.predict_proba(row)[0]
    classes = MODEL.named_steps["lr"].classes_
    probabilities = {str(name): round(float(prob), 4) for name, prob in zip(classes, raw)}
    raw_probabilities = {str(name): float(prob) for name, prob in zip(classes, raw)}
    severe = round(
        sum(raw_probabilities.get(level, 0.0) for level in SEVERE_LEVELS), 6
    )
    return {
        "label": label,
        "confidence": round(float(max(raw)), 4),
        "probabilities": probabilities,
        "severe_probability": severe,
        "severe_levels": list(SEVERE_LEVELS),
        "feature_keys": FEATURE_KEYS,
        "features": prepared,
        "model_version": MODEL_VERSION,
        "formula": (
            "手工交叉特征 = avg_gate_dwell_min×reefer_ratio, "
            "gate_event_count×container_count；StandardScaler + "
            "LogisticRegression(random_state=42)；严重延误概率 = P(major)+P(critical)"
        ),
    }
