"""
Preprocessor: parses Apple Health export.xml once and saves Parquet files.
Run this script whenever you export new data from Apple Health.

Usage:
    python preprocess.py
    python preprocess.py --export /path/to/exportación.xml
"""

import argparse
import sys
import time
from collections import defaultdict
from pathlib import Path

from lxml import etree
import pandas as pd

from config import HK_TYPE_MAP, CATEGORY_TYPES

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DEFAULT_EXPORT = Path(__file__).parent.parent / "apple_health_export" / "exportación.xml"
OUTPUT_DIR = Path(__file__).parent / "data"


def parse_and_save(export_path: Path, output_dir: Path) -> None:
    output_dir.mkdir(exist_ok=True)
    t0 = time.time()

    print(f"Parsing {export_path} ...")
    records: dict[str, list] = defaultdict(list)
    workouts: list = []
    me_info: dict = {}

    context = etree.iterparse(str(export_path), events=("end",), tag=("Record", "Workout", "Me"))

    for _event, elem in context:
        tag = elem.tag

        if tag == "Me":
            me_info = dict(elem.attrib)

        elif tag == "Record":
            hk_type = elem.get("type", "")
            if hk_type in HK_TYPE_MAP:
                records[hk_type].append({
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

    # -----------------------------------------------------------------------
    # Save each type as Parquet
    # -----------------------------------------------------------------------
    saved = []
    for hk_type, rows in records.items():
        short = HK_TYPE_MAP[hk_type]
        df = pd.DataFrame(rows)
        df["startDate"] = pd.to_datetime(df["startDate"], utc=True, errors="coerce")
        df["endDate"]   = pd.to_datetime(df["endDate"],   utc=True, errors="coerce")
        if hk_type not in CATEGORY_TYPES:
            df["value"] = pd.to_numeric(df["value"], errors="coerce")
        # else: keep value as string (e.g. sleep stage names)

        # --- Sleep-specific cleanup -------------------------------------------
        if short == "sleep":
            before = len(df)
            # 1. Drop exact duplicates
            df = df.drop_duplicates(subset=["startDate", "endDate", "value", "sourceName"])
            # 2. For Zepp Life, keep only the record with the latest endDate per
            #    (startDate, value) — Zepp writes progressive updates to the same
            #    session as the night goes on, each with the same startDate but a
            #    later endDate, so summing them inflates totals dramatically.
            zepp_mask = df["sourceName"] == "Zepp Life"
            zepp = df[zepp_mask].copy()
            other = df[~zepp_mask]
            zepp = (
                zepp.sort_values("endDate")
                    .drop_duplicates(subset=["startDate", "value", "sourceName"], keep="last")
            )
            # 3. Merge overlapping Zepp Life intervals per (night, value).
            #    Zepp progressively appends overlapping segments, so a naive sum
            #    double-counts the overlapping time. Replace with merged intervals.
            def _merge_intervals(group: pd.DataFrame) -> pd.DataFrame:
                g = group.sort_values("startDate")
                merged = []
                cur_start = g.iloc[0]["startDate"]
                cur_end   = g.iloc[0]["endDate"]
                template  = g.iloc[0].to_dict()
                for _, row in g.iloc[1:].iterrows():
                    if row["startDate"] <= cur_end:          # overlapping
                        cur_end = max(cur_end, row["endDate"])
                    else:
                        r = dict(template)
                        r["startDate"] = cur_start
                        r["endDate"]   = cur_end
                        merged.append(r)
                        cur_start = row["startDate"]
                        cur_end   = row["endDate"]
                r = dict(template)
                r["startDate"] = cur_start
                r["endDate"]   = cur_end
                merged.append(r)
                return pd.DataFrame(merged)

            zepp["_night"] = zepp.apply(
                lambda r: (pd.Timestamp(r["startDate"]).date() - pd.Timedelta(days=1)).isoformat()
                if pd.Timestamp(r["startDate"]).hour < 12 else pd.Timestamp(r["startDate"]).date().isoformat(),
                axis=1,
            )
            merged_parts = []
            for (night, val), grp in zepp.groupby(["_night", "value"]):
                merged_parts.append(_merge_intervals(grp))
            zepp_merged = pd.concat(merged_parts, ignore_index=True).drop(columns=["_night"], errors="ignore")
            df = pd.concat([other, zepp_merged], ignore_index=True)

            # 4. Drop Zepp Life InBed records > 15h (device-on session, not real in-bed time)
            duration_h = (df["endDate"] - df["startDate"]).dt.total_seconds() / 3600
            mask_inbed = (
                (df["value"] == "HKCategoryValueSleepAnalysisInBed")
                & (df["sourceName"] == "Zepp Life")
                & (duration_h > 15)
            )
            df = df[~mask_inbed]
            dropped = before - len(df)
            if dropped:
                print(f"    (sleep: removed {dropped:,} duplicate/junk rows)")
        # ----------------------------------------------------------------------

        df["date"]      = df["startDate"].dt.date.astype(str)   # store as str for Parquet compat
        path = output_dir / f"{short}.parquet"
        df.to_parquet(path, index=False)
        saved.append((short, len(df)))
        print(f"  ✓ {short:<35} {len(df):>8,} rows → {path.name}")

    # -----------------------------------------------------------------------
    # Save workouts
    # -----------------------------------------------------------------------
    if workouts:
        wdf = pd.DataFrame(workouts)
        wdf["startDate"]        = pd.to_datetime(wdf["startDate"], utc=True, errors="coerce")
        wdf["endDate"]          = pd.to_datetime(wdf["endDate"],   utc=True, errors="coerce")
        wdf["duration_min"]     = pd.to_numeric(wdf["duration_min"], errors="coerce")
        wdf["totalDistance"]    = pd.to_numeric(wdf["totalDistance"], errors="coerce")
        wdf["totalEnergy_kcal"] = pd.to_numeric(wdf["totalEnergy_kcal"], errors="coerce")
        wdf["avgHeartRate"]      = pd.to_numeric(wdf["avgHeartRate"], errors="coerce")
        wdf["maxHeartRate"]      = pd.to_numeric(wdf["maxHeartRate"], errors="coerce")
        wdf["date"]             = wdf["startDate"].dt.date.astype(str)
        path = output_dir / "workouts.parquet"
        wdf.to_parquet(path, index=False)
        print(f"  ✓ {'workouts':<35} {len(wdf):>8,} rows → {path.name}")

    # -----------------------------------------------------------------------
    # Save Me metadata
    # -----------------------------------------------------------------------
    if me_info:
        pd.DataFrame([me_info]).to_parquet(output_dir / "me.parquet", index=False)
        print(f"  ✓ me.parquet")

    elapsed = time.time() - t0
    total_rows = sum(n for _, n in saved)
    print(f"\nDone in {elapsed:.1f}s — {total_rows:,} records across {len(saved)} metrics + {len(workouts)} workouts")
    print(f"Output: {output_dir}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Preprocess Apple Health XML → Parquet")
    parser.add_argument("--export", type=Path, default=DEFAULT_EXPORT,
                        help="Path to exportación.xml")
    parser.add_argument("--output", type=Path, default=OUTPUT_DIR,
                        help="Output directory for Parquet files")
    args = parser.parse_args()

    if not args.export.exists():
        print(f"ERROR: Export file not found: {args.export}", file=sys.stderr)
        sys.exit(1)

    parse_and_save(args.export, args.output)
