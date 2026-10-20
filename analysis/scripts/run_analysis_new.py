"""
analysis/scripts/run_analysis_new.py

What it does
------------
Runs every bout-level, occupancy, co-occupancy, and -- if configured --
spatial proximity/locomotion metric for every animal listed in an
analysis config YAML, and writes the results as CSV files.

Why it exists
-------------
This is the single command that turns the pipeline's existing per-animal
lmt_binary_search_A<id>_*.sqlite outputs into the metrics described in
the accompanying analysis writeup, without requiring manual per-animal,
per-metric function calls in a notebook.

Inputs
------
--config PATH : an analysis_config.yaml (see analysis/config/
    analysis_config.yaml for the expected shape and
    analysis/src/config.py for validation rules).
--output-dir PATH : optional. Overrides the config's own "output_dir"
    as the PARENT output directory for this run. Either way, a NEW
    timestamped subdirectory is created inside that parent for every
    execution (see analysis.src.run_utils.create_run_output_dir) --
    this flag never causes results to be written directly into the
    parent directory itself.

Outputs
-------
Written under a freshly created <parent_output_dir>/<run_timestamp>/
directory (parent is --output-dir if given, else the config's own
"output_dir" -- analysis/outputs/ by default), all as CSV:
    per_animal_summary.csv       -- one row per animal: time-in-nest,
                                     entry/exit counts, bout-duration
                                     stats, fill-source composition.
    occupancy_timeline_<id>.csv  -- one file per animal, binned occupancy
                                     fractions over time.
    entry_exit_events.csv        -- all animals' entry/exit events,
                                     concatenated, for a raster plot.
    co_occupancy_summary.csv     -- one row per requested dyad/group
                                     (dam-dam, babysitter-babysitter,
                                     every dam-babysitter pair, all
                                     adults together).
    co_occupancy_all_summary.csv -- group-size breakdown across ALL
                                     adults: seconds with exactly k
                                     adults in the nest (k = 0..N, so
                                     k=0 is "nobody in the nest"), plus,
                                     for every subgroup of
                                     EXACT_GROUP_SIZE adults (3 by
                                     default), the seconds EXACTLY that
                                     subgroup was in the nest with every
                                     other adult out ("exactly 3, and
                                     which 3"). See
                                     build_co_occupancy_all_summary().
    group_occupancy_profile.csv  -- one row per frame: FRAMENUMBER,
                                     n_in_nest, and one 1/0/-1 column per
                                     adult showing exactly which
                                     animal(s) were in the nest (see
                                     analysis.src.co_occupancy.
                                     group_occupancy_table()'s own
                                     docstring for why -1 is preserved,
                                     not collapsed to 0).
    proximity_summary.csv        -- (only if processed_detection_sqlite
                                     and proximity_contact_threshold are
                                     set) one row per animal pair.
    locomotion_summary.csv       -- (only if processed_detection_sqlite
                                     is set) one row per animal.

Logic
-----
1. Load and validate the config.
2. For each animal: load its GAP_FILL_ANALYSIS table, compute its bout
   table once, and derive every per-animal metric from that one bout
   table / per-frame table.
3. Merge all animals' outputs into one occupancy matrix and compute every
   configured co-occupancy dyad/group from it, plus the all-adults
   group-size summary.
4. If a shared processed_detection_sqlite is configured, compute spatial
   metrics for every animal pair and every animal individually.
5. Write every result table to <output_dir> as CSV.

Assumptions
-----------
See each analysis/src module's own docstring for the assumptions behind
its specific metric. This script itself assumes every animal in the
config belongs to the SAME recording session (required by
analysis.src.co_occupancy_v2.build_occupancy_matrix -- not checked here,
see that function's own docstring).

Failure modes
-------------
Any missing/invalid config field, or any SQLite file that doesn't match
the expected schema, raises immediately with a clear error message (from
analysis.src.config.load_config or analysis.src.io.load_gap_fill_analysis
respectively) rather than continuing on incomplete data.

Validation
----------
See "How to run" / "How to test" in analysis/README.md. In brief: run
against a real animal's file and cross-check per_animal_summary.csv's
seconds_unresolved against that animal's own LMT_Summary_A<id>_*.txt
report from 2.lmt_binary_search.py.

For co_occupancy_all_summary.csv: the "group_size" rows' seconds must sum
to total_resolved_seconds, and the "exactly_N_which" rows' seconds must
sum to the "group_size" row for n_in_nest == N.

Integration
-----------
This is the top-level entry point for this analysis layer; it does not
modify any existing pipeline script (0-4, lmt_common.py) and only reads
their output files.

Requires analysis/src/co_occupancy_v2.py (the extended co-occupancy
module) to be present alongside the other analysis/src modules.
"""

