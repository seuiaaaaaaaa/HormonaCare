def evaluate_sleep(sleep_hours):
    """Return transparent sleep pattern labels for rule-based analysis."""
    if sleep_hours < 6:
        return {
            "title": "Low Sleep Pattern",
            "explanation": "Recent logs show fewer than 6 hours of sleep.",
            "recommendation": "Aim for a more consistent sleep routine and allow enough time for rest.",
            "tone": "warning",
            "status": "low",
        }
    if sleep_hours <= 8:
        return {
            "title": "Normal Sleep Range",
            "explanation": "Recent logs show sleep within a common general wellness range.",
            "recommendation": "Maintain your current sleep routine as consistently as possible.",
            "tone": "success",
            "status": "normal",
        }
    return {
        "title": "Adequate Recovery",
        "explanation": "Recent logs show more than 8 hours of sleep.",
        "recommendation": "Keep balancing rest with your daily schedule and monitor how you feel.",
        "tone": "success",
        "status": "recovery",
    }


def evaluate_stress(stress_level):
    """Return non-diagnostic stress pattern labels based on a 1-10 scale."""
    if stress_level >= 7:
        return {
            "title": "High Stress Pattern",
            "explanation": "Recent logs show stress at the higher end of the recorded range.",
            "recommendation": "Practice stress-reduction techniques such as breathing exercises, stretching, or lighter scheduling.",
            "tone": "warning",
            "status": "high",
        }
    if stress_level >= 4:
        return {
            "title": "Moderate Stress",
            "explanation": "Recent logs show a moderate stress pattern.",
            "recommendation": "Keep using manageable routines that support rest, breaks, and emotional recovery.",
            "tone": "info",
            "status": "moderate",
        }
    return {
        "title": "Low Stress",
        "explanation": "Recent logs show lower reported stress levels.",
        "recommendation": "Continue habits that support calm routines and emotional balance.",
        "tone": "success",
        "status": "low",
    }


def evaluate_hydration(water_intake_liters):
    """Return simple hydration status from general daily intake ranges."""
    if water_intake_liters < 1.5:
        return {
            "title": "Low Hydration",
            "explanation": "Recent logs show water intake below 1.5 liters.",
            "recommendation": "Increase water intake gradually across the day as a general health recommendation.",
            "tone": "warning",
            "status": "low",
        }
    return {
        "title": "Adequate Hydration",
        "explanation": "Recent logs show water intake within a general hydration range.",
        "recommendation": "Keep spacing fluids across the day to maintain hydration consistency.",
        "tone": "success",
        "status": "adequate",
    }


def generate_combined_alerts(sleep_hours, stress_level, water_intake_liters, cycle_logs=None):
    """Build support-oriented alerts from simple combinations of user logs."""
    alerts = []

    if sleep_hours < 6 and stress_level >= 7:
        alerts.append(
            {
                "tone": "warning",
                "title": "Potential Stress-Related Hormonal Risk",
                "description": "Based on recent logs showing low sleep and high stress levels, this pattern may contribute to hormonal imbalance in PCOS.",
                "recommendation": "Improve sleep consistency and practice stress-reduction techniques.",
            }
        )

    if water_intake_liters < 1.5:
        alerts.append(
            {
                "tone": "warning",
                "title": "Potential Low Hydration Pattern",
                "description": "Recent logs show lower water intake than the general hydration range used in this rule-based analysis.",
                "recommendation": "Try increasing fluids gradually and spreading intake across the day.",
            }
        )

    if cycle_logs is not None and not cycle_logs:
        alerts.append(
            {
                "tone": "info",
                "title": "Cycle Pattern Data Needed",
                "description": "Cycle logs are limited, so cycle-related pattern-based insight may be less detailed.",
                "recommendation": "Keep logging cycle changes so the support insights stay grounded in your records.",
            }
        )

    return alerts


def predict_health_score(sleep_hours, water_intake, stress_level, activity_minutes):
    """Return a simple wellness-style score from transparent rules, not ML."""
    score = 100
    if sleep_hours < 6:
        score -= 25
    elif sleep_hours <= 8:
        score -= 5

    if water_intake < 1.5:
        score -= 20
    elif water_intake <= 2.5:
        score -= 5

    if stress_level >= 7:
        score -= 25
    elif stress_level >= 4:
        score -= 10

    if activity_minutes < 20:
        score -= 15
    elif activity_minutes < 30:
        score -= 5

    return max(0, min(100, round(float(score), 1)))


