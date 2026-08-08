"""
Apple Health MCP Server
Loads preprocessed Parquet files (run preprocess.py first) for fast startup.
Falls back to parsing the XML directly if Parquet files are not found.
"""

import os
import sys
from pathlib import Path
from collections import defaultdict
from typing import Optional

import pandas as pd
from mcp.server.fastmcp import FastMCP

from config import HK_TYPE_MAP, SHORT_NAMES, SLEEP_VALUES, CATEGORY_TYPES

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

EXPORT_PATH = Path(os.environ.get(
    "APPLE_HEALTH_EXPORT",
    Path(__file__).parent.parent / "apple_health_export" / "exportación.xml"
))
DATA_DIR = Path(os.environ.get(
    "APPLE_HEALTH_DATA_DIR",
    Path(__file__).parent / "data"
))

# ---------------------------------------------------------------------------
# Data loading — Parquet first, XML fallback
# ---------------------------------------------------------------------------

_cache: dict = {}


def _load_data() -> dict:
    """
    Load data from Parquet files (fast). Falls back to parsing XML if
    Parquet files don't exist — run preprocess.py to generate them.
    """
    if _cache:
        return _cache

    parquet_available = DATA_DIR.exists() and any(DATA_DIR.glob("*.parquet"))

    if parquet_available:
        _load_from_parquet()
    else:
        print(
            "[apple-health-mcp] No Parquet files found. "
            "Run 'python preprocess.py' for faster startup. Falling back to XML...",
            file=sys.stderr,
        )
        _load_from_xml()

    return _cache


def _load_from_parquet() -> None:
    """Load all Parquet files from DATA_DIR into _cache."""
    import time
    t0 = time.time()
    print(f"[apple-health-mcp] Loading from Parquet: {DATA_DIR}", file=sys.stderr)

    frames: dict[str, pd.DataFrame] = {}
    for short in SHORT_NAMES:
        path = DATA_DIR / f"{short}.parquet"
        if path.exists():
            df = pd.read_parquet(path)
            # Restore datetime columns
            df["startDate"] = pd.to_datetime(df["startDate"], utc=True, errors="coerce")
            df["endDate"]   = pd.to_datetime(df["endDate"],   utc=True, errors="coerce")
            # date col is stored as str in Parquet — keep as str for filtering
            frames[short] = df

    workout_path = DATA_DIR / "workouts.parquet"
    workout_df = pd.DataFrame()
    if workout_path.exists():
        workout_df = pd.read_parquet(workout_path)
        workout_df["startDate"] = pd.to_datetime(workout_df["startDate"], utc=True, errors="coerce")
        workout_df["endDate"]   = pd.to_datetime(workout_df["endDate"],   utc=True, errors="coerce")

    me_info: dict = {}
    me_path = DATA_DIR / "me.parquet"
    if me_path.exists():
        me_info = pd.read_parquet(me_path).iloc[0].to_dict()

    _cache["frames"]   = frames
    _cache["workouts"] = workout_df
    _cache["me"]       = me_info

    total = sum(len(v) for v in frames.values())
    print(f"[apple-health-mcp] Loaded {total:,} records in {time.time()-t0:.2f}s", file=sys.stderr)


