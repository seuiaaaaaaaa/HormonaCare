from ml_model import (
    build_health_recommendations,
    build_weekly_prediction_input,
    build_weekly_trend_guidance,
    evaluate_hydration,
    evaluate_sleep,
    evaluate_stress,
    generate_combined_alerts,
    get_weekly_wellness_trend_bundle,
    identify_weekly_trend_factors,
    predict_health_score,
    predict_wellness,
)

MIN_LOGGED_DAYS_FOR_WEEKLY_WELLNESS_PREDICTION = 4


def _clamp(value, minimum, maximum):
    return max(minimum, min(maximum, value))


def _coerce_number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _extract_first_number(row, keys):
    row = row or {}
    for key in keys:
        numeric_value = _coerce_number(row.get(key))
        if numeric_value is not None:
            return numeric_value
    return None


def _average(values):
    if not values:
        return None
    return round(sum(values) / len(values), 2)


def _normalize_metric(value, divisor):
    if value is None:
        return 0.0
    return _clamp(value / divisor, 0.0, 1.0)


def _calculate_daily_wellness_score(row):
    sleep_score = _normalize_metric(_extract_first_number(row, ["sleep_hours", "sleep_duration"]), 8.0)
    exercise_score = _normalize_metric(_extract_first_number(row, ["physical_activity", "exercise_minutes"]), 60.0)
    hydration_score = _normalize_metric(_extract_first_number(row, ["water_intake"]), 2.0)
    stress_score = _normalize_metric(_extract_first_number(row, ["stress_level"]), 10.0)

    return (
        (sleep_score * 0.30)
        + (exercise_score * 0.25)
        + (hydration_score * 0.20)
        - (stress_score * 0.25)
    )


def _summarize_week(rows):
    rows = list(rows or [])
    sleep_values = []
    hydration_values = []
    exercise_values = []
    stress_values = []
    logged_days = 0

    for row in rows:
        if row.get("has_user_data") is False:
            continue

        sleep_value = _extract_first_number(row, ["sleep_hours", "sleep_duration"])
        hydration_value = _extract_first_number(row, ["water_intake"])
        exercise_value = _extract_first_number(row, ["physical_activity", "exercise_minutes"])
        stress_value = _extract_first_number(row, ["stress_level"])

        metric_values = [sleep_value, hydration_value, exercise_value, stress_value]
        if any(value is not None for value in metric_values):
            logged_days += 1

        if sleep_value is not None:
            sleep_values.append(sleep_value)
        if hydration_value is not None:
            hydration_values.append(hydration_value)
        if exercise_value is not None:
            exercise_values.append(exercise_value)
        if stress_value is not None:
            stress_values.append(stress_value)

    average_sleep = _average(sleep_values)
    average_hydration = _average(hydration_values)
    average_exercise = _average(exercise_values)
    average_stress = _average(stress_values)

    sleep_score = _normalize_metric(average_sleep, 8.0)
    exercise_score = _normalize_metric(average_exercise, 60.0)
    hydration_score = _normalize_metric(average_hydration, 2.0)
    stress_score = _normalize_metric(average_stress, 10.0)
    wellness_score = (
        (sleep_score * 0.30)
        + (exercise_score * 0.25)
        + (hydration_score * 0.20)
        - (stress_score * 0.25)
    )

    return {
        "sleep_score": sleep_score,
        "exercise_score": exercise_score,
        "hydration_score": hydration_score,
        "stress_score": stress_score,
        "wellness_score": round(wellness_score, 4),
        "logged_days": logged_days,
        "sleep_logs": len(sleep_values),
        "exercise_logs": len(exercise_values),
        "hydration_logs": len(hydration_values),
        "stress_logs": len(stress_values),
    }


def _predict_weekly_label(current_week_score, previous_week_score):
    if current_week_score > previous_week_score + 0.05:
        return "Improving"
    if current_week_score < previous_week_score - 0.05:
        return "Declining"
    return "Stable"


def _build_weekly_highlights(summary):
    highlights = []
    if summary["stress_logs"] and summary["stress_score"] > 0.6:
        highlights.append("Higher stress may be adding pressure to hormonal balance")
    if summary["hydration_logs"] and summary["hydration_score"] < 0.5:
        highlights.append("Lower hydration may be adding to fatigue or routine inconsistency")
    if summary["sleep_logs"] and summary["sleep_score"] >= 0.8:
        highlights.append("More consistent sleep supported hormonal rhythm")
    if summary["exercise_logs"] and summary["exercise_score"] >= 0.5:
        highlights.append("Regular movement supported blood sugar and routine balance")
    return highlights


def _build_weekly_recommendation(predicted_label):
    if predicted_label == "Improving":
        return "Your recent routine is moving in a supportive direction for PCOS management. Keep those habits steady."
    if predicted_label == "Declining":
        return "Recent logs suggest your PCOS support routine may need attention. Focus on sleep, meals, movement, and stress recovery."
    return "Your routine looks steady right now. Maintain consistent sleep, hydration, and activity to support PCOS balance."


def build_weekly_confidence_label(logged_days):
    """Estimate user-facing confidence from the number of recent logged days."""
    if logged_days >= 6:
        return "High"
    if logged_days >= 4:
        return "Medium"
    return "Low"


