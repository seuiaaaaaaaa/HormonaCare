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

COMPLETE_WEEK_LOGGED_DAYS = 7
MIN_LOGGED_DAYS_FOR_WEEKLY_WELLNESS_PREDICTION = 4
INSUFFICIENT_WEEKLY_WELLNESS_MESSAGE = "Not enough data to generate a reliable wellness trend."
WEEKLY_WELLNESS_DISCLAIMER = (
    "This insight is based on recent lifestyle data and does not replace medical advice."
)


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
    """Describe display confidence without changing the ML prediction output."""
    if logged_days >= COMPLETE_WEEK_LOGGED_DAYS:
        return "High confidence."
    if logged_days == 6:
        return "Moderate confidence (6/7 days logged)."
    if logged_days >= MIN_LOGGED_DAYS_FOR_WEEKLY_WELLNESS_PREDICTION:
        return "Low confidence (limited data)."
    return None


def build_weekly_display_label(predicted_label, logged_days):
    if logged_days < MIN_LOGGED_DAYS_FOR_WEEKLY_WELLNESS_PREDICTION:
        return INSUFFICIENT_WEEKLY_WELLNESS_MESSAGE
    if logged_days <= 5:
        return f"Preliminary trend: {predicted_label}"
    return predicted_label


def build_weekly_badge_label(predicted_label, logged_days):
    if logged_days < MIN_LOGGED_DAYS_FOR_WEEKLY_WELLNESS_PREDICTION:
        return "Incomplete data"
    return predicted_label


def format_weekly_checkin_summary(logged_days):
    """Describe how many recent days include logs."""
    return f"{logged_days}/{COMPLETE_WEEK_LOGGED_DAYS} days logged"


def format_weekly_activity_summary(logged_days):
    """Describe recent logged activity in sentence form."""
    day_label = "day" if logged_days == 1 else "days"
    return f"{logged_days} of {COMPLETE_WEEK_LOGGED_DAYS} {day_label} logged"


def build_weekly_explanation(logged_days):
    """Keep full-window wording only when all seven days are present."""
    if logged_days >= COMPLETE_WEEK_LOGGED_DAYS:
        return "Predicted from your recent 7-day window."
    return f"Based on partial data ({format_weekly_activity_summary(logged_days)})."


def build_weekly_data_completeness_label(logged_days):
    return f"Data completeness: {logged_days}/{COMPLETE_WEEK_LOGGED_DAYS} days."


def build_weekly_completion_helper(logged_days):
    if logged_days >= COMPLETE_WEEK_LOGGED_DAYS:
        return None
    return "Log more days to improve accuracy."


def build_weekly_chart_window_label(logged_days):
    if logged_days >= COMPLETE_WEEK_LOGGED_DAYS:
        return "7-day window"
    return "Recent activity"


def build_weekly_chart_points(daily_rows):
    """Create a compact 7-day visual signal for the dashboard card."""
    rows = list(daily_rows or [])[-7:]
    fallback_labels = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

    if len(rows) < COMPLETE_WEEK_LOGGED_DAYS:
        rows = ([None] * (COMPLETE_WEEK_LOGGED_DAYS - len(rows))) + rows

    raw_values = []
    labels = []
    for index, row in enumerate(rows):
        row = row or {}
        row_date = row.get("date")
        label = row_date.strftime("%a") if hasattr(row_date, "strftime") else fallback_labels[index]
        has_data = row.get("has_user_data") is not False and any(
            row.get(key) is not None
            for key in ("sleep_hours", "sleep_duration", "water_intake", "physical_activity", "exercise_minutes", "stress_level")
        )
        raw_values.append(_calculate_daily_wellness_score(row) if has_data else None)
        labels.append(label)

    chart_points = []
    for index, value in enumerate(raw_values):
        if value is None:
            height = 0
            has_data = False
        else:
            normalized = _clamp((value + 0.25) / 1.0, 0.0, 1.0)
            height = round(30 + (normalized * 40))
            has_data = True
        chart_points.append(
            {
                "label": labels[index],
                "height": height,
                "has_data": has_data,
                "tooltip": f"{labels[index]}: logged" if has_data else "No data recorded.",
            }
        )

    return chart_points