def _load_from_xml() -> None:
    """Stream-parse the XML as fallback when Parquet files don't exist."""
    from lxml import etree
    from collections import defaultdict
    import time



    t0 = time.time()
    print(f"[apple-health-mcp] Parsing XML: {EXPORT_PATH}", file=sys.stderr)

    records: dict[str, list] = defaultdict(list)
    workouts: list = []
    me_info: dict = {}

    context = etree.iterparse(str(EXPORT_PATH), events=("end",), tag=("Record", "Workout", "Me"))
    for _event, elem in context:
        tag = elem.tag
        if tag == "Me":
            me_info = dict(elem.attrib)
        elif tag == "Record":
            hk_type = elem.get("type", "")
            if hk_type in HK_TYPE_MAP:
                records[HK_TYPE_MAP[hk_type]].append({
                    "startDate":  elem.get("startDate"),
                    "endDate":    elem.get("endDate"),
                    "value":      elem.get("value"),
                    "unit":       elem.get("unit"),
                    "sourceName": elem.get("sourceName"),
                })
        elif tag == "Workout":
            avg_hr = None
            max_hr = None
            for child in elem:
                if child.tag == "WorkoutStatistics":
                    stat_type = child.get("type", "")
                    if "HeartRate" in stat_type:
                        avg_hr = child.get("average")
                        max_hr = child.get("maximum")
            workouts.append({
                "activityType":      elem.get("workoutActivityType", "").replace("HKWorkoutActivityType", ""),
                "startDate":         elem.get("startDate"),
                "endDate":           elem.get("endDate"),
                "duration_min":      elem.get("duration"),
                "totalDistance":     elem.get("totalDistance"),
                "totalDistanceUnit": elem.get("totalDistanceUnit"),
                "totalEnergy_kcal":  elem.get("totalEnergyBurned"),
                "sourceName":        elem.get("sourceName"),
                "avgHeartRate":      avg_hr,
                "maxHeartRate":      max_hr,
            })
        elem.clear()

    frames: dict[str, pd.DataFrame] = {}
    for short, rows in records.items():
        df = pd.DataFrame(rows)
        df["startDate"] = pd.to_datetime(df["startDate"], utc=True, errors="coerce")
        df["endDate"]   = pd.to_datetime(df["endDate"],   utc=True, errors="coerce")
        if hk_type not in CATEGORY_TYPES:
            df["value"] = pd.to_numeric(df["value"], errors="coerce")
        df["date"]      = df["startDate"].dt.date.astype(str)
        frames[short] = df

    workout_df = pd.DataFrame(workouts)
    if not workout_df.empty:
        workout_df["startDate"]        = pd.to_datetime(workout_df["startDate"], utc=True, errors="coerce")
        workout_df["endDate"]          = pd.to_datetime(workout_df["endDate"],   utc=True, errors="coerce")
        workout_df["duration_min"]     = pd.to_numeric(workout_df["duration_min"], errors="coerce")
        workout_df["totalDistance"]    = pd.to_numeric(workout_df["totalDistance"], errors="coerce")
        workout_df["totalEnergy_kcal"] = pd.to_numeric(workout_df["totalEnergy_kcal"], errors="coerce")
        workout_df["avgHeartRate"]     = pd.to_numeric(workout_df["avgHeartRate"], errors="coerce")
        workout_df["maxHeartRate"]     = pd.to_numeric(workout_df["maxHeartRate"], errors="coerce")
        workout_df["date"]             = workout_df["startDate"].dt.date.astype(str)

    _cache["frames"]   = frames
    _cache["workouts"] = workout_df
    _cache["me"]       = me_info

    total = sum(len(v) for v in frames.values())
    print(f"[apple-health-mcp] Loaded {total:,} records in {time.time()-t0:.1f}s", file=sys.stderr)


def _get_frame(short_name: str) -> Optional[pd.DataFrame]:
    data = _load_data()
    return data["frames"].get(short_name)


def _filter_dates(df: pd.DataFrame, start: Optional[str], end: Optional[str]) -> pd.DataFrame:
    if start:
        df = df[df["date"] >= start]
    if end:
        df = df[df["date"] <= end]
    return df


def _date_summary(df: pd.DataFrame, agg: str = "sum") -> str:
    if df.empty:
        return "No data for the specified range."
    daily = df.groupby("date")["value"].agg(agg).reset_index()
    daily.columns = ["date", "value"]
    daily["value"] = daily["value"].round(2)
    return daily.to_string(index=False)


def _to_period(date_str: str, granularity: str) -> str:
    """Map a date string to a period key based on granularity."""
    d = pd.Timestamp(date_str)
    if granularity == "weekly":
        iso = d.isocalendar()
        return f"{iso.year}-W{iso.week:02d}"
    if granularity == "monthly":
        return d.strftime("%Y-%m")
    if granularity == "yearly":
        return d.strftime("%Y")
    return date_str  # daily / nightly


# ---------------------------------------------------------------------------
# MCP Server
# ---------------------------------------------------------------------------

mcp = FastMCP("apple-health", host="0.0.0.0", port=8001)