import argparse
import itertools
import pathlib
import sys

import pandas as pd

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from analysis.src.config import load_config, animals_by_role
from analysis.src.io import load_gap_fill_analysis, load_positions
from analysis.src.bouts import (
    compute_nest_bouts,
    bout_duration_summary,
    count_entries_exits,
    entry_exit_events,
)
from analysis.src.occupancy import (
    total_time_in_nest,
    occupancy_timeline,
    fill_source_composition,
)
from analysis.src.co_occupancy_v2 import (
    build_occupancy_matrix,
    co_occupancy_seconds,
    group_occupancy_table,
    group_size_summary,
    exact_subgroup_co_occupancy,
)
from analysis.src.spatial import pairwise_distance, proximity_summary, locomotor_distance
from analysis.src.run_utils import create_run_output_dir

# Subgroup size for the "exactly N together, and which N" rows in
# co_occupancy_all_summary.csv.
EXACT_GROUP_SIZE = 3


def run_per_animal_metrics(config: dict) -> dict:
    """
    What it does
    ------------
    Loads every configured animal's GAP_FILL_ANALYSIS table and computes
    every per-animal metric (Metrics 1-4, 6, 9) from it.

    Outputs
    -------
    dict with:
        "summary_df": pandas.DataFrame, one row per animal.
        "timelines": dict[animal_id, pandas.DataFrame] (Metric 4 output).
        "events_df": pandas.DataFrame, all animals' entry/exit events
            concatenated (Metric 9 output, ready for a raster plot).

    Integration
    -----------
    Called once by main(); its outputs are written to CSV there.
    """
    summary_rows = []
    timelines = {}
    all_events = []

    bin_seconds = config["occupancy_timeline_bin_seconds"]

    for animal_id, animal_cfg in config["animals"].items():
        df = load_gap_fill_analysis(animal_cfg["gap_fill_sqlite"])
        bouts = compute_nest_bouts(df, animal_id)

        time_stats = total_time_in_nest(bouts)
        entry_exit_stats = count_entries_exits(bouts)
        in_nest_duration_stats = bout_duration_summary(bouts, state=1)
        out_of_nest_duration_stats = bout_duration_summary(bouts, state=0)

        row = {
            "animal_id": animal_id,
            "role": animal_cfg["role"],
            **time_stats,
            **entry_exit_stats,
            "in_nest_bout_median_sec": in_nest_duration_stats.get("median_sec"),
            "in_nest_bout_mean_sec": in_nest_duration_stats.get("mean_sec"),
            "out_of_nest_bout_median_sec": out_of_nest_duration_stats.get("median_sec"),
            "out_of_nest_bout_mean_sec": out_of_nest_duration_stats.get("mean_sec"),
        }

        if "FILL_SOURCE" in df.columns:
            fill_comp = fill_source_composition(df)
            for source_name, fraction in fill_comp["FRACTION"].items():
                row[f"fill_source_frac_{source_name}"] = fraction
        else:
            print(
                f"[run_analysis] NOTE: animal {animal_id}'s input has "
                "no FILL_SOURCE column (likely a 1.lmt_gap_fill.py-only "
                "file, not 2.lmt_binary_search.py's output) -- skipping "
                "fill-source composition for this animal."
            )

        summary_rows.append(row)
        timelines[animal_id] = occupancy_timeline(df, bin_seconds=bin_seconds)

        label = f"{animal_cfg['role']}_{animal_id}"
        all_events.append(entry_exit_events(bouts, animal_label=label))

    summary_df = pd.DataFrame(summary_rows)
    events_df = (
        pd.concat(all_events, ignore_index=True) if all_events else pd.DataFrame()
    )
    return {"summary_df": summary_df, "timelines": timelines, "events_df": events_df}


