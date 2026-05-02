from pathlib import Path

import pandas as pd

SOURCE_WORKBOOK_PATH = Path(r"C:\Users\custo\Downloads\Sleep Health and Lifestyle Dataset.xlsx")
OUTPUT_DATASET_PATH = Path(__file__).resolve().with_name("weekly_wellness_dataset.csv")
TARGET_COLUMNS = [
    "sleep_duration",
    "sleep_quality",
    "physical_activity",
    "stress_level",
    "heart_rate",
    "daily_steps",
]
SOURCE_COLUMN_ALIASES = {
    "sleep_duration": "sleep_duration",
    "sleep_quality": "sleep_quality",
    "quality_of_sleep": "sleep_quality",
    "physical_activity": "physical_activity",
    "physical_activity_level": "physical_activity",
    "stress_level": "stress_level",
    "heart_rate": "heart_rate",
    "daily_steps": "daily_steps",
}


def _normalize_column_name(name):
    cleaned = []
    for char in str(name).strip().lower():
        cleaned.append(char if char.isalnum() else "_")
    normalized = "".join(cleaned)
    while "__" in normalized:
        normalized = normalized.replace("__", "_")
    return normalized.strip("_")


def _scale_series(series):
    minimum = float(series.min())
    maximum = float(series.max())
    if maximum == minimum:
        return pd.Series([0.0] * len(series), index=series.index)
    return (series - minimum) / (maximum - minimum)


def build_weekly_wellness_dataset(source_path=SOURCE_WORKBOOK_PATH, output_path=OUTPUT_DATASET_PATH):
    source_path = Path(source_path)
    output_path = Path(output_path)

    if not source_path.exists():
        raise FileNotFoundError(f"Source workbook not found: {source_path}")

    raw_frame = pd.read_excel(source_path)
    rename_map = {}
    for column in raw_frame.columns:
        normalized = _normalize_column_name(column)
        if normalized in SOURCE_COLUMN_ALIASES:
            rename_map[column] = SOURCE_COLUMN_ALIASES[normalized]

    missing_columns = [column for column in TARGET_COLUMNS if column not in rename_map.values()]
    if missing_columns:
        raise ValueError(
            "Source workbook is missing required wellness columns: " + ", ".join(missing_columns)
        )

    working_frame = raw_frame[list(rename_map.keys())].rename(columns=rename_map)
    working_frame = working_frame[TARGET_COLUMNS].copy()

    for column in TARGET_COLUMNS:
        working_frame[column] = pd.to_numeric(working_frame[column], errors="coerce")

    feature_medians = working_frame[TARGET_COLUMNS].median()
    working_frame[TARGET_COLUMNS] = working_frame[TARGET_COLUMNS].fillna(feature_medians)
    working_frame = working_frame.drop_duplicates().reset_index(drop=True)

    working_frame["wellness_score"] = (
        _scale_series(working_frame["sleep_duration"])
        + _scale_series(working_frame["sleep_quality"])
        + _scale_series(working_frame["physical_activity"])
        - _scale_series(working_frame["stress_level"])
    )

    lower_threshold = working_frame["wellness_score"].quantile(1 / 3)
    upper_threshold = working_frame["wellness_score"].quantile(2 / 3)
    working_frame["wellness_trend"] = pd.cut(
        working_frame["wellness_score"],
        bins=[float("-inf"), lower_threshold, upper_threshold, float("inf")],
        labels=["Declining", "Stable", "Improving"],
        include_lowest=True,
    ).astype(str)

    output_frame = working_frame[TARGET_COLUMNS + ["wellness_trend"]]
    output_frame.to_csv(output_path, index=False)
    return output_frame


if __name__ == "__main__":
    dataset = build_weekly_wellness_dataset()
    print(f"Saved {len(dataset)} rows to {OUTPUT_DATASET_PATH}")
    print(dataset["wellness_trend"].value_counts().to_string())