@mcp.tool()
def health_summary() -> str:
    """
    Returns a high-level summary of all available Apple Health data:
    types available, total record counts, and date range.
    """
    data = _load_data()
    frames = data["frames"]
    me = data["me"]
    workouts = data["workouts"]

    lines = ["=== Apple Health Export Summary ===\n"]

    if me:
        dob = me.get("HKCharacteristicTypeIdentifierDateOfBirth", "?")
        sex = me.get("HKCharacteristicTypeIdentifierBiologicalSex", "?").replace("HKBiologicalSex", "")
        lines.append(f"Date of birth : {dob}")
        lines.append(f"Biological sex: {sex}\n")

    lines.append(f"{'Data type':<45} {'Records':>10}  {'From':<12}  {'To':<12}")
    lines.append("-" * 85)

    for short in SHORT_NAMES:
        df = frames.get(short)
        if df is not None and not df.empty:
            n   = len(df)
            lo  = str(df["date"].min())
            hi  = str(df["date"].max())
            lines.append(f"{short:<45} {n:>10,}  {lo:<12}  {hi:<12}")

    if not workouts.empty:
        n   = len(workouts)
        lo  = str(workouts["date"].min())
        hi  = str(workouts["date"].max())
        lines.append(f"{'workouts':<45} {n:>10,}  {lo:<12}  {hi:<12}")

    return "\n".join(lines)


@mcp.tool()
def get_steps(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    granularity: str = "daily",
) -> str:
    """
    Returns step counts aggregated by granularity: daily (default), weekly, monthly, or yearly.
    Optionally filter by start_date and/or end_date (YYYY-MM-DD).
    """
    df = _get_frame("steps")
    if df is None or df.empty:
        return "No step data available."
    df = _filter_dates(df, start_date, end_date)

    daily = df.groupby("date")["value"].sum().reset_index()
    daily.columns = ["date", "steps"]

    if granularity == "daily":
        total = int(daily["steps"].sum())
        avg   = daily["steps"].mean()
        daily["steps"] = daily["steps"].round(0).astype(int)
        return f"Total steps: {total:,}  |  Daily average: {avg:,.0f}\n\n{daily.to_string(index=False)}"

    daily["period"] = daily["date"].apply(lambda d: _to_period(d, granularity))
    grouped = daily.groupby("period")["steps"].agg(total="sum", avg_daily="mean", days="count")
    grouped["total"]     = grouped["total"].round(0).astype(int)
    grouped["avg_daily"] = grouped["avg_daily"].round(0).astype(int)
    overall_avg = grouped["avg_daily"].mean()
    return f"Overall avg daily steps: {overall_avg:,.0f}\n\n{grouped.to_string()}"


@mcp.tool()
def get_heart_rate(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    stat: str = "mean",
    granularity: str = "daily",
) -> str:
    """
    Returns heart rate aggregated by granularity: daily (default), weekly, monthly, or yearly.
    stat: mean (default), min, max.
    Optionally filter by start_date and/or end_date (YYYY-MM-DD).
    """
    df = _get_frame("heart_rate")
    if df is None or df.empty:
        return "No heart rate data available."
    df = _filter_dates(df, start_date, end_date)
    agg_map = {"mean": "mean", "min": "min", "max": "max"}
    agg = agg_map.get(stat, "mean")

    daily = df.groupby("date")["value"].agg(agg).reset_index()
    daily.columns = ["date", "bpm"]
    daily["bpm"] = daily["bpm"].round(1)

    if granularity == "daily":
        overall = daily["bpm"].agg(agg)
        return f"Heart rate ({stat}) overall: {overall:.1f} bpm\n\n{daily.to_string(index=False)}"

    daily["period"] = daily["date"].apply(lambda d: _to_period(d, granularity))
    grouped = daily.groupby("period")["bpm"].agg(agg).round(1).reset_index()
    grouped.columns = ["period", f"bpm_{stat}"]
    overall = grouped[f"bpm_{stat}"].agg(agg)
    return f"Heart rate ({stat}) overall: {overall:.1f} bpm\n\n{grouped.to_string(index=False)}"


@mcp.tool()
def get_resting_heart_rate(start_date: Optional[str] = None, end_date: Optional[str] = None) -> str:
    """
    Returns resting heart rate values by day. Optionally filter by date range (YYYY-MM-DD).
    """
    df = _get_frame("resting_hr")
    if df is None or df.empty:
        return "No resting heart rate data available."
    df = _filter_dates(df, start_date, end_date)
    return _date_summary(df, "mean")