def build_health_recommendations(sleep_hours, water_intake, stress_level, activity_minutes):
    """Return general health recommendations from rule-based analysis."""
    recommendations = []
    sleep_result = evaluate_sleep(sleep_hours)
    stress_result = evaluate_stress(stress_level)
    hydration_result = evaluate_hydration(water_intake)

    if sleep_result["status"] == "low":
        recommendations.append("General health recommendation: improve sleep consistency to support overall wellbeing.")
    if hydration_result["status"] == "low":
        recommendations.append("General health recommendation: increase hydration gradually through the day.")
    if stress_result["status"] == "high":
        recommendations.append("Pattern-based insight: high stress logs may benefit from relaxation routines and pacing.")
    if activity_minutes < 30:
        recommendations.append("General health recommendation: add light to moderate movement when practical.")
    if not recommendations:
        recommendations.append("Pattern-based insight: your recent logs are within generally balanced ranges.")
    return recommendations

import os
from functools import lru_cache
from pathlib import Path

REQUIRED_WELLNESS_COLUMNS = [
    "sleep_duration",
    "sleep_quality",
    "physical_activity",
    "stress_level",
    "heart_rate",
    "daily_steps",
    "wellness_trend",
]

MODEL_FEATURE_COLUMNS = REQUIRED_WELLNESS_COLUMNS[:-1]
DEFAULT_WEEKLY_WELLNESS_TREND = "Stable"
DEFAULT_DATASET_NAME = "weekly_wellness_dataset.csv"
EXPECTED_WELLNESS_TRENDS = {"Declining", "Stable", "Improving"}




MOOD_SCORES = {
    "awful": 0,
    "bad": 1,
    "okay": 2,
    "good": 3,
    "great": 4,
}


ACTIVITY_SCORES = {
    "low activity": 0,
    "recommended activity": 1,
    "high activity": 2,
}


MEAL_SCORES = {
    "high sugar": 0,
    "fast food": 1,
    "balanced": 2,
    "protein-rich": 2,
    "protein rich": 2,
}


def _normalize_sleep_hours(value):
    try:
        return round(float(value or 0), 2)
    except (TypeError, ValueError):
        return 0.0


def _normalize_stress_level(value):
    try:
        stress = int(float(value or 0))
    except (TypeError, ValueError):
        return 3
    return min(10, max(1, stress))


def _encode_mood(mood_label):
    return MOOD_SCORES.get((mood_label or "okay").strip().lower(), 2)


def _encode_activity(activity_level):
    return ACTIVITY_SCORES.get((activity_level or "").strip().lower(), 1)


def _encode_meal(meal_category):
    return MEAL_SCORES.get((meal_category or "").strip().lower(), 1)


def _encode_hydration(hydration_status):
    return 0 if (hydration_status or "").strip().lower() == "low hydration" else 1


def _resolve_dataset_path(dataset_path=None):
    if dataset_path:
        return Path(dataset_path)

    explicit_env_value = os.getenv("WELLNESS_DATASET_PATH")
    explicit_env_path = Path(explicit_env_value).expanduser() if explicit_env_value else None
    candidate_paths = [
        explicit_env_path,
        Path(__file__).resolve().with_name(DEFAULT_DATASET_NAME),
        Path.cwd() / DEFAULT_DATASET_NAME,
    ]
    for candidate in candidate_paths:
        if candidate and candidate.exists():
            return candidate
    return Path(__file__).resolve().with_name(DEFAULT_DATASET_NAME)


def _coerce_float(value, default=None):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _average(values, default_value):
    numeric_values = [value for value in values if value is not None]
    if not numeric_values:
        return float(default_value)
    return round(sum(numeric_values) / len(numeric_values), 2)


def _collect_numeric_values(rows, keys):
    values = []
    for row in rows:
        for key in keys:
            if key in row:
                numeric_value = _coerce_float(row.get(key), default=None)
                if numeric_value is not None:
                    values.append(numeric_value)
                    break
    return values


def _prepare_model_input(data_input, feature_medians):
    """Normalize a single prediction input into the same columns used during training."""
    raw_input = data_input or {}
    return {
        "sleep_duration": _coerce_float(raw_input.get("sleep_duration"), feature_medians["sleep_duration"]),
        "sleep_quality": _coerce_float(raw_input.get("sleep_quality"), feature_medians["sleep_quality"]),
        "physical_activity": _coerce_float(raw_input.get("physical_activity"), feature_medians["physical_activity"]),
        "stress_level": _coerce_float(raw_input.get("stress_level"), feature_medians["stress_level"]),
        "heart_rate": _coerce_float(raw_input.get("heart_rate"), feature_medians["heart_rate"]),
        "daily_steps": _coerce_float(raw_input.get("daily_steps"), feature_medians["daily_steps"]),
    }