def build_weekly_chart_points(daily_rows):
    """Create a compact 7-day visual signal for the dashboard card."""
    rows = list(daily_rows or [])[-7:]
    fallback_labels = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    fallback_values = [56, 60, 54, 58, 62, 59, 57]

    if not rows:
        return [
            {"label": label, "height": fallback_values[index]}
            for index, label in enumerate(fallback_labels)
        ]

    raw_values = []
    labels = []
    for index, row in enumerate(rows):
        row_date = row.get("date")
        label = row_date.strftime("%a") if hasattr(row_date, "strftime") else fallback_labels[index]
        raw_values.append(_calculate_daily_wellness_score(row) if row.get("has_user_data", True) else None)
        labels.append(label)

    chart_points = []
    for index, value in enumerate(raw_values):
        if value is None:
            height = 18
        else:
            normalized = _clamp((value + 0.25) / 1.0, 0.0, 1.0)
            height = round(30 + (normalized * 40))
        chart_points.append({"label": labels[index], "height": height})

    return chart_points


def build_health_assessment(sleep_hours, water_intake, stress_level, activity_minutes, cycle_logs=None):
    return {
        "inputs": {
            "sleep_hours": sleep_hours,
            "water_intake": water_intake,
            "stress_level": stress_level,
            "activity_minutes": activity_minutes,
        },
        "sleep_evaluation": evaluate_sleep(sleep_hours),
        "stress_evaluation": evaluate_stress(stress_level),
        "hydration_evaluation": evaluate_hydration(water_intake),
        "combined_alerts": generate_combined_alerts(
            sleep_hours=sleep_hours,
            stress_level=stress_level,
            water_intake_liters=water_intake,
            cycle_logs=cycle_logs,
        ),
        "score": predict_health_score(
            sleep_hours=sleep_hours,
            water_intake=water_intake,
            stress_level=stress_level,
            activity_minutes=activity_minutes,
        ),
        "recommendations": build_health_recommendations(
            sleep_hours=sleep_hours,
            water_intake=water_intake,
            stress_level=stress_level,
            activity_minutes=activity_minutes,
        ),
        "model_source": "rule_based_analysis",
    }


def build_weekly_wellness_trend(daily_rows):
    rows = list(daily_rows or [])
    current_week_rows = rows[-7:]
    current_week_summary = _summarize_week(current_week_rows)
    model_rows = [row for row in current_week_rows if row.get("has_user_data", True)]
    logged_days = current_week_summary["logged_days"]

    if logged_days < MIN_LOGGED_DAYS_FOR_WEEKLY_WELLNESS_PREDICTION:
        return {
            "title": "PCOS Wellness Trend",
            "predicted_label": "Not enough data yet",
            "explanation": "Log at least 4 days of lifestyle entries to build a PCOS wellness trend from your recent patterns.",
            "recommendation": None,
            "pattern_highlights": [],
            "tone": "info",
            "model_source": "insufficient_recent_lifestyle_logs",
            "model_dataset": None,
            "model_test_accuracy": None,
            "confidence_label": None,
            "chart_points": [],
            "current_week_score": None,
            "logged_days": logged_days,
            "has_sufficient_data": False,
        }

    try:
        prediction_input = build_weekly_prediction_input(model_rows)
        predicted_label = predict_wellness(prediction_input)
        model_bundle = get_weekly_wellness_trend_bundle()
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        return {
            "title": "PCOS Wellness Trend",
            "predicted_label": "Unavailable",
            "explanation": "Your recent 7-day lifestyle logs were reviewed, but the PCOS trend model is temporarily unavailable.",
            "recommendation": "Keep logging sleep, meals, movement, stress, and hydration so the next PCOS trend update has stronger data.",
            "pattern_highlights": [],
            "tone": "warning",
            "model_source": "ml_model_unavailable",
            "model_dataset": None,
            "model_test_accuracy": None,
            "confidence_label": build_weekly_confidence_label(logged_days),
            "chart_points": build_weekly_chart_points(current_week_rows),
            "current_week_score": None,
            "logged_days": logged_days,
            "has_sufficient_data": True,
        }

    factors = identify_weekly_trend_factors(model_rows, predicted_label=predicted_label)
    tone_map = {
        "Improving": "success",
        "Stable": "info",
        "Declining": "warning",
    }

    return {
        "title": "PCOS Wellness Trend",
        "predicted_label": predicted_label,
        "explanation": "Predicted from your recent 7-day lifestyle logs to highlight patterns that may influence PCOS management.",
        "recommendation": build_weekly_trend_guidance(predicted_label),
        "pattern_highlights": factors,
        "tone": tone_map.get(predicted_label, "info"),
        "model_source": "decision_tree_classifier_from_csv_dataset",
        "model_dataset": model_bundle["dataset_path"],
        "model_test_accuracy": model_bundle["test_accuracy"],
        "confidence_label": build_weekly_confidence_label(logged_days),
        "chart_points": build_weekly_chart_points(current_week_rows),
        "current_week_score": current_week_summary["wellness_score"],
        "logged_days": logged_days,
        "has_sufficient_data": True,
    }