def build_co_occupancy_all_summary(matrix: pd.DataFrame, all_adults: list,
                                   exact_size: int = EXACT_GROUP_SIZE) -> pd.DataFrame:
    """
    What it does
    ------------
    Builds the table written to co_occupancy_all_summary.csv: how the
    full adult group distributes over group sizes, and which specific
    subgroup was present when exactly `exact_size` adults were together.

    Outputs
    -------
    DataFrame with columns:
        analysis -- "group_size" (one row per k = 0..N adults in nest) or
            "exactly_<exact_size>_which" (one row per subgroup).
        group -- "exactly_<k>_in_nest" for group_size rows; the subgroup's
            members joined with "+" for exactly_N_which rows.
        n_in_nest -- k for group_size rows; exact_size for the others.
        members_in -- comma-separated members confirmed in the nest
            (blank for group_size rows, which don't name animals).
        members_out -- comma-separated adults confirmed out of the nest
            (blank for group_size rows).
        seconds -- seconds in that state.
        total_resolved_seconds -- frames where EVERY adult was resolved,
            as seconds (the shared denominator for all rows).
        fraction_of_resolved_time -- seconds / total_resolved_seconds.

    Logic
    -----
    Both parts only count frames where every adult is resolved (no -1),
    because "exactly k" and "exactly these" can't be claimed if any
    adult's state is unknown. The exactly_N_which part is skipped when
    fewer than exact_size adults are configured.
    """
    size_df = group_size_summary(matrix, all_adults)
    total_resolved = float(size_df["seconds"].sum())

    size_rows = pd.DataFrame({
        "analysis": "group_size",
        "group": [f"exactly_{k}_in_nest" for k in size_df["n_in_nest"]],
        "n_in_nest": size_df["n_in_nest"],
        "members_in": "",
        "members_out": "",
        "seconds": size_df["seconds"],
        "total_resolved_seconds": total_resolved,
        "fraction_of_resolved_time": size_df["fraction_of_resolved_time"],
    })

    parts = [size_rows]

    if len(all_adults) >= exact_size:
        exact_df = exact_subgroup_co_occupancy(matrix, all_adults, size=exact_size)
        exact_rows = pd.DataFrame({
            "analysis": f"exactly_{exact_size}_which",
            "group": exact_df["group"],
            "n_in_nest": exact_size,
            "members_in": exact_df["group"].str.replace("+", ",", regex=False),
            "members_out": exact_df["absent"].str.replace("+", ",", regex=False),
            "seconds": exact_df["seconds_exactly_these"],
            "total_resolved_seconds": exact_df["total_resolved_seconds"],
            "fraction_of_resolved_time": exact_df["fraction_of_resolved_time"],
        })
        parts.append(exact_rows)
    else:
        print(
            f"[run_analysis] NOTE: only {len(all_adults)} adult(s) configured "
            f"-- skipping the exactly-{exact_size}-together rows."
        )

    return pd.concat(parts, ignore_index=True)


def run_co_occupancy_metrics(config: dict) -> dict:
    """
    What it does
    ------------
    Builds the merged occupancy matrix across all configured animals and
    computes co-occupancy (Metric 5) for every dam-dam, babysitter-
    babysitter, and dam-babysitter pairing present, plus the full-group
    profile and the all-adults group-size summary.

    Outputs
    -------
    dict with "summary_df" (one row per dyad/group), "profile"
    (pandas.DataFrame from group_occupancy_table(), or None if fewer
    than 2 adults are configured), and "all_summary_df" (from
    build_co_occupancy_all_summary(), or None if fewer than 2 adults).

    Logic
    -----
    Dyads/groups are DERIVED from each animal's configured "role", not
    hard-coded animal IDs -- this generalizes to any number of dams/
    babysitters the config declares, not just the 2+2 example.
    """
    animal_files = {
        str(aid): cfg["gap_fill_sqlite"] for aid, cfg in config["animals"].items()
    }
    matrix = build_occupancy_matrix(animal_files)

    dams = [str(a) for a in animals_by_role(config, "dam")]
    babysitters = [str(a) for a in animals_by_role(config, "babysitter")]
    all_adults = dams + babysitters

    groups_to_compute = {}
    if len(dams) >= 2:
        for a, b in itertools.combinations(dams, 2):
            groups_to_compute[f"dam_{a}_dam_{b}"] = [a, b]
    if len(babysitters) >= 2:
        for a, b in itertools.combinations(babysitters, 2):
            groups_to_compute[f"babysitter_{a}_babysitter_{b}"] = [a, b]
    for d, b in itertools.product(dams, babysitters):
        groups_to_compute[f"dam_{d}_babysitter_{b}"] = [d, b]
    if len(all_adults) >= 2:
        groups_to_compute["all_adults"] = all_adults

    rows = []
    for group_name, labels in groups_to_compute.items():
        result = co_occupancy_seconds(matrix, labels)
        rows.append({
            "group": group_name,
            "labels": ",".join(labels),
            "seconds_all_together": result["seconds_all_together"],
            "total_resolved_seconds": result["total_resolved_seconds"],
            "fraction_of_resolved_time": result["fraction_of_resolved_time"],
        })

    profile = (
    group_occupancy_table(matrix, all_adults)
    if len(all_adults) >= 2 else None
    )

    # Generate the group-size summary even when only one adult is configured.
    # This produces rows for 0 animals and 1 animal in the nest.
    all_summary_df = (
    build_co_occupancy_all_summary(matrix, all_adults)
    if len(all_adults) >= 1 else None
    )

    return {
        "summary_df": pd.DataFrame(rows),
        "profile": profile,
        "all_summary_df": all_summary_df,
    }