@mcp.tool()
def get_sleep(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    granularity: str = "nightly",
) -> str:
    """
    Returns sleep analysis aggregated by granularity: nightly (default), weekly, monthly, or yearly.
    Columns: Core, Deep, REM, Asleep (pre-watchOS 9), Awake, InBed, Total_sleep_h.
    Optionally filter by start_date and/or end_date (YYYY-MM-DD).
    """
    data = _load_data()
    df = data["frames"].get("sleep")
    if df is None or df.empty:
        return "No sleep data available."

    df = _filter_dates(df, start_date, end_date).copy()
    df["stage"]      = df["value"].map(SLEEP_VALUES).fillna(df["value"])
    df["duration_h"] = (df["endDate"] - df["startDate"]).dt.total_seconds() / 3600
    # Attribute sleep to the night it belongs to (if start < 12:00 → previous night)
    df["night"] = df.apply(
        lambda r: (pd.Timestamp(r["date"]) - pd.Timedelta(days=1)).date().isoformat()
        if r["startDate"].hour < 12 else r["date"],
        axis=1,
    )

    # Build nightly pivot
    pivot = (
        df.groupby(["night", "stage"])["duration_h"]
        .sum()
        .unstack(fill_value=0)
        .round(2)
    )
    total_sleep = pivot[[c for c in ["Core", "Deep", "REM", "Asleep"] if c in pivot.columns]].sum(axis=1)
    pivot["Total_sleep_h"] = total_sleep.round(2)

    if granularity == "nightly":
        means = pivot.mean().round(2)
        header = f"Nightly averages:\n{means.to_string()}\n\n"
        return header + pivot.to_string()

    # Aggregate nightly data by period
    pivot = pivot.reset_index()
    pivot["period"] = pivot["night"].apply(lambda d: _to_period(d, granularity))
    sleep_cols = [c for c in pivot.columns if c not in ("night", "period")]
    grouped = pivot.groupby("period")[sleep_cols].mean().round(2)
    grouped["nights"] = pivot.groupby("period")["night"].count()
    overall_avg = grouped["Total_sleep_h"].mean()
    return f"Average total sleep: {overall_avg:.2f} h/night\n\n{grouped.to_string()}"


@mcp.tool()
def get_workouts(
    activity_type: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    limit: int = 50
) -> str:
    """
    Returns workout sessions. Optionally filter by activity_type (e.g. 'Running', 'Cycling'),
    date range (YYYY-MM-DD), and limit the number of results (default 50).
    """
    data = _load_data()
    df = data["workouts"].copy()
    if df.empty:
        return "No workout data available."

    if activity_type:
        df = df[df["activityType"].str.contains(activity_type, case=False, na=False)]
    df = _filter_dates(df, start_date, end_date)

    if df.empty:
        return "No workouts matching the given filters."

    # Summary stats
    n = len(df)
    types = df["activityType"].value_counts().to_dict()
    total_min = df["duration_min"].sum()
    total_kcal = df["totalEnergy_kcal"].sum()

    summary = (
        f"Total workouts: {n}\n"
        f"Total time: {total_min/60:.1f} h\n"
        f"Total energy: {total_kcal:,.0f} kcal\n"
        f"By type: {types}\n\n"
    )

    cols = ["date", "activityType", "duration_min", "totalDistance",
            "totalDistanceUnit", "totalEnergy_kcal", "avgHeartRate", "maxHeartRate", "sourceName"]
    display = df[[c for c in cols if c in df.columns]].head(limit)
    display = display.rename(columns={
        "activityType": "type",
        "duration_min": "mins",
        "totalDistance": "dist",
        "totalDistanceUnit": "dist_unit",
        "totalEnergy_kcal": "kcal",
        "avgHeartRate": "avg_hr",
        "maxHeartRate": "max_hr",
    })
    return summary + display.to_string(index=False)