def build_health_assessment(sleep_hours, water_intake, stress_level, activity_minutes, cycle_logs=None):
    sleep_hours = _coerce_number(sleep_hours)
    water_intake = _coerce_number(water_intake)
    stress_level = _coerce_number(stress_level)
    activity_minutes = _coerce_number(activity_minutes)

    sleep_hours = 7.0 if sleep_hours is None else sleep_hours
    water_intake = 2.0 if water_intake is None else water_intake
    stress_level = 5.0 if stress_level is None else stress_level
    activity_minutes = 30.0 if activity_minutes is None else activity_minutes

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


def build_weekly_wellness_fallback(logged_days=0):
    return {
        "title": "PCOS Wellness Trend",
        "predicted_label": "Unavailable",
        "display_label": "Unavailable",
        "badge_label": "Model unavailable",
        "explanation": f"{build_weekly_explanation(logged_days)}, but the wellness trend could not be calculated right now.",
        "recommendation": "Keep logging sleep, meals, movement, stress, and hydration so the next PCOS trend update has stronger data.",
        "pattern_highlights": [],
        "tone": "warning",
        "model_source": "dashboard_fallback",
        "model_dataset": None,
        "model_test_accuracy": None,
        "confidence_label": build_weekly_confidence_label(logged_days) if logged_days else None,
        "chart_points": [],
        "current_week_score": None,
        "logged_days": logged_days,
        "checkin_summary": None,
        "data_completeness_label": build_weekly_data_completeness_label(logged_days),
        "weekly_completion_helper": build_weekly_completion_helper(logged_days),
        "chart_window_label": build_weekly_chart_window_label(logged_days),
        "disclaimer": WEEKLY_WELLNESS_DISCLAIMER,
        "is_complete_week": logged_days >= COMPLETE_WEEK_LOGGED_DAYS,
        "has_sufficient_data": False,
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
            "predicted_label": None,
            "display_label": INSUFFICIENT_WEEKLY_WELLNESS_MESSAGE,
            "badge_label": "Incomplete data",
            "explanation": build_weekly_explanation(logged_days),
            "recommendation": None,
            "pattern_highlights": [],
            "tone": "info",
            "model_source": "insufficient_recent_lifestyle_logs",
            "model_dataset": None,
            "model_test_accuracy": None,
            "confidence_label": None,
            "chart_points": build_weekly_chart_points(current_week_rows),
            "current_week_score": None,
            "logged_days": logged_days,
            "checkin_summary": None,
            "data_completeness_label": build_weekly_data_completeness_label(logged_days),
            "weekly_completion_helper": build_weekly_completion_helper(logged_days),
            "chart_window_label": build_weekly_chart_window_label(logged_days),
            "disclaimer": WEEKLY_WELLNESS_DISCLAIMER,
            "is_complete_week": logged_days >= COMPLETE_WEEK_LOGGED_DAYS,
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
            "display_label": "Unavailable",
            "badge_label": "Model unavailable",
            "explanation": f"{build_weekly_explanation(logged_days)}, but the PCOS trend model is temporarily unavailable.",
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
            "checkin_summary": None,
            "data_completeness_label": build_weekly_data_completeness_label(logged_days),
            "weekly_completion_helper": build_weekly_completion_helper(logged_days),
            "chart_window_label": build_weekly_chart_window_label(logged_days),
            "disclaimer": WEEKLY_WELLNESS_DISCLAIMER,
            "is_complete_week": logged_days >= COMPLETE_WEEK_LOGGED_DAYS,
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
        "display_label": build_weekly_display_label(predicted_label, logged_days),
        "badge_label": build_weekly_badge_label(predicted_label, logged_days),
        "explanation": build_weekly_explanation(logged_days),
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
        "checkin_summary": None,
        "data_completeness_label": build_weekly_data_completeness_label(logged_days),
        "weekly_completion_helper": build_weekly_completion_helper(logged_days),
        "chart_window_label": build_weekly_chart_window_label(logged_days),
        "disclaimer": WEEKLY_WELLNESS_DISCLAIMER,
        "is_complete_week": logged_days >= COMPLETE_WEEK_LOGGED_DAYS,
        "has_sufficient_data": True,
    }
