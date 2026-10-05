"""
Apple Health MCP Server
Loads preprocessed Parquet files (run preprocess.py first) for fast startup.
Falls back to parsing the XML directly if Parquet files are not found.
"""

import hmac
import os
import sys
from pathlib import Path
from collections import defaultdict
from typing import Optional

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pyarrow.compute as pc
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
    Ensure the cache skeleton (workouts + profile info) is initialized.
    Falls back to parsing the XML if Parquet files don't exist — run
    preprocess.py to generate them.

    Individual metric frames are NOT loaded here — they're loaded lazily,
    one Parquet file at a time, by _get_frame(). Eagerly loading all ~22
    metrics (2.9M+ rows combined, including high-frequency ones like
    per-minute headphone audio exposure) used to pull the whole dataset
    into memory on the very first tool call, even for queries that only
    ever touch one metric (e.g. body weight).
    """
    if _cache.get("_initialized"):
        return _cache

    parquet_available = DATA_DIR.exists() and any(DATA_DIR.glob("*.parquet"))

    if parquet_available:
        _init_from_parquet()
    else:
        print(
            "[apple-health-mcp] No Parquet files found. "
            "Run 'python preprocess.py' for faster startup. Falling back to XML...",
            file=sys.stderr,
        )
        _load_from_xml()

    return _cache


def _init_from_parquet() -> None:
    """Load workouts + profile info (small, always needed) from DATA_DIR.
    Metric frames start as an empty dict and are filled on demand by
    _get_frame()."""
    print(f"[apple-health-mcp] Initializing from Parquet: {DATA_DIR}", file=sys.stderr)

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

    _cache["frames"]       = {}
    _cache["workouts"]     = workout_df
    _cache["me"]           = me_info
    _cache["_initialized"] = True

    print(
        f"[apple-health-mcp] Ready ({len(workout_df):,} workouts). "
        "Metric data loads lazily per-request.",
        file=sys.stderr,
    )


def _load_metric_frame(
    short: str,
    start: Optional[str] = None,
    end: Optional[str] = None,
) -> Optional[pd.DataFrame]:
    """Read a single metric's Parquet file from disk. Returns None if the
    file doesn't exist. Not cached here — the caller (_get_frame) does that.

    When start/end are given, the date filter is pushed down to the Parquet
    read itself (pyarrow row-group pruning via the `filters` kwarg) instead
    of loading the whole file and filtering in pandas afterwards. The files
    are written sorted by startDate (see _upsert_parquet), so row groups
    outside the requested range are skipped entirely rather than just read
    and discarded — this is what actually saves memory for metrics with
    years of history (steps, heart rate, headphone audio, ...).
    """
    path = DATA_DIR / f"{short}.parquet"
    if not path.exists():
        return None
    filters = []
    if start:
        filters.append(("date", ">=", start))
    if end:
        filters.append(("date", "<=", end))
    df = pd.read_parquet(path, filters=filters or None)
    df["startDate"] = pd.to_datetime(df["startDate"], utc=True, errors="coerce")
    df["endDate"]   = pd.to_datetime(df["endDate"],   utc=True, errors="coerce")

    # `date` stays a plain string: an ordered categorical raises TypeError
    # in _filter_dates when the bound isn't one of its categories.
    for col in ("unit", "sourceName"):
        if col in df.columns:
            df[col] = df[col].astype("category")

    return df


def _parquet_quick_stats(short: str) -> Optional[tuple[int, str, str]]:
    """Row count and date range for a metric's Parquet file, read via
    pyarrow without building a full pandas DataFrame or going through the
    (now lazy) frame cache. Used by health_summary(), which otherwise would
    force every metric to load fully into memory just to print a table."""
    path = DATA_DIR / f"{short}.parquet"
    if not path.exists():
        return None
    table = pq.read_table(path, columns=["date"])
    n = table.num_rows
    if n == 0:
        return None
    col = table.column("date")
    return n, str(pc.min(col).as_py()), str(pc.max(col).as_py())


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


def _get_frame(
    short_name: str,
    start: Optional[str] = None,
    end: Optional[str] = None,
) -> Optional[pd.DataFrame]:
    """Return a metric's DataFrame.

    Without a date range, loads the full metric once and caches it for the
    life of the process (several tools reuse it across a session). With a
    date range, reads only the matching rows straight from Parquet via
    _load_metric_frame and does NOT touch the cache — caching every distinct
    range a caller might ask for would just reintroduce the memory problem
    this is meant to avoid, and re-reading a pruned Parquet slice is cheap.
    """
    if start or end:
        return _load_metric_frame(short_name, start, end)

    data = _load_data()
    frames = data["frames"]
    if short_name not in frames:
        frames[short_name] = _load_metric_frame(short_name)
    return frames[short_name]


def _filter_dates(df: pd.DataFrame, start: Optional[str], end: Optional[str]) -> pd.DataFrame:
    if start:
        df = df[df["date"] >= start]
    if end:
        df = df[df["date"] <= end]
    return df


_DAILY_STATS = ("sum", "mean", "min", "max", "count")


def _daily_stats(
    short: str,
    start: Optional[str] = None,
    end: Optional[str] = None,
) -> Optional[pd.DataFrame]:
    """Per-day sum/count/min/max/mean of a numeric metric's `value`.

    Aggregated with pyarrow in 100k-row batches over just the date/value
    columns, so raw rows never become a pandas DataFrame: measured on
    heart_rate (1.69M rows) this peaks at ~+40MB versus ~+300MB for the
    full-frame pandas path, and pyarrow's allocator hands the memory back
    afterwards. The result (one row per day) is cached for the whole
    history; date ranges are sliced from it.
    """
    cache = _cache.setdefault("daily", {})
    if short not in cache:
        path = DATA_DIR / f"{short}.parquet"
        if not path.exists():
            return None
        pf = pq.ParquetFile(path)
        partials = [
            pa.Table.from_batches([batch]).group_by("date").aggregate(
                [("value", "sum"), ("value", "count"), ("value", "min"), ("value", "max")]
            )
            for batch in pf.iter_batches(batch_size=100_000, columns=["date", "value"])
        ]
        if partials:
            daily = (
                pa.concat_tables(partials)
                .group_by("date")
                .aggregate([("value_sum", "sum"), ("value_count", "sum"),
                            ("value_min", "min"), ("value_max", "max")])
                .to_pandas()
            )
            daily.columns = ["date", "sum", "count", "min", "max"]
            daily["date"] = daily["date"].astype(str)
            daily["sum"] = daily["sum"].fillna(0.0)
            daily["mean"] = daily["sum"] / daily["count"].where(daily["count"] > 0)
            daily = daily.sort_values("date", ignore_index=True)
            first = next(pf.iter_batches(batch_size=1, columns=["unit"]), None)
            daily.attrs["unit"] = first.column(0)[0].as_py() if first is not None and first.num_rows else ""
        else:
            daily = pd.DataFrame(columns=["date", "sum", "count", "min", "max", "mean"])
            daily.attrs["unit"] = ""
        cache[short] = daily

    daily = cache[short]
    if start:
        daily = daily[daily["date"] >= start]
    if end:
        daily = daily[daily["date"] <= end]
    return daily


def _date_summary(daily: pd.DataFrame, agg: str = "sum") -> str:
    if daily.empty:
        return "No data for the specified range."
    out = daily[["date", agg]].rename(columns={agg: "value"})
    out["value"] = out["value"].round(2)
    return out.to_string(index=False)


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

    # Row counts/date ranges come from Parquet metadata (_parquet_quick_stats),
    # not from _get_frame — pulling every metric through the full frame cache
    # just to print this table would defeat the point of loading lazily.
    for short in SHORT_NAMES:
        stats = _parquet_quick_stats(short)
        if stats:
            n, lo, hi = stats
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
    stats = _daily_stats("steps", start_date, end_date)
    if stats is None or stats.empty:
        return "No step data available."

    daily = stats[["date", "sum"]].rename(columns={"sum": "steps"})

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
    stats = _daily_stats("heart_rate", start_date, end_date)
    if stats is None or stats.empty:
        return "No heart rate data available."
    agg = stat if stat in ("mean", "min", "max") else "mean"

    daily = stats[["date", agg]].rename(columns={agg: "bpm"})
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
    stats = _daily_stats("resting_hr", start_date, end_date)
    if stats is None or stats.empty:
        return "No resting heart rate data available."
    return _date_summary(stats, "mean")


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
    df = _get_frame("sleep", start_date, end_date)
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
    results = []
    for short in ["body_mass", "bmi", "body_fat", "lean_body_mass"]:
        stats = _daily_stats(short, start_date, end_date)
        if stats is not None and not stats.empty:
            unit = stats.attrs.get("unit", "")
            results.append(f"--- {short} ({unit}) ---\n{_date_summary(stats, 'mean')}")

    return "\n\n".join(results) if results else "No body metrics available."


@mcp.tool()
def get_activity_energy(start_date: Optional[str] = None, end_date: Optional[str] = None) -> str:
    """
    Returns daily active and basal energy burned (kcal), plus walking/running distance.
    Optionally filter by date range (YYYY-MM-DD).
    """
    sections = []
    for short, label in [
        ("active_energy", "Active energy (kcal)"),
        ("basal_energy",  "Basal energy (kcal)"),
        ("distance_walk", "Walking/running distance"),
        ("flights_climbed", "Flights climbed"),
    ]:
        stats = _daily_stats(short, start_date, end_date)
        if stats is not None and not stats.empty:
            unit = stats.attrs.get("unit", "")
            total = stats["sum"].sum()
            avg   = stats["sum"].mean()
            sections.append(
                f"--- {label} ({unit}) ---\n"
                f"Total: {total:,.1f}  |  Daily average: {avg:,.1f}\n"
                f"{_date_summary(stats, 'sum')}"
            )
    return "\n\n".join(sections) if sections else "No energy/activity data available."


@mcp.tool()
def get_nutrition(start_date: Optional[str] = None, end_date: Optional[str] = None) -> str:
    """
    Returns daily nutritional intake: energy (kcal), protein, carbs, fat.
    Optionally filter by date range (YYYY-MM-DD).
    """
    sections = []
    for short, label in [
        ("dietary_energy",  "Energy (kcal)"),
        ("dietary_protein", "Protein (g)"),
        ("dietary_carbs",   "Carbohydrates (g)"),
        ("dietary_fat",     "Total fat (g)"),
    ]:
        stats = _daily_stats(short, start_date, end_date)
        if stats is not None and not stats.empty:
            sections.append(f"--- {label} ---\n{_date_summary(stats, 'sum')}")
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
    if metric == "sleep":
        df = _get_frame(metric, start_date, end_date)
        if df is None or df.empty:
            return f"No data for '{metric}'."
        df = _filter_dates(df, start_date, end_date)
        daily = df.groupby("date")["value"].agg(aggregation).reset_index()
        daily.columns = ["date", "value"]
        return daily.to_string(index=False)

    if aggregation not in _DAILY_STATS:
        return f"Unsupported aggregation '{aggregation}'. Use one of: {', '.join(_DAILY_STATS)}"
    stats = _daily_stats(metric, start_date, end_date)
    if stats is None:
        available = ", ".join(SHORT_NAMES)
        return f"Unknown metric '{metric}'. Available: {available}"
    if stats.empty:
        return f"No data for '{metric}'."
    return _date_summary(stats, aggregation)


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
    # Names Health Auto Export 10.x actually sends for the same metrics
    "dietary_energy":                    "dietary_energy",
    "protein":                           "dietary_protein",
    "carbohydrates":                     "dietary_carbs",
    "total_fat":                         "dietary_fat",
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
    if not _INGEST_SECRET:
        return False
    api_key = request.headers.get("x-api-key") or request.query_params.get("api_key", "")
    return hmac.compare_digest(api_key.encode(), _INGEST_SECRET.encode())


_UPSERT_BATCH_ROWS = 65_536


def _upsert_parquet(short: str, new_rows: list[dict]) -> int:
    """Merge new_rows into the Parquet for `short`; a new row replaces any
    existing row with the same startDate. Returns rows added.

    The existing file is streamed in batches into a temp file that then
    replaces it, so memory scales with the batch size, not the file: loading
    heart_rate (1.69M rows) whole into pandas peaked at ~+300MB. Both inputs
    are sorted by startDate, so interleaving per batch keeps the output sorted.
    """
    if not new_rows:
        return 0

    new_df = pd.DataFrame(new_rows)
    new_df["startDate"] = pd.to_datetime(new_df["startDate"], utc=True, errors="coerce")
    new_df["endDate"]   = pd.to_datetime(new_df["endDate"],   utc=True, errors="coerce")
    new_df["value"]     = pd.to_numeric(new_df["value"], errors="coerce")
    new_df["date"]      = new_df["startDate"].dt.date.astype(str)
    new_df = (new_df.drop_duplicates(subset=["startDate"], keep="last")
                    .sort_values("startDate", ignore_index=True))

    path = DATA_DIR / f"{short}.parquet"
    if not path.exists():
        new_df.to_parquet(path, index=False, row_group_size=_UPSERT_BATCH_ROWS)
        return len(new_df)

    # pre_buffer=False: don't read ahead whole row groups (~30MB on heart_rate).
    pf = pq.ParquetFile(path, pre_buffer=False)
    schema = pf.schema_arrow
    new_tbl = pa.Table.from_pandas(new_df, preserve_index=False)
    new_tbl = pa.table(
        [new_tbl.column(f.name).cast(f.type, safe=False) if f.name in new_tbl.column_names
         else pa.nulls(new_tbl.num_rows, f.type) for f in schema],
        schema=schema,
    )
    new_starts = new_tbl.column("startDate")

    tmp = path.with_name(path.name + ".tmp")
    written = 0
    taken = 0
    with pq.ParquetWriter(tmp, schema) as writer:
        for batch in pf.iter_batches(batch_size=_UPSERT_BATCH_ROWS):
            starts = batch.column("startDate")
            kept = pa.Table.from_batches([batch]).filter(
                pc.invert(pc.is_in(starts, value_set=new_starts))
            )
            batch_max = pc.max(starts)
            if batch_max.is_valid:
                upto = pc.sum(pc.less_equal(new_starts, batch_max)).as_py() or 0
                chunk = new_tbl.slice(taken, max(upto - taken, 0))
                taken = max(upto, taken)
                kept = pa.concat_tables([kept, chunk]).sort_by("startDate")
            writer.write_table(kept)
            written += kept.num_rows
        rest = new_tbl.slice(taken)
        writer.write_table(rest)
        written += rest.num_rows
    os.replace(tmp, path)

    return max(written - pf.metadata.num_rows, 0)


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

    body = await request.body()
    try:
        payload = json.loads(body)
    except Exception:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)

    # Save raw payload for debugging (overwritten each call). The bytes as
    # received: re-dumping with indent doubled the size and the memory.
    DATA_DIR.mkdir(exist_ok=True)
    _LAST_PAYLOAD_PATH.write_bytes(body)
    del body

    updated: dict[str, int] = {}
    data_root = payload.get("data", payload)

    metrics = data_root.get("metrics", [])
    for metric in metrics:
        # pop: each metric's samples are freed once processed instead of the
        # whole parsed payload staying alive until the request ends.
        data = metric.pop("data", None) or []
        hae_name = metric.get("name", "")

        if hae_name == "sleep_analysis":
            sleep_rows = []
            for sample in data:
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
        samples = []
        for sample in data:
            date_str = sample.get("date") or sample.get("startDate")
            if not date_str:
                continue
            # HAE sends qty for most metrics; heart_rate sends Avg/Min/Max.
            # `is not None`, not `or`: a real 0 (e.g. a day with no food logged) must be kept.
            value = next((sample[k] for k in ("qty", "Avg", "value") if sample.get(k) is not None), None)
            if value is None:
                continue
            samples.append((date_str, sample.get("endDate", date_str), float(value),
                            sample.get("source", "HealthAutoExport")))

        # Dates are parsed per metric in one call: per-sample pd.to_datetime
        # re-guessed the format every time (~1.3ms each, minutes per backfill).
        starts = pd.to_datetime([s[0] for s in samples], utc=True, errors="coerce")
        ends   = pd.to_datetime([s[1] for s in samples], utc=True, errors="coerce")
        rows = [
            {"startDate": start.isoformat(), "endDate": end.isoformat(),
             "value": value, "unit": unit, "sourceName": source}
            for (_, _, value, source), start, end in zip(samples, starts, ends)
            if not pd.isna(start)
        ]

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