@mcp.tool()
def get_body_metrics(start_date: Optional[str] = None, end_date: Optional[str] = None) -> str:
    """
    Returns body metrics: weight (kg), BMI, body fat %, and lean body mass over time.
    Optionally filter by date range (YYYY-MM-DD).
    """
    data = _load_data()
    results = []
    for short in ["body_mass", "bmi", "body_fat", "lean_body_mass"]:
        df = _get_frame(short)
        if df is not None and not df.empty:
            df = _filter_dates(df, start_date, end_date)
            if not df.empty:
                unit = df["unit"].iloc[0] if "unit" in df.columns else ""
                daily = df.groupby("date")["value"].mean().round(2).reset_index()
                results.append(f"--- {short} ({unit}) ---\n{daily.to_string(index=False)}")

    return "\n\n".join(results) if results else "No body metrics available."


@mcp.tool()
def get_activity_energy(start_date: Optional[str] = None, end_date: Optional[str] = None) -> str:
    """
    Returns daily active and basal energy burned (kcal), plus walking/running distance.
    Optionally filter by date range (YYYY-MM-DD).
    """
    data = _load_data()
    sections = []
    for short, label in [
        ("active_energy", "Active energy (kcal)"),
        ("basal_energy",  "Basal energy (kcal)"),
        ("distance_walk", "Walking/running distance"),
        ("flights_climbed", "Flights climbed"),
    ]:
        df = _get_frame(short)
        if df is not None and not df.empty:
            df = _filter_dates(df, start_date, end_date)
            if not df.empty:
                unit = df["unit"].iloc[0] if "unit" in df.columns else ""
                total = df["value"].sum()
                avg   = df.groupby("date")["value"].sum().mean()
                sections.append(
                    f"--- {label} ({unit}) ---\n"
                    f"Total: {total:,.1f}  |  Daily average: {avg:,.1f}\n"
                    f"{_date_summary(df, 'sum')}"
                )
    return "\n\n".join(sections) if sections else "No energy/activity data available."


@mcp.tool()
def get_nutrition(start_date: Optional[str] = None, end_date: Optional[str] = None) -> str:
    """
    Returns daily nutritional intake: energy (kcal), protein, carbs, fat.
    Optionally filter by date range (YYYY-MM-DD).
    """
    data = _load_data()
    sections = []
    for short, label in [
        ("dietary_energy",  "Energy (kcal)"),
        ("dietary_protein", "Protein (g)"),
        ("dietary_carbs",   "Carbohydrates (g)"),
        ("dietary_fat",     "Total fat (g)"),
    ]:
        df = _get_frame(short)
        if df is not None and not df.empty:
            df = _filter_dates(df, start_date, end_date)
            if not df.empty:
                sections.append(f"--- {label} ---\n{_date_summary(df, 'sum')}")
    return "\n\n".join(sections) if sections else "No nutrition data available."


@mcp.tool()
def query_health_data(
    metric: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    aggregation: str = "sum"
) -> str:
    """
    Generic query for any available metric.
    metric: one of steps, heart_rate, resting_hr, active_energy, basal_energy,
            distance_walk, distance_cycling, flights_climbed, sleep, body_mass,
            bmi, body_fat, lean_body_mass, walking_speed, walking_steadiness,
            dietary_energy, dietary_protein, dietary_carbs, dietary_fat,
            headphone_audio, walking_step_length, walking_double_support,
            walking_asymmetry
    aggregation: sum, mean, min, max (default: sum)
    start_date / end_date: YYYY-MM-DD (optional)
    """
    df = _get_frame(metric)
    if df is None:
        available = ", ".join(SHORT_NAMES)
        return f"Unknown metric '{metric}'. Available: {available}"
    if df.empty:
        return f"No data for '{metric}'."
    df = _filter_dates(df, start_date, end_date)
    return _date_summary(df, aggregation)


# ---------------------------------------------------------------------------
# /ingest endpoint — receives Health Auto Export webhook payloads
# ---------------------------------------------------------------------------