@lru_cache(maxsize=4)
def get_weekly_wellness_trend_bundle(dataset_path=None):
    """
    Load the processed CSV dataset, split it into training/testing sets, and train the classifier.

    This is supervised machine learning: the model learns from historical rows in
    ``weekly_wellness_dataset.csv`` instead of using fixed if/else trend rules.
    """
    import pandas as pd
    from sklearn.metrics import accuracy_score
    from sklearn.model_selection import train_test_split
    from sklearn.tree import DecisionTreeClassifier

    resolved_path = _resolve_dataset_path(dataset_path)
    if not resolved_path.exists():
        raise FileNotFoundError(
            f"Weekly wellness dataset not found: {resolved_path}. "
            "Run prepare_weekly_wellness_dataset.py to regenerate it before running predictions."
        )

    if resolved_path.suffix.lower() != ".csv":
        raise ValueError(
            f"Weekly wellness dataset must be a CSV file, but received: {resolved_path.name}."
        )

    dataset = pd.read_csv(resolved_path)
    missing_columns = [column for column in REQUIRED_WELLNESS_COLUMNS if column not in dataset.columns]
    if missing_columns:
        raise ValueError(
            f"{resolved_path.name} is missing required columns: "
            + ", ".join(missing_columns)
        )

    working_frame = dataset[REQUIRED_WELLNESS_COLUMNS].copy()
    for column in MODEL_FEATURE_COLUMNS:
        working_frame[column] = pd.to_numeric(working_frame[column], errors="coerce")

    working_frame["wellness_trend"] = working_frame["wellness_trend"].astype(str).str.strip().str.title()
    working_frame = working_frame[working_frame["wellness_trend"] != ""]
    working_frame = working_frame.dropna(subset=["wellness_trend"])
    unexpected_labels = sorted(set(working_frame["wellness_trend"]) - EXPECTED_WELLNESS_TRENDS)
    if unexpected_labels:
        raise ValueError(
            f"{resolved_path.name} contains unexpected wellness_trend values: "
            + ", ".join(unexpected_labels)
        )

    if len(working_frame) < 5:
        raise ValueError(f"{resolved_path.name} needs at least 5 rows to support an 80/20 train-test split.")

    feature_medians = working_frame[MODEL_FEATURE_COLUMNS].median()
    working_frame[MODEL_FEATURE_COLUMNS] = working_frame[MODEL_FEATURE_COLUMNS].fillna(feature_medians)

    training_features = working_frame[MODEL_FEATURE_COLUMNS]
    training_labels = working_frame["wellness_trend"]
    if training_labels.nunique() < 2:
        raise ValueError(f"{resolved_path.name} must contain at least two wellness trend classes.")

    stratify_labels = training_labels if training_labels.value_counts().min() >= 2 else None
    try:
        train_features, test_features, train_labels, test_labels = train_test_split(
            training_features,
            training_labels,
            test_size=0.2,
            random_state=42,
            stratify=stratify_labels,
        )
    except ValueError:
        train_features, test_features, train_labels, test_labels = train_test_split(
            training_features,
            training_labels,
            test_size=0.2,
            random_state=42,
        )

    model = DecisionTreeClassifier(random_state=42)
    model.fit(train_features, train_labels)
    test_predictions = model.predict(test_features)
    test_accuracy = accuracy_score(test_labels, test_predictions)

    return {
        "model": model,
        "dataset_path": str(resolved_path),
        "feature_medians": feature_medians.to_dict(),
        "test_accuracy": round(float(test_accuracy), 4),
        "training_rows": int(len(train_features)),
        "testing_rows": int(len(test_features)),
    }


@lru_cache(maxsize=1)
def get_weekly_wellness_trend_model():
    """Return the cached decision tree classifier used by the dashboard trend card."""
    return get_weekly_wellness_trend_bundle()["model"]


def build_weekly_prediction_input(daily_rows, dataset_path=None):
    """
    Convert the app's 7-day log rows into the six numeric features used by the dataset.

    Logged values are used whenever the current schema provides them. For features that
    the current app does not yet capture directly (such as heart rate or daily steps),
    the dataset median is used as a simple fallback so the ML pipeline can still run.
    """
    rows = list(daily_rows or [])
    feature_medians = get_weekly_wellness_trend_bundle(dataset_path)["feature_medians"]

    return {
        "sleep_duration": _average(
            _collect_numeric_values(rows, ["sleep_duration", "sleep_hours"]),
            feature_medians["sleep_duration"],
        ),
        "sleep_quality": _average(
            _collect_numeric_values(rows, ["sleep_quality"]),
            feature_medians["sleep_quality"],
        ),
        "physical_activity": _average(
            _collect_numeric_values(rows, ["physical_activity", "exercise_minutes"]),
            feature_medians["physical_activity"],
        ),
        "stress_level": _average(
            _collect_numeric_values(rows, ["stress_level"]),
            feature_medians["stress_level"],
        ),
        "heart_rate": _average(
            _collect_numeric_values(rows, ["heart_rate"]),
            feature_medians["heart_rate"],
        ),
        "daily_steps": _average(
            _collect_numeric_values(rows, ["daily_steps"]),
            feature_medians["daily_steps"],
        ),
    }


