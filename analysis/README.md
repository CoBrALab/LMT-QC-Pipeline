# Analysis Layer

This directory contains nest-occupancy and social metrics built on top
of the existing LMT QC pipeline's outputs. It does not
modify `0.Preprocessing.py`, `1.lmt_gap_fill.py`, `2.lmt_binary_search.py`,
`3.lmt_qc_sampler.py`, `4.lmt_qc_validator.py`, or `lmt_common.py`.

## Contents

- [What it computes](#what-it-computes)
- [Structure](#structure)
- [Installing dependencies](#installing-dependencies)
- [How to run](#how-to-run)
- [Output directory behavior](#output-directory-behavior)
- [Output tables: full column reference](#output-tables-full-column-reference)
- [How to test](#how-to-test)

## What it computes

| Metric | Module | Function(s) |
|---|---|---|
| 1. Total time in nest | `src/occupancy.py` | `total_time_in_nest` |
| 2. Nest entry/exit counts | `src/bouts.py` | `count_entries_exits` |
| 3. Bout duration distribution | `src/bouts.py` | `bout_duration_summary` |
| 4. Occupancy timeline | `src/occupancy.py` | `occupancy_timeline` |
| 5. Co-occupancy (dam-dam, babysitter-babysitter, dam-babysitter, group) | `src/co_occupancy.py` | `build_occupancy_matrix`, `co_occupancy_seconds`, `group_occupancy_table` |
| 6. Fill-source / tracking-confidence composition | `src/occupancy.py` | `fill_source_composition` |
| 7. Inter-animal proximity | `src/spatial.py` | `pairwise_distance`, `proximity_summary` |
| 8. Locomotor activity | `src/spatial.py` | `locomotor_distance` |
| 9. Entry/exit event log (raster data) | `src/bouts.py` | `entry_exit_events` |

Every function has a full docstring (what it does / why it exists /
inputs / outputs / logic / assumptions / failure modes / validation /
integration). This README
summarizes the same information at the output-table level; the
docstrings are the authoritative source if the two ever disagree.

## Structure

```text
analysis/
├── src/
│   ├── io.py            # SQLite loaders shared by every module
│   ├── bouts.py          # Bout detection, entries/exits, bout duration
│   ├── occupancy.py       # Time-in-nest, occupancy timeline, fill-source QC
│   ├── co_occupancy.py    # Multi-animal simultaneous-occupancy merge/metrics
│   ├── spatial.py         # Proximity and locomotion (raw MASS_X/MASS_Y)
│   ├── config.py          # YAML config loading/validation
│   └── run_utils.py       # Timestamped run-output-directory helper
├── scripts/
│   └── run_analysis.py   # CLI entry point
├── tests/
│   ├── conftest.py             # Synthetic SQLite fixtures
│   ├── test_io.py
│   ├── test_bouts.py
│   ├── test_occupancy.py
│   ├── test_co_occupancy.py
│   ├── test_spatial.py
│   ├── test_config.py
│   ├── test_run_utils.py       # Timestamped-directory unit tests
│   └── test_integration.py     # Full CLI run against synthetic data
├── config/
│   └── analysis_config.yaml    # Example config -- edit before use
├── outputs/               # Default parent for timestamped run folders (gitignored)
└── README.md
```

## How to run

1. In `analysis/config/analysis_config.yaml`, fill in your actual
   animal IDs, roles, and each animal's
   `lmt_binary_search_A<id>_<timestamp>.sqlite` path (script 2's output). Set
   `processed_detection_sqlite` and `proximity_contact_threshold` too if
   you want the spatial metrics (Metrics 7-8); leave them as `null` to
   skip those.
2. From the repo root:

```bash
uv run python analysis/scripts/run_analysis.py \
    --config analysis/config/analysis_config.yaml
```

**What successful execution looks like:** the script prints one
`[run_analysis]` line per stage (config loaded, output directory,
per-animal metrics written, co-occupancy rows written, spatial summaries
written if configured) and ends with `[run_analysis] Done.`, exit
code 0.

### Example run

```bash
$ uv run python analysis/scripts/run_analysis.py --config analysis/config/analysis_config.yaml
[run_analysis] Loaded config for 4 animal(s).
[run_analysis] Output directory: analysis/outputs/2026-08-27_19-15-23
[run_analysis] Wrote per-animal metrics for 4 animal(s).
[run_analysis] Wrote 6 co-occupancy row(s).
[run_analysis] Wrote proximity summary for 6 pair(s).
[run_analysis] Wrote locomotion summary for 4 animal(s).
[run_analysis] Done.
```

### Example run with a custom output location

```bash
$ uv run python analysis/scripts/run_analysis.py \
    --config analysis/config/analysis_config.yaml \
    --output-dir /data/lmt_results/2026-cohort-3
[run_analysis] Loaded config for 4 animal(s).
[run_analysis] Output directory: /data/lmt_results/2026-cohort-3/2026-08-27_19-16-04
...
```

If it fails instead, the error is a clear `ValueError` naming the
specific config field or SQLite schema problem (see `src/config.py` and
`src/io.py`'s own "Failure modes" docstring sections).

## Output directory behavior

**Every run of `run_analysis.py` creates a new, timestamped
subdirectory and writes all of that run's output files inside it.**
Results are never written directly into a parent output directory.

```text
analysis/
└── outputs/
    └── 2026-08-27_19-06-04/
        ├── per_animal_summary.csv
        ├── occupancy_timeline_101.csv
        ├── ...
        └── group_occupancy_profile.csv
```

- **Default parent directory:** `analysis/outputs/` (via the config
  file's own `output_dir` key, resolved relative to the config file's
  location -- unchanged from before).
- **Custom parent directory:** pass `--output-dir /path/to/output` to
  override the config's `output_dir` as the parent. This is still a
  *parent* directory, not the final location -- a new timestamped
  subdirectory is created inside it exactly the same way:

  ```bash
  uv run python analysis/scripts/run_analysis.py \
      --config analysis/config/analysis_config.yaml \
      --output-dir "/path/to/output"
  # writes into /path/to/output/2026-08-27_19-06-04/
  ```

- **Folder naming:** `YYYY-MM-DD_HH-MM-SS` (second precision), e.g.
  `2026-08-27_19-06-04`. If two runs start within the same second
  against the same parent directory (rare, but possible; e.g. two
  runs launched back-to-back in a script), a numeric suffix is appended
  (`_2`, `_3`, ...) so no run ever overwrites another's output. This is
  implemented once, in `src/run_utils.py::create_run_output_dir`, and
  used by every CLI script in this directory (see that function's own
  docstring for the exact collision-handling logic).
- The parent directory (`analysis/outputs/` or your `--output-dir`
  value) is created automatically if it doesn't already exist, but is
  **never** written into directly, only the timestamped subdirectory
  inside it receives output files.

## Output tables: full column reference

Every table below is written by `run_analysis.py`'s `main()` into
that run's timestamped output directory. "How calculated" points to the
exact source function; read that function's own docstring for the full
algorithm, edge cases, and validation notes, this section gives the
column-level summary needed to interpret a CSV without re-reading the
source.

All time values are in **seconds** unless a column name says otherwise
(e.g. `*_FRAMENUMBER`). All frame-based calculations use `DB_FPS = 30`
(from the base pipeline's `lmt_common.py`) as the frame-to-seconds
conversion, not the video's own 15fps export rate.

---

### `per_animal_summary.csv`

One row per configured animal. Generated by `run_per_animal_metrics()`
in `run_analysis.py`, combining `total_time_in_nest()`,
`count_entries_exits()`, `bout_duration_summary()` (called once for
in-nest and once for out-of-nest bouts), and `fill_source_composition()`.

| Column | Meaning | How calculated | Assumptions |
|---|---|---|---|
| `animal_id` | The animal's `ANIMALID` from its LMT SQLite file | From `analysis_config.yaml`'s `animals` keys | Matches the SQLite's own `ANIMALID` exactly |
| `role` | `dam` or `babysitter` | From `analysis_config.yaml` | Assigned by the user, not inferred from data |
| `seconds_in_nest` | Total confirmed in-nest time | Sum of `DURATION_SEC` over bouts with `STATE == 1` | Excludes unresolved (`-1`) time entirely |
| `seconds_out_of_nest` | Total confirmed out-of-nest time | Sum of `DURATION_SEC` over bouts with `STATE == 0` | |
| `seconds_unresolved` | Total time LMT could not resolve as in/out of nest | Sum of `DURATION_SEC` over bouts with `STATE == -1` | Report this alongside any in-nest number (see `fraction_unresolved` below) |
| `total_seconds` | `seconds_in_nest + seconds_out_of_nest + seconds_unresolved` | Sum of the three above | |
| `fraction_in_nest` | Fraction of **resolved** time spent in nest | `seconds_in_nest / (seconds_in_nest + seconds_out_of_nest)` | Deliberately computed over resolved time only, NOT `total_seconds`, dividing by total would let a high unresolved fraction silently understate the estimate rather than flagging it as less certain |
| `fraction_unresolved` | Fraction of the whole session that's unresolved | `seconds_unresolved / total_seconds` | Always check this before trusting `fraction_in_nest`, a high value here means the in-nest estimate rests on less directly-observed data |
| `n_entries` | Count of confirmed nest entries (`0->1` transitions) | Count of bouts with `STATE == 1` and `PREV_STATE == 0` in the true (unfiltered) bout sequence | A transition immediately following an unresolved bout is excluded: it was never actually observed, so this count is a **lower bound** when `fraction_unresolved` is non-trivial |
| `n_exits` | Count of confirmed nest exits (`1->0` transitions) | Same logic, reversed | Same lower-bound caveat |
| `n_bouts_in_nest` | Count of resolved-transition in-nest bouts | Count of bouts with `STATE == 1`, any `PREV_STATE` | |
| `n_bouts_out_of_nest` | Count of resolved-transition out-of-nest bouts | Count of bouts with `STATE == 0` | |
| `in_nest_bout_median_sec` | Median duration of in-nest bouts | Median of `DURATION_SEC` for `STATE == 1` bouts with `PREV_STATE == 0` | Median, not mean, is the headline statistic. Bout durations are typically right-skewed |
| `in_nest_bout_mean_sec` | Mean duration of in-nest bouts | Mean of the same set | Report alongside the median, not instead of it |
| `out_of_nest_bout_median_sec` | Median duration of out-of-nest bouts | Same logic for `STATE == 0` | |
| `out_of_nest_bout_mean_sec` | Mean duration of out-of-nest bouts | Same logic | |
| `fill_source_frac_DETECTED` | Fraction of frames directly detected by LMT (no gap-filling) | From `2.lmt_binary_search.py`'s own `FILL_SOURCE` column | Only present if the input file has a `FILL_SOURCE` column (script 2's output) |
| `fill_source_frac_LOGIC` | Fraction resolved by gap-fill boundary logic | Same source | |
| `fill_source_frac_BINARY_SEARCH` | Fraction resolved via human-reviewed binary search | Same source | |
| `fill_source_frac_UNKNOWN` | Fraction that remains unresolved even after binary search | Same source | Corresponds to `STATE == -1` bouts above |

Note: the `fill_source_frac_*` columns are generated dynamically from
whichever `FILL_SOURCE` categories are actually present in that
animal's file. An animal with zero `UNKNOWN` frames simply won't have
a `fill_source_frac_UNKNOWN` column (its true value would be 0, but the
column is only added when the category appears at all).

---

### `occupancy_timeline_<animal_id>.csv`

One file per animal. Generated by `occupancy_timeline()`, binning the
animal's full per-frame timeline into fixed windows
(`occupancy_timeline_bin_seconds` from the config; 60s by default).

| Column | Meaning | How calculated |
|---|---|---|
| `BIN_START_SEC` | Bin's start time | `bin_index * bin_seconds` |
| `BIN_END_SEC` | Bin's end time | `(bin_index + 1) * bin_seconds` |
| `FRAC_IN_NEST` | Fraction of frames in this bin with `IN_NEST == 1` | Count of `IN_NEST == 1` frames / `N_FRAMES` |
| `FRAC_OUT_OF_NEST` | Fraction of frames in this bin with `IN_NEST == 0` | Same, for `== 0` |
| `FRAC_UNRESOLVED` | Fraction of frames in this bin with `IN_NEST == -1` | Same, for `== -1` |
| `N_FRAMES` | Frames actually present in this bin | Row count within the bin |

`FRAC_IN_NEST + FRAC_OUT_OF_NEST + FRAC_UNRESOLVED == 1.0` for every row
(within floating-point tolerance), this is asserted in the source
function itself. **The last bin is typically partial** (session length
is rarely an exact multiple of `bin_seconds`), check `N_FRAMES` before
treating every bin as equally reliable.

---

### `entry_exit_events.csv`

All animals' entries and exits, concatenated. The raw data for a
raster/timeline plot. Generated by `entry_exit_events()` per animal,
concatenated across animals in `run_per_animal_metrics()`.

| Column | Meaning | How calculated |
|---|---|---|
| `ANIMAL` | Label for this event's animal | `f"{role}_{animal_id}"` |
| `EVENT_TYPE` | `ENTRY` or `EXIT` | A bout's `STATE`/`PREV_STATE` pair (`1`/`0` = entry, `0`/`1` = exit) |
| `FRAMENUMBER` | The frame the transition occurred on | The new bout's `START_FRAME` |
| `TIME_SEC` | Same instant, in seconds | `FRAMENUMBER / DB_FPS` |

Same "observed transition only" rule as `n_entries`/`n_exits` above: an
event immediately following an unresolved bout is not included.

---

### `co_occupancy_summary.csv`

One row per dyad/group actually derivable from the configured animal
roles (every dam-dam pair, every babysitter-babysitter pair, every
dam-babysitter pair, and the full `all_adults` group if >=2 adults are
configured). Generated by `run_co_occupancy_metrics()`, calling
`co_occupancy_seconds()` once per group.

| Column | Meaning | How calculated |
|---|---|---|
| `group` | Human-readable group name | e.g. `dam_101_dam_102`, `all_adults` |
| `labels` | Comma-joined animal IDs in this group | The exact labels passed to `co_occupancy_seconds()` |
| `seconds_all_together` | Time every animal in this group was simultaneously in the nest | Frames where all group members have `IN_NEST == 1`, divided by `DB_FPS` |
| `total_resolved_seconds` | Time every animal in this group had a resolved (non `-1`) state, regardless of together/apart | Count of frames where no group member is `-1`, divided by `DB_FPS` |
| `fraction_of_resolved_time` | `seconds_all_together / total_resolved_seconds` | |

A frame only counts toward `seconds_all_together` if **every** animal in
the group is resolved at that frame. If any one of them is `-1`, that
frame is excluded from both the numerator and the denominator entirely
(not counted as "not together").

---

### `group_occupancy_profile.csv`

Shows exactly which animals are in the
nest, per frame. One row per frame in the merged
occupancy matrix (the frame range common to every configured adult; see
`build_occupancy_matrix()`). Generated by `group_occupancy_table()`.

| Column | Meaning | How calculated |
|---|---|---|
| `FRAMENUMBER` | The frame this row describes | The merged occupancy matrix's own index |
| `n_in_nest` | Count of animals **confirmed** in the nest this frame | Count of columns (below) equal to `1` |
| `<animal_id>` (one column per mice) | That animal's nest status this frame | Taken directly from that animal's `IN_NEST` value: `1` = confirmed in nest, `0` = confirmed not in nest, `-1` = unresolved at this frame |

**Important design note:** the per-animal columns are **not** strictly binary
`{0, 1}`, they preserve `-1` for an unresolved frame, matching the
`IN_NEST` convention already used everywhere else in this pipeline
(`co_occupancy_seconds`, `total_time_in_nest`, etc.). This was a
deliberate choice, not an oversight: silently converting an unresolved
animal's status to `0` would misrepresent "unknown" as "confirmed
absent," which every other part of this codebase goes out of its way to
avoid (see `fraction_unresolved` above). `n_in_nest` itself still counts
only confirmed-present (`== 1`) animals, so it's always a real, honest
number even on a row with some `-1` columns.

---

### `proximity_summary.csv`

Only written if `proximity_contact_threshold` **and**
`processed_detection_sqlite` are both set in the config. One row per
animal pair. Generated by `run_spatial_metrics()`, calling
`pairwise_distance()` then `proximity_summary()` for every pair.

| Column | Meaning | How calculated | Assumptions |
|---|---|---|---|
| `animal_a`, `animal_b` | The pair's two animal IDs | From `analysis_config.yaml` | |
| `n_valid_frames` | Frames where both animals had a detected position | Count of non-NaN frames in the distance series | A missing detection for either animal makes that frame NaN, not estimated |
| `mean_distance` | Mean centroid-to-centroid distance across valid frames | Mean of `sqrt((xa-xb)^2 + (ya-yb)^2)` | Same spatial unit as your LMT export's `MASS_X`/`MASS_Y` |
| `median_distance` | Median of the same | | |
| `seconds_in_contact` | Time the pair spent within `proximity_contact_threshold` | Count of valid frames with distance <= threshold, / `DB_FPS` | |
| `fraction_valid_time_in_contact` | Fraction of **valid** (not total) time in contact | `seconds_in_contact / (n_valid_frames / DB_FPS)` | If detection dropout is more likely exactly when animals are close together (a plausible tracking-error mechanism), this fraction is a biased, not unbiased, estimate of true contact time (see `src/spatial.py`'s own docstring) |

---

### `locomotion_summary.csv`

Only written if `processed_detection_sqlite` is set. One row per animal.
Generated by `locomotor_distance()`.

| Column | Meaning | How calculated | Assumptions |
|---|---|---|---|
| `animal_id` | The animal's ID | | |
| `total_distance` | Total path length traveled | Sum of consecutive-frame Euclidean displacement, ONLY across truly-adjacent frames (see `n_excluded_gap_segments`) | Same spatial-unit caveat as proximity above |
| `n_valid_segments` | Count of adjacent-frame pairs actually summed | | |
| `n_excluded_gap_segments` | Count of frame-pairs excluded because they weren't truly adjacent (a detection gap in between) | A displacement across a multi-frame gap is NOT summed, it would wildly underestimate true path length by assuming a straight line across however far the animal actually moved during the gap | |
| `fraction_path_observed` | `n_valid_segments / (n_valid_segments + n_excluded_gap_segments)` | | Typically **substantially lower** than the frame-resolved fraction behind the occupancy metrics. There is no gap-filling mechanism for path length the way there is for nest state. A low value means `total_distance` is a real underestimate, always report this alongside `total_distance`, never in isolation |

## How to test

From the repo root:

```bash
uv run pytest analysis/tests/ -v
```

**What the tests check:**
- `test_io.py` -- SQLite loading and validation: malformed/wrong-schema
  databases, duplicate `FRAMENUMBER`, more than one `ANIMALID` in a
  per-animal file, unknown animal IDs, empty tables.
- `test_bouts.py` -- bout detection correctness, including the edge
  cases explicitly required: one-frame bouts, a session beginning or
  ending in-nest, and (most importantly) that an unresolved gap is never
  silently merged into a neighboring bout's duration or miscounted as an
  observed entry/exit.
- `test_occupancy.py` -- total-time-in-nest fraction math, occupancy
  timeline bin-fraction/frame-count invariants, fill-source composition.
- `test_co_occupancy.py` -- multi-animal merge behavior, including
  animals with non-overlapping frame ranges, frames where one animal is
  unresolved, and (new) the extended `group_occupancy_table()`'s
  per-animal columns, `n_in_nest` definition, `-1` preservation, and
  column ordering.
- `test_spatial.py` -- proximity/locomotion math and their handling of
  missing detections and large frame gaps.
- `test_config.py` -- every config validation failure mode (missing
  keys, invalid role, empty animals, empty file).
- `test_run_utils.py` -- (new) the timestamped run-directory helper:
  exact folder-name format, parent-directory auto-creation, and
  same-second collision handling (verifies the numeric-suffix fallback
  actually produces distinct directories).
- `test_integration.py` -- runs the actual CLI script end-to-end via
  `subprocess` against a synthetic 4-animal dataset and checks every
  output file's existence and content, including: the spatial-metrics-
  skipped path when not configured, that no files are ever written
  directly into the parent output directory, that `--output-dir`
  correctly overrides the config's own parent, and that two consecutive
  runs against the same parent always get distinct directories.

**A passing run** ends with a line like `63 passed in 5.16s` and no
`FAILED`/`ERROR` entries. **A failure** names the specific test and
assertion that broke -- for a metric test, that means the underlying
computation changed in a way the test's documented expectation doesn't
allow; re-read that test's comment before changing the assertion, since
the comment usually explains *why* that specific number is the correct
one, not just what it is.