# Health Auto Export metric names → our Parquet short names
_HAE_METRIC_MAP = {
    "step_count":                        "steps",
    "heart_rate":                        "heart_rate",
    "resting_heart_rate":                "resting_hr",
    "active_energy":                     "active_energy",
    "basal_energy_burned":               "basal_energy",
    "walking_running_distance":          "distance_walk",
    "cycling_distance":                  "distance_cycling",
    "flights_climbed":                   "flights_climbed",
    "weight_body_mass":                  "body_mass",
    "body_mass_index":                   "bmi",
    "body_fat_percentage":               "body_fat",
    "lean_body_mass":                    "lean_body_mass",
    "walking_speed":                     "walking_speed",
    "walking_step_length":               "walking_step_length",
    "walking_double_support_percentage": "walking_double_support",
    "walking_asymmetry_percentage":      "walking_asymmetry",
    "apple_walking_steadiness":          "walking_steadiness",
    "dietary_energy_consumed":           "dietary_energy",
    "dietary_protein":                   "dietary_protein",
    "dietary_carbohydrates":             "dietary_carbs",
    "dietary_fat_total":                 "dietary_fat",
    "headphone_audio_exposure":          "headphone_audio",
}

_INGEST_SECRET = os.environ.get("INTERNAL_SECRET", "")
_LAST_PAYLOAD_PATH = DATA_DIR / "_last_ingest_payload.json"

# Health Auto Export's sleep_analysis sends one row per night with daily
# aggregate hours per stage, unlike our per-interval "sleep" parquet schema
# (one row per stage-interval, value = raw HK category string). We synthesize
# one synthetic interval per stage per night to bridge the two formats.
_HAE_SLEEP_STAGE_MAP = {
    "core":   "HKCategoryValueSleepAnalysisAsleepCore",
    "deep":   "HKCategoryValueSleepAnalysisAsleepDeep",
    "rem":    "HKCategoryValueSleepAnalysisAsleepREM",
    "awake":  "HKCategoryValueSleepAnalysisAwake",
    "asleep": "HKCategoryValueSleepAnalysisAsleepUnspecified",
    "inBed":  "HKCategoryValueSleepAnalysisInBed",
}


def _check_ingest_auth(request) -> bool:
    api_key = request.headers.get("x-api-key") or request.query_params.get("api_key", "")
    return not _INGEST_SECRET or api_key == _INGEST_SECRET


def _upsert_parquet(short: str, new_rows: list[dict]) -> int:
    """Merge new_rows into the existing Parquet for `short`, dedup by startDate. Returns rows added."""
    if not new_rows:
        return 0

    new_df = pd.DataFrame(new_rows)
    new_df["startDate"] = pd.to_datetime(new_df["startDate"], utc=True, errors="coerce")
    new_df["endDate"]   = pd.to_datetime(new_df["endDate"],   utc=True, errors="coerce")
    new_df["value"]     = pd.to_numeric(new_df["value"], errors="coerce")
    new_df["date"]      = new_df["startDate"].dt.date.astype(str)

    path = DATA_DIR / f"{short}.parquet"
    if path.exists():
        existing = pd.read_parquet(path)
        existing["startDate"] = pd.to_datetime(existing["startDate"], utc=True, errors="coerce")
        combined = pd.concat([existing, new_df], ignore_index=True)
        combined = combined.drop_duplicates(subset=["startDate"], keep="last")
    else:
        combined = new_df

    combined = combined.sort_values("startDate")
    combined.to_parquet(path, index=False)

    added = len(combined) - (len(existing) if path.exists() else 0)
    return max(added, 0)


def _hae_qty(value) -> Optional[float]:
    """HAE numeric fields can be a plain number or {"qty": x, "units": ...}."""
    if value is None:
        return None
    if isinstance(value, dict):
        value = value.get("qty")
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _upsert_sleep(new_rows: list[dict]) -> int:
    """Replace sleep.parquet rows for the nights present in new_rows with synthetic
    per-stage rows derived from HAE's daily aggregate. Unlike _upsert_parquet, "value"
    stays a category string (not coerced to numeric), and replacement is by "date"
    (whole night), not by exact startDate, since synthetic and XML-derived intervals
    for the same night would otherwise coexist and double-count the totals."""
    if not new_rows:
        return 0

    new_df = pd.DataFrame(new_rows)
    new_df["startDate"] = pd.to_datetime(new_df["startDate"], utc=True, errors="coerce")
    new_df["endDate"]   = pd.to_datetime(new_df["endDate"],   utc=True, errors="coerce")
    nights = set(new_df["date"])

    path = DATA_DIR / "sleep.parquet"
    if path.exists():
        existing = pd.read_parquet(path)
        existing["startDate"] = pd.to_datetime(existing["startDate"], utc=True, errors="coerce")
        existing = existing[~existing["date"].isin(nights)]  # drop old rows for these nights
        combined = pd.concat([existing, new_df], ignore_index=True)
    else:
        combined = new_df

    combined = combined.sort_values("startDate")
    combined.to_parquet(path, index=False)
    return len(new_df)