def run_spatial_metrics(config: dict) -> dict:
    """
    What it does
    ------------
    Computes proximity (Metric 7) for every animal pair and locomotion
    (Metric 8) for every animal, IF the config provides
    processed_detection_sqlite.

    Outputs
    -------
    dict with "proximity_df" and "locomotion_df", each possibly empty if
    the required config fields are absent.
    """
    detection_path = config.get("processed_detection_sqlite")
    if not detection_path:
        print(
            "[run_analysis] NOTE: processed_detection_sqlite not set "
            "in config -- skipping spatial metrics (proximity, locomotion)."
        )
        return {"proximity_df": pd.DataFrame(), "locomotion_df": pd.DataFrame()}

    animal_ids = list(config["animals"].keys())
    positions = load_positions(detection_path, animal_ids)

    locomotion_rows = []
    for animal_id in animal_ids:
        stats = locomotor_distance(positions, animal_id)
        locomotion_rows.append({"animal_id": animal_id, **stats})
    locomotion_df = pd.DataFrame(locomotion_rows)

    contact_threshold = config.get("proximity_contact_threshold")
    proximity_rows = []
    if contact_threshold is not None:
        for id_a, id_b in itertools.combinations(animal_ids, 2):
            distance = pairwise_distance(positions, id_a, id_b)
            stats = proximity_summary(distance, contact_threshold)
            proximity_rows.append({"animal_a": id_a, "animal_b": id_b, **stats})
    else:
        print(
            "[run_analysis] NOTE: proximity_contact_threshold not set "
            "in config -- computed locomotion but skipped proximity."
        )
    proximity_df = pd.DataFrame(proximity_rows)

    return {"proximity_df": proximity_df, "locomotion_df": locomotion_df}


def main():
    parser = argparse.ArgumentParser(
        description="Run nest-occupancy and social metrics from an "
                    "analysis_config.yaml."
    )
    parser.add_argument(
        "--config", required=True,
        help="Path to analysis_config.yaml (see analysis/config/analysis_config.yaml).",
    )
    parser.add_argument(
        "--output-dir", default=None,
        help="Optional. Overrides the config's own output_dir as the PARENT "
             "directory for this run. A new timestamped subdirectory is always "
             "created inside it (or inside the config's output_dir, if this "
             "flag is omitted) -- results are never written directly into the "
             "parent directory itself.",
    )
    args = parser.parse_args()

    config = load_config(args.config)
    parent_output_dir = pathlib.Path(args.output_dir) if args.output_dir else config["output_dir"]
    output_dir = create_run_output_dir(parent_output_dir)

    print(f"[run_analysis] Loaded config for {len(config['animals'])} animal(s).")
    print(f"[run_analysis] Output directory: {output_dir}")

    per_animal = run_per_animal_metrics(config)
    per_animal["summary_df"].to_csv(output_dir / "per_animal_summary.csv", index=False)
    for animal_id, timeline_df in per_animal["timelines"].items():
        timeline_df.to_csv(output_dir / f"occupancy_timeline_{animal_id}.csv", index=False)
    per_animal["events_df"].to_csv(output_dir / "entry_exit_events.csv", index=False)
    print(f"[run_analysis] Wrote per-animal metrics for "
          f"{len(per_animal['summary_df'])} animal(s).")

    co_occ = run_co_occupancy_metrics(config)
    co_occ["summary_df"].to_csv(output_dir / "co_occupancy_summary.csv", index=False)
    if co_occ["profile"] is not None:
        co_occ["profile"].to_csv(output_dir / "group_occupancy_profile.csv", index=False)
    if co_occ["all_summary_df"] is not None:
        co_occ["all_summary_df"].to_csv(output_dir / "co_occupancy_all_summary.csv", index=False)
        print(f"[run_analysis] Wrote {len(co_occ['all_summary_df'])} row(s) to "
              "co_occupancy_all_summary.csv.")
    print(f"[run_analysis] Wrote {len(co_occ['summary_df'])} co-occupancy row(s).")

    spatial = run_spatial_metrics(config)
    if not spatial["proximity_df"].empty:
        spatial["proximity_df"].to_csv(output_dir / "proximity_summary.csv", index=False)
        print(f"[run_analysis] Wrote proximity summary for "
              f"{len(spatial['proximity_df'])} pair(s).")
    if not spatial["locomotion_df"].empty:
        spatial["locomotion_df"].to_csv(output_dir / "locomotion_summary.csv", index=False)
        print(f"[run_analysis] Wrote locomotion summary for "
              f"{len(spatial['locomotion_df'])} animal(s).")

    print("[run_analysis] Done.")


if __name__ == "__main__":
    main()