def predict_wellness(data_input, dataset_path=None):
    """
    Predict the weekly wellness trend using supervised machine learning.

    Expected keys in ``data_input``:
    - sleep_duration
    - sleep_quality
    - physical_activity
    - stress_level
    - heart_rate
    - daily_steps

    The prediction is informational only and does not diagnose medical conditions.
    """
    import pandas as pd

    bundle = get_weekly_wellness_trend_bundle(dataset_path)
    model_input = _prepare_model_input(data_input, bundle["feature_medians"])
    prediction_frame = pd.DataFrame([model_input], columns=MODEL_FEATURE_COLUMNS)
    prediction = bundle["model"].predict(prediction_frame)[0]
    return str(prediction)


def predict_weekly_wellness_trend(daily_rows):
    """
    Predict the dashboard's weekly wellness trend from user logs using ``model.predict()``.

    The app first summarizes the last 7 days of user logs into the same input columns
    used by the CSV dataset, then lets the trained decision tree produce the trend.
    """
    prediction_input = build_weekly_prediction_input(daily_rows)
    predicted_label = predict_wellness(prediction_input)
    if not predicted_label:
        return DEFAULT_WEEKLY_WELLNESS_TREND
    return predicted_label


def identify_weekly_trend_factors(daily_rows, predicted_label=None):
    """Highlight the 7-day pattern shifts that likely shaped the trend result."""
    rows = list(daily_rows or [])
    if not rows:
        return []

    first_window = rows[:3]
    last_window = rows[-3:]

    def average(window, key, converter=float):
        values = [converter(row.get(key)) for row in window]
        return sum(values) / len(values) if values else 0.0

    mood_delta = average(last_window, "mood", _encode_mood) - average(first_window, "mood", _encode_mood)
    stress_delta = average(first_window, "stress_level", _normalize_stress_level) - average(last_window, "stress_level", _normalize_stress_level)
    sleep_delta = average(last_window, "sleep_hours", _normalize_sleep_hours) - average(first_window, "sleep_hours", _normalize_sleep_hours)
    food_delta = average(last_window, "food_classification", _encode_meal) - average(first_window, "food_classification", _encode_meal)
    activity_delta = average(last_window, "exercise_summary", _encode_activity) - average(first_window, "exercise_summary", _encode_activity)
    hydration_delta = average(last_window, "hydration_status", _encode_hydration) - average(first_window, "hydration_status", _encode_hydration)

    factors = []

    if stress_delta <= -0.45:
        factors.append("Higher stress levels lowered stability")
    elif stress_delta >= 0.45:
        factors.append("Lower stress levels supported balance")

    if activity_delta >= 0.35:
        factors.append("Moderate activity supported balance")
    elif activity_delta <= -0.35:
        factors.append("Lower activity reduced consistency")

    if hydration_delta >= 0.25:
        factors.append("Hydration consistency improved trend")
    elif hydration_delta <= -0.25:
        factors.append("Lower hydration weakened the weekly pattern")

    if sleep_delta >= 0.45:
        factors.append("Sleep consistency reinforced the trend")
    elif sleep_delta <= -0.45:
        factors.append("Sleep inconsistency reduced stability")

    if food_delta >= 0.35:
        factors.append("Balanced meals supported the result")
    elif food_delta <= -0.35:
        factors.append("Less balanced meals softened the trend")

    if mood_delta >= 0.45:
        factors.append("Improved mood patterns supported balance")
    elif mood_delta <= -0.45:
        factors.append("Lower mood patterns affected stability")

    if not factors and predicted_label == "Stable":
        factors.append("Recent routines kept the weekly trend steady")
    elif not factors:
        factors.append("Recent routines shaped the weekly trend")

    return factors[:3]


def build_weekly_trend_guidance(predicted_label):
    """Return a short, non-clinical recommendation for the weekly trend card."""
    if predicted_label == "Improving":
        return "Continue consistent sleep, hydration, and balanced daily habits to support your positive wellness trend."
    if predicted_label == "Declining":
        return "Focus on consistent sleep, hydration, and balanced daily habits to help strengthen your wellness trend this week."
    return "Your wellness is stable. Maintain consistent sleep, hydration, and activity to sustain this trend."