def _upsert_workouts(new_rows: list[dict]) -> int:
    """Merge new workout rows into workouts.parquet, dedup by startDate."""
    if not new_rows:
        return 0

    new_df = pd.DataFrame(new_rows)
    new_df["startDate"]        = pd.to_datetime(new_df["startDate"], utc=True, errors="coerce")
    new_df["endDate"]          = pd.to_datetime(new_df["endDate"],   utc=True, errors="coerce")
    new_df["duration_min"]     = pd.to_numeric(new_df["duration_min"], errors="coerce")
    new_df["totalDistance"]    = pd.to_numeric(new_df["totalDistance"], errors="coerce")
    new_df["totalEnergy_kcal"] = pd.to_numeric(new_df["totalEnergy_kcal"], errors="coerce")
    new_df["avgHeartRate"]     = pd.to_numeric(new_df["avgHeartRate"], errors="coerce")
    new_df["maxHeartRate"]     = pd.to_numeric(new_df["maxHeartRate"], errors="coerce")
    new_df["date"]             = new_df["startDate"].dt.date.astype(str)

    path = DATA_DIR / "workouts.parquet"
    if path.exists():
        existing = pd.read_parquet(path)
        existing["startDate"] = pd.to_datetime(existing["startDate"], utc=True, errors="coerce")
        combined = pd.concat([existing, new_df], ignore_index=True)
        combined = combined.drop_duplicates(subset=["startDate", "activityType"], keep="last")
    else:
        combined = new_df

    combined = combined.sort_values("startDate")
    combined.to_parquet(path, index=False)

    added = len(combined) - (len(existing) if path.exists() else 0)
    return max(added, 0)


async def ingest_handler(request):
    from starlette.responses import JSONResponse
    import json

    if not _check_ingest_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)

    # Save raw payload for debugging (overwritten each call)
    DATA_DIR.mkdir(exist_ok=True)
    _LAST_PAYLOAD_PATH.write_text(json.dumps(payload, indent=2, default=str))

    updated: dict[str, int] = {}
    data_root = payload.get("data", payload)

    metrics = data_root.get("metrics", [])
    for metric in metrics:
        hae_name = metric.get("name", "")

        if hae_name == "sleep_analysis":
            sleep_rows = []
            for sample in metric.get("data", []):
                date_str = sample.get("date")
                # Keep the original local offset (not utc=True) so the calendar date
                # matches HAE's own "date" field — converting midnight-local straight
                # to UTC would shift it to the previous day.
                night = pd.to_datetime(date_str, errors="coerce") if date_str else None
                if night is None or pd.isna(night):
                    continue
                night_date = night.date().isoformat()
                source = sample.get("source", "HealthAutoExport")
                # Anchor each stage at hour>=12 on the night's own date so get_sleep's
                # night-attribution formula (hour<12 -> previous day) keeps it on this
                # same calendar date, matching HAE's own "date" field for the night.
                for i, (hae_field, hk_value) in enumerate(_HAE_SLEEP_STAGE_MAP.items()):
                    hours = _hae_qty(sample.get(hae_field))
                    if not hours:
                        continue
                    start = pd.Timestamp(f"{night_date} 22:00:00", tz="UTC") + pd.Timedelta(minutes=i)
                    end = start + pd.Timedelta(hours=hours)
                    sleep_rows.append({
                        "startDate":  start.isoformat(),
                        "endDate":    end.isoformat(),
                        "value":      hk_value,
                        "unit":       "",
                        "sourceName": source,
                        "date":       night_date,
                    })

            added = _upsert_sleep(sleep_rows)
            if sleep_rows:
                updated["sleep"] = added
            continue

        short = _HAE_METRIC_MAP.get(hae_name)
        if not short:
            continue

        unit = metric.get("units", "")
        rows = []
        for sample in metric.get("data", []):
            date_str = sample.get("date") or sample.get("startDate")
            if not date_str:
                continue
            # HAE sends qty for most metrics; heart_rate sends Avg/Min/Max
            value = sample.get("qty") or sample.get("Avg") or sample.get("value")
            if value is None:
                continue
            start = pd.to_datetime(date_str, utc=True, errors="coerce")
            if pd.isna(start):
                continue
            end_str = sample.get("endDate", date_str)
            rows.append({
                "startDate":  start.isoformat(),
                "endDate":    pd.to_datetime(end_str, utc=True, errors="coerce").isoformat(),
                "value":      float(value),
                "unit":       unit,
                "sourceName": sample.get("source", "HealthAutoExport"),
            })

        added = _upsert_parquet(short, rows)
        if rows:
            updated[short] = added

    workouts = data_root.get("workouts", [])
    workout_rows = []
    for w in workouts:
        start_str = w.get("start") or w.get("startDate")
        end_str   = w.get("end") or w.get("endDate")
        if not start_str:
            continue
        start = pd.to_datetime(start_str, utc=True, errors="coerce")
        end   = pd.to_datetime(end_str, utc=True, errors="coerce") if end_str else start
        if pd.isna(start):
            continue

        duration_sec = _hae_qty(w.get("duration"))
        distance     = w.get("distance") or w.get("totalDistance")
        energy       = w.get("activeEnergyBurned") or w.get("totalEnergyBurned")

        hr_samples = w.get("heartRateData", [])
        avg_values = [_hae_qty(s.get("Avg")) for s in hr_samples if _hae_qty(s.get("Avg")) is not None]
        max_values = [_hae_qty(s.get("Max")) for s in hr_samples if _hae_qty(s.get("Max")) is not None]

        workout_rows.append({
            "activityType":      w.get("name", "Unknown"),
            "startDate":         start.isoformat(),
            "endDate":           end.isoformat(),
            "duration_min":      (duration_sec / 60) if duration_sec is not None else None,
            "totalDistance":     _hae_qty(distance),
            "totalDistanceUnit": distance.get("units", "") if isinstance(distance, dict) else "",
            "totalEnergy_kcal":  _hae_qty(energy),
            "avgHeartRate":      (sum(avg_values) / len(avg_values)) if avg_values else None,
            "maxHeartRate":      max(max_values) if max_values else None,
            "sourceName":        w.get("source", "HealthAutoExport"),
        })

    added_workouts = _upsert_workouts(workout_rows)
    if workout_rows:
        updated["workouts"] = added_workouts

    # Invalidate in-memory cache so next tool call reloads fresh data
    _cache.clear()

    print(f"[apple-health-mcp] /ingest: updated {updated}", file=sys.stderr)
    return JSONResponse({"ok": True, "updated": updated})


async def inspect_handler(request):
    from starlette.responses import JSONResponse
    import json

    if not _check_ingest_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    if not _LAST_PAYLOAD_PATH.exists():
        return JSONResponse({"error": "no payload received yet"}, status_code=404)

    return JSONResponse(json.loads(_LAST_PAYLOAD_PATH.read_text()))


async def ingest_info_handler(request):
    from starlette.responses import JSONResponse
    return JSONResponse({"error": "use POST to send Health Auto Export payloads"}, status_code=405)


# Reusable Starlette app for the /ingest routes only (no mcp_app mounted here —
# callers combine this with the MCP app themselves, e.g. wrapper.py on Fly.io,
# to avoid Starlette's Mount() breaking the MCP app's lifespan/task-group init).
from starlette.applications import Starlette
from starlette.routing import Route

ingest_app = Starlette(routes=[
    Route("/ingest",         endpoint=ingest_handler,      methods=["POST"]),
    Route("/ingest",         endpoint=ingest_info_handler, methods=["GET"]),
    Route("/ingest/inspect", endpoint=inspect_handler,     methods=["GET"]),
])

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn

    mcp_app = mcp.streamable_http_app()

    class _LocalDevApp:
        """Manual ASGI router (not Starlette Mount) so mcp_app's lifespan still runs."""

        async def __call__(self, scope, receive, send):
            if scope["type"] == "http" and scope["path"].startswith("/ingest"):
                await ingest_app(scope, receive, send)
                return
            await mcp_app(scope, receive, send)

    uvicorn.run(_LocalDevApp(), host="0.0.0.0", port=8001)
