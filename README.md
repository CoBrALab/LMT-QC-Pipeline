# LMT QC Pipeline Documentation

This README documents a 5-script pipeline that takes a Live Mouse Tracker (LMT)
SQLite output, cleans it, infers a mouse's in-nest/out-of-nest state for frames
where the tracker lost detection, resolves ambiguous gaps via a human-in-the-loop
binary-search video review, and then draws QC samples to measure the pipeline's
accuracy against human judgment.

The pipeline exists because the raw LMT detector is not always able to see the
animal. Huddling, nesting material, and running wheel all produce
stretches of missing detections. Simply ignoring those gaps would silently
underestimate time spent in the nest; naively assuming "last known state"
across a long gap would overestimate it in the other direction. Each script in
this pipeline exists to resolve that ambiguity a little further, using the
cheapest reliable method available before escalating to a more expensive one:
static spatial rules first, then a small number of targeted human video
reviews, and finally an independent statistical audit of how well the whole
process performed.

## Execution order

1. `0.Preprocessing.py`: clean raw LMT SQLite (deduplicate DETECTION rows).
2. `1.lmt_gap_fill.py`: classify detected frames + logic-fill/flag gaps per animal.
3. `2.lmt_binary_search.py`: human-in-the-loop resolution of ambiguous gaps via video.
4. `3.lmt_qc_sampler.py`: draw random QC samples per pool + extract screenshots.
5. `4.lmt_qc_validator.py`: manual labeling of QC samples + accuracy metrics.

Each script's SQLite output is the next script's SQLite input; video files are
re-supplied independently at each step that needs them (scripts 2, 3, and 4)
rather than being passed along in the database.

## Setup & Running

This repository is a [uv](https://docs.astral.sh/uv/) project. uv manages
the Python interpreter, virtual environment, and dependencies together. 
You do not need to create a virtualenv or `pip install` anything by hand.

**Install once:**
```bash
uv sync
```
This provisions Python 3.12 (pinned in `.python-version`) and installs the
exact dependency versions pinned in `uv.lock` (numpy, pandas, opencv-python,
Pillow) into a local `.venv/`.

**Run any script:**

Scripts 0, 1, and 3 are pure command-line tools; every input is a CLI
argument/flag. Scripts 2 and 4 take their file/video/folder inputs as CLI
arguments too, but still open a GUI window for the interactive
part. See the **CLI Reference** section below for the
full argument list for
each script, or run any script with `--help`.
```bash
uv run python 0.Preprocessing.py --help
```
`uv run` automatically ensures the environment is in sync with `uv.lock`
before launching, so there's no separate "activate the venv" step.

**Adding or updating a dependency:**
```bash
uv add <package>       # add a new dependency
uv lock --upgrade       # refresh pinned versions in uv.lock
```
Both commands update `pyproject.toml`/`uv.lock` together, commit both
files afterward.

**Note on `tkinter`:** scripts 0, 1, and 3 are pure command-line tools and
have no GUI at all, they run fine on a headless/server environment.
Scripts 2 and 4 take their setup inputs as CLI arguments but still open a
GUI window for their interactive step (binary-search/checkpoint
review, and manual QC labeling, respectively), so they still need a
graphical display (X11, Wayland, macOS, or Windows) to run. uv's managed
Python build already bundles Tcl/Tk, so no separate system `python3-tk`
package install is required for the two scripts that still need it.

---

## CLI Reference

Every argument below is exactly as declared in each script's `argparse`
parser (or, for script 2, `_build_arg_parser()`). Run any script with
`--help` (or `-h`) to see this same information from argparse directly.

### `0.Preprocessing.py`

**Required:**

| Argument | Type | Controls |
|---|---|---|
| `-i`, `--input` | path | The LMT Output SQLite to clean. |
| `-o`, `--output-folder` | directory | Where `{input_name}_processed.sqlite` is written. |

**Optional (no value taken, plain flags):**

| Argument | Default | Controls |
|---|---|---|
| `--overwrite` | off | Overwrite `{input_name}_processed.sqlite` if it already exists in the output folder. Without it, the script aborts rather than replacing a previous run's output. |

**Example:**
```bash
uv run python 0.Preprocessing.py -i "/path/to/input/<input_sqlite_file>.sqlite" -o "/path/to/output/<output_directory>"
```

### `1.lmt_gap_fill.py`

**Required:** Unlike the
retired GUI (which had default values), the CLI intentionally does not
have a default animal ID or ROI/buffer coordinates: an unnoticed
pre-filled value silently applied to the wrong animal or the wrong nest
geometry would corrupt the in-nest time estimate without any indication
something was wrong, so the script refuses to run until every one of
these is supplied explicitly.

| Argument | Type | Controls |
|---|---|---|
| `-i`, `--input` | path | The `{name}_processed.sqlite` file from `0.Preprocessing.py`. |
| `-o`, `--output-folder` | directory | Where the timestamped `lmt_gap_fill_...sqlite` result is written. |
| `--animal-id` | int | Which animal's `DETECTION` rows to process. |
| `--nest-xmin` | float | Nest ROI X minimum. |
| `--nest-xmax` | float | Nest ROI X maximum. |
| `--nest-ymin` | float | Nest ROI Y minimum. |
| `--nest-ymax` | float | Nest ROI Y maximum. |
| `--buffer-xmin` | float | Buffer ROI X minimum. |
| `--buffer-xmax` | float | Buffer ROI X maximum. |
| `--buffer-ymin` | float | Buffer ROI Y minimum. |
| `--buffer-ymax` | float | Buffer ROI Y maximum. |

**Optional:** none, every argument above is required.

**Example:**
```bash
uv run python 1.lmt_gap_fill.py -i "/path/to/input/<*_processed>.sqlite" -o "/path/to/output/<output_directory>" --animal-id <animal_id> --nest-xmin <nest_xmin> --nest-xmax <nest_xmax> --nest-ymin <nest_ymin> --nest-ymax <nest_ymax> --buffer-xmin <buffer_xmin> --buffer-xmax <buffer_xmax> --buffer-ymin <buffer_ymin> --buffer-ymax <buffer_ymax>
```

### `2.lmt_binary_search.py`

**Required:**

| Argument | Type | Controls |
|---|---|---|
| `-i`, `--input` | path | The `lmt_gap_fill_A<animal_id>_<timestamp>.sqlite` file from `1.lmt_gap_fill.py`. |
| `-v`, `--videos` | one or more paths | LMT video file(s) (`*.mp4`) covering the gaps to review. |
| `-o`, `--output-folder` | directory | Where the resulting SQLite/report is written. |

**Optional:** none, these three are the only CLI arguments; everything
else (thresholds, review interval, etc.) is a hardcoded module constant,
unchanged by this issue.

**Example:**
```bash
uv run python 2.lmt_binary_search.py -i "/path/to/input/lmt_gap_fill_A<animal_id>_<date>.sqlite" -v "/path/to/videos/*.mp4" -o "/path/to/output/<output_directory>"
```

### `3.lmt_qc_sampler.py`

**Required:**

| Argument | Type | Controls |
|---|---|---|
| `-i`, `--input` | path | The `lmt_binary_search_A<animal_id>_<timestamp>.sqlite` file from `2.lmt_binary_search.py`. |
| `-v`, `--videos` | one or more paths | LMT video file(s) (`*.mp4`) to extract sample screenshots from. |
| `-o`, `--output-folder` | directory | Where per-pool results are written. |

**Optional:**

| Argument | Default | Controls |
|---|---|---|
| `-n`, `--samples` | `100` | Number of samples to draw, applied independently to each selected pool. |
| `--pools` | all three (`DETECTED BINARY_SEARCH LOGIC`) | Which QC pool(s) to sample from; pass one or more of `DETECTED`, `BINARY_SEARCH`, `LOGIC`. |
| `--overwrite` | off | Overwrite a pool's output folder if it already contains files from an earlier run today. Without it, that pool aborts. |

**Example:**
```bash
uv run python 3.lmt_qc_sampler.py -i "/path/to/input/lmt_binary_search_A<animal_id>_<YYYY-MM-DD_HH-MM-SS>.sqlite" -v "/path/to/videos/*.mp4" -o "/path/to/output/<output_directory>" -n 150 --pools DETECTED LOGIC
```

### `4.lmt_qc_validator.py`

**Required:**

| Argument | Type | Controls |
|---|---|---|
| `-i`, `--input` | path | The `lmt_qc_sampler_<qc_mode>_A<animal_id>_<timestamp>.sqlite` file from `3.lmt_qc_sampler.py`. |
| `-o`, `--screenshot-folder` | directory | The `Screenshots/` folder produced alongside that same file. |

**Optional:**

| Argument | Default | Controls |
|---|---|---|
| `-v`, `--videos` | none (empty) | LMT video file(s) (`*.mp4`); when supplied, enables the three-panel before/QC-frame/after view for `BINARY_SEARCH`/`LOGIC`/legacy `ASSUMED`-mode samples. Not needed for `DETECTED`-mode samples, which show only the pre-extracted screenshot. |

**Example:**
```bash
uv run python 4.lmt_qc_validator.py -i "/path/to/input/lmt_qc_sampler_<qc_mode>_A<animal_id>_<YYYY-MM-DD_HH-MM-SS>.sqlite" -o "/path/to/output/<screenshots_directory>" -v "/path/to/videos/*.mp4"
```

---

## Script: `0.Preprocessing.py`

### Overview
This script exists to give every downstream script a single, well-defined
notion of "a valid detection row" for a given frame and animal. LMT's
DETECTION export can contain duplicate or conflicting rows for the same
(`FRAMENUMBER`, `ANIMALID`) pair; every later script in this pipeline
assumes that pair is unique, so this cleanup has to happen first, once,
rather than being re-implemented as a filter in every other script.

**This script no longer removes rows based on `FRONT_X`/`FRONT_Y`/
`FRONT_Z`/`BACK_X`/`BACK_Y`/`BACK_Z` being `-1`.** It previously did,
on the assumption that a missing FRONT/BACK coordinate meant the row was
unusable. QC testing across several real SQLite databases showed that
assumption doesn't hold: a row can have `FRONT_*`/`BACK_*` all `-1` while
still carrying an accurate, usable `MASS_X`/`MASS_Y` position (see Git Issue [#26](https://github.com/CoBrALab/LMT-QC-Pipeline/issues/26)), the
coordinate pair this pipeline actually relies on throughout. Those rows
are now kept unchanged; this script doesn't inspect the `FRONT_*`/
`BACK_*` columns at all.

Instead, the script creates a cleaned **copy** of a raw LMT Output SQLite
database (it never modifies the original file) and deduplicates the
`DETECTION` table on (`FRAMENUMBER`, `ANIMALID`), the identity every
downstream script assumes is unique for a single animal's timeline, using
two rules:

- **Case A - completely identical rows.** If two or more rows are
  identical across every column (excluding a surrogate/auto-increment
  primary key, if the table has one), the first occurrence (by original
  row order) is kept, and the later identical rows are deleted.
- **Case B - conflicting rows.** If two or more rows share the same
  (`FRAMENUMBER`, `ANIMALID`) but disagree on some other column (e.g. two
  different `MASS_X`/`MASS_Y` readings for the same frame and animal),
  there is no principled way to pick a "correct" one, so **every** row in
  that group is deleted, including what would have been the first
  occurrence.

This matters beyond just data cleanliness: `1.lmt_gap_fill.py`'s
gap-detection logic assumes `FRAMENUMBER` strictly increases for a given
animal (see Git Issue [#5](https://github.com/CoBrALab/LMT-QC-Pipeline/issues/5)). A duplicate or out-of-order `FRAMENUMBER` silently
corrupts that script's gap sizing and the resulting in-nest time estimate,
without raising any error, unless this deduplication has already run.

After deletion, it runs `VACUUM` to reclaim the disk space freed by the
deleted rows (SQLite does not shrink a database file automatically after a
`DELETE`; the file only shrinks once `VACUUM` rewrites it).

### Inputs

| Input | Type | Purpose |
|---|---|---|
| LMT Output SQLite | SQLite database, table `DETECTION` | Raw LMT tracking output to be cleaned. |
| Output folder | Directory | Where the cleaned copy is written. |

`DETECTION` columns referenced:

| Column | Role |
|---|---|
| `FRAMENUMBER`, `ANIMALID` | The identity pair deduplication groups on. Both are required to be present; the script errors out clearly if either is missing. |
| Every other column (`MASS_X`, `MASS_Y`, `FRONT_*`, `BACK_*`, and any others the table happens to have) | Compared for row equality to distinguish Case A (identical) from Case B (conflicting), but never inspected for a specific value — this script no longer makes any deletion decision based on what a coordinate *is*, only on whether two rows for the same frame/animal *match*. A surrogate/auto-increment primary key column, if the table has one, is excluded from this comparison. |

All other tables in the source database are untouched and are not
inspected by this script.

### Outputs

| Output | Type | Purpose |
|---|---|---|
| `{original_name}_processed.sqlite` | SQLite database | Deduplicated input for `1.lmt_gap_fill.py`. |

### Processing Steps
1. **Select input and output** via CLI arguments (`-i/--input`,
   `-o/--output-folder`).
2. **Overwrite check** if `{original_name}_processed.sqlite` already exists
   in the output folder, the script aborts with a clear error unless
   `--overwrite` was passed, rather than silently replacing a previous
   run's output.
3. **Copy the file** with `shutil.copy2`, a single filesystem-level copy,
   rather than reading and rewriting rows in Python. This is both far
   faster and guarantees every table/column the script doesn't know about
   is preserved byte-for-byte.
4. **Verify the `DETECTION` table exists** in the copy before running any
   query against it, so a non-LMT or corrupted file produces a clear error
   instead of a raw SQLite exception. Also verifies `FRAMENUMBER` and
   `ANIMALID` are both present as columns.
5. **Load every `DETECTION` row**, along with its `rowid` (SQLite's
   implicit, insertion-order row identifier), so "first occurrence" in
   Case A can be determined deterministically from the original file's
   row order rather than an arbitrary in-memory order.
6. **Group by (`FRAMENUMBER`, `ANIMALID`)** and, within each group with
   more than one row, check whether every row is identical across all
   non-primary-key columns. A fully-identical group is Case A (keep the
   lowest-`rowid` row, delete the rest); a group with any disagreement is
   Case B (delete every row in the group). Groups of size one are left
   untouched.
7. **Report counts** for each case (groups affected and rows removed), plus
   up to 10 example (`FRAMENUMBER`, `ANIMALID`) pairs for Case B so a user
   can investigate the conflicting source data if needed, and exit early
   (no-op) if nothing needs deleting.
8. **Delete in bulk** via a temporary table of row IDs to remove plus a
   single `DELETE ... WHERE rowid IN (...)` statement, a set-based SQL
   operation rather than a per-row Python loop, which matters when a
   session has hundreds of thousands of frames.
9. **`VACUUM`** to physically reclaim the freed space, via `VACUUM INTO` a
   sibling temp file followed by an atomic rename over the output file,
   rather than an in-place `VACUUM` (see Key Design Decisions below for
   why).
10. **Report a timing summary** (copy / delete / vacuum durations) so a
    user running this against a very large database understands where the
    time went.

### Key Design Decisions & Assumptions
- **Copy-then-modify, never mutate the source.** The original LMT database
  is treated as an immutable, re-runnable source of truth; every later stage
  operates on derived files, so a mistake anywhere downstream never requires
  re-exporting from LMT.
- **Deduplicate on identity (`FRAMENUMBER` + `ANIMALID`), not on any
  coordinate value.** Earlier versions of this script deleted rows based
  on `FRONT_X = -1`, treating a specific coordinate value as an invalidity
  sentinel. That conflated "this row's FRONT/BACK tracking failed" with
  "this row is unusable," which QC testing showed to be false: `MASS_X`/
  `MASS_Y`, the values this pipeline is actually built on, can still be
  accurate on such a row. Whether two rows for the same frame and animal
  *agree* is a much safer signal than what either row's coordinates
  happen to be.
- **No arbitrary tie-breaking on conflicting data.** When two rows disagree
  about the same (`FRAMENUMBER`, `ANIMALID`), there's no information in
  this table that says which one is correct, so silently keeping one
  (e.g. "first wins") would quietly inject wrong data into the timeline.
  Deleting the whole group instead turns that frame into an ordinary gap,
  which downstream scripts already have a principled way to handle.
- **All database work is wrapped in `try`/`except`/`finally`** so the SQLite
  connection is always closed, even if a step fails partway through, and so
  a failure produces a readable error message on stderr (with a non-zero
  exit code) instead of a raw console traceback.
- **`VACUUM INTO` a sibling temp file plus an atomic swap, not an in-place
  `VACUUM`.** An in-place `VACUUM` builds its rebuild scratch file in
  SQLite's default temp location (typically the system temp directory),
  which can be on a much smaller partition than the output folder and can
  fail with "database or disk is full" on a large database even when the
  output location has plenty of space. `VACUUM INTO` writes the compacted
  copy directly alongside the output file instead, then `os.replace` swaps
  it in; if that step fails, the already-committed, uncompacted output
  from the `DELETE` step is left in place rather than losing the run's
  result.


### Open Source Notes
- **External dependencies**: `pandas` (used for the deduplication group-by
  logic).
- **Standard library**: `argparse`, `os`, `shutil`, `sqlite3`, `sys`, `time`.
- **Configuration files / environment variables**: none.
- **Expected directory structure**: none required; input file and output
  folder are given as CLI arguments.
- **Platform assumptions**: none — fully headless, runs on any environment
  with Python (no display required). `VACUUM` runs synchronously on the
  main thread, so the process may appear unresponsive during this step on
  very large databases.

---

## Script: `1.lmt_gap_fill.py`

### Overview
This script exists to turn a sparse, animal-specific stream of detected
frames into a complete, per-frame timeline, and to do as much of that work
as possible *without* requiring a human to look at any video. For every
animal, most of the timeline is unambiguous: a frame is either directly
observed by LMT, or it falls inside a short gap where simple geometry makes
the answer obvious. This script resolves everything it safely can using two
static spatial checks, and explicitly flags the remainder as unresolved so
that the more expensive, human-driven process in `2.lmt_binary_search.py`
only has to look at the frames that genuinely need it.

**This script does not perform any interactive or binary search.** It
applies a fixed, two-part geometric rule to each gap's two boundary frames
and immediately commits to one of two outcomes: confidently fill the gap, or
mark it uncertain. The algorithm that actually resolves an uncertain gap by
interactively narrowing down where the animal crossed a boundary (including
how it distinguishes an entry from an exit and how it converges on an exact
transition frame) is implemented entirely in `2.lmt_binary_search.py`; see
that script's **Processing Steps** section below for the full explanation.

### Inputs

| Input | Type | Purpose |
|---|---|---|
| `{original_name}_processed.sqlite` (script 0 output) | SQLite database, table `DETECTION` | Source of per-frame animal positions. |
| Animal ID | Integer | Which animal's `DETECTION` rows to process. |
| Nest ROI (`xmin`, `xmax`, `ymin`, `ymax`) | Floats | Bounding box defining "inside the nest." |
| Buffer ROI (`xmin`, `xmax`, `ymin`, `ymax`) | Floats | A looser bounding box, expected to fully contain the nest ROI, defining "close enough to the nest that leaving detection range is plausibly benign." |
| Output folder | Directory | Where the result database is written. |

`DETECTION` columns used: `FRAMENUMBER`, `MASS_X`, `MASS_Y` (animal centroid
position), `ANIMALID` (filter, passed as a bound SQL parameter).

### Outputs

| Output | Type | Purpose |
|---|---|---|
| `lmt_gap_fill_A<animal_id>_<YYYY-MM-DD_HH-MM-SS>.sqlite` | SQLite database, table `GAP_FILL_ANALYSIS` | Complete per-frame in-nest classification for one animal, including unresolved gaps flagged for script 2. |

`GAP_FILL_ANALYSIS` columns:

| Column | Meaning |
|---|---|
| `FRAMENUMBER` | Frame index. |
| `IN_NEST` | `1` = in nest, `0` = out of nest (both only for `DETECTED` rows); for `ASSUMED` rows, `1` = confidently logic-filled in-nest, `-1` = uncertain, deferred to script 2. `ASSUMED` rows never receive a plain `0` from this script. |
| `ASSUMPTION_TYPE` | `"DETECTED"` (an actual LMT observation) or `"ASSUMED"` (a filled-in gap frame). |
| `GAP_START_FRAME` | For `ASSUMED` rows, the last detected frame *before* the gap; `None` for `DETECTED` rows. |
| `GAP_END_FRAME` | For `ASSUMED` rows, the first detected frame *after* the gap; `None` for `DETECTED` rows. |
| `ANIMALID` | The animal ID this run was filtered to (the `--animal-id` argument), on every row. |

### Processing Steps
1. **Load one animal's detections**, ordered by `FRAMENUMBER`, using a
   parameterized query (`WHERE ANIMALID = ?`).
2. **Classify every detected frame** directly, using the nest ROI: a
   detected frame's `IN_NEST` value is simply whether its `(MASS_X, MASS_Y)`
   position falls strictly inside the nest bounding box. This is the ground
   truth the rest of the pipeline is built on.
3. **Walk every consecutive pair of detected frames** and compute the frame
   distance between them. A distance of `1` means no gap. A distance greater
   than `1` means one or more frames went undetected in between a *gap*
   and every missing `FRAMENUMBER` in that range becomes an `ASSUMED` row.
   A distance of `0` or less (a duplicate or out-of-order `FRAMENUMBER` for
   this animal) is a defensive validation failure (Git Issue [#5](https://github.com/CoBrALab/LMT-QC-Pipeline/issues/5)) rather than a
   silently-accepted "no gap" (see Key Design Decisions below).
4. **Decide each gap's fate using only its two endpoints** (this is the
   core heuristic, and the reason a human is not required for most gaps):
   - Test whether the animal was inside the **nest ROI** at the last frame
     *before* the gap.
   - Test whether the animal was inside the wider **buffer ROI** at the
     first frame *after* the gap.
   - If **both** are true, every frame in the gap is filled with
     `IN_NEST = 1`. The reasoning: the animal was already home when
     tracking was lost, and was still within a generous margin of home the
     moment tracking resumed. The most plausible explanation is that it
     simply held still in or near the nest while briefly occluded (e.g. by
     nesting material), rather than having left and returned undetected.
   - If **either** test fails, the gap is left as `IN_NEST = -1`. The
     script deliberately does not guess in this case, because it has no
     visual information about what happened during the gap and a wrong
     guess here would silently corrupt the in-nest time estimate.
5. **Reassemble the full timeline** by combining the detected rows and the
   generated assumed rows and sorting by `FRAMENUMBER`, then write the
   result to a new, timestamped SQLite file.

### Key Design Decisions & Assumptions
- **Defensive uniqueness check, not a repeat of `0.Preprocessing.py`'s
  deduplication (Issue #5).** This script assumes `FRAMENUMBER` strictly
  increases for the animal it's processing; a duplicate or out-of-order
  value would otherwise be silently treated as "no gap" (a `<= 0` distance
  between consecutive frames), corrupting gap sizing and the in-nest time
  estimate without any error. `0.Preprocessing.py`'s dedup on
  (`FRAMENUMBER`, `ANIMALID`) is expected to make this invariant hold by
  the time this script runs, so the check here should never actually
  trigger in the normal pipeline order. It exists to fail loudly, rather
  than corrupt results silently, if this script is ever run against data
  that skipped that step.
- **Two independent, asymmetric ROI tests, not one.** The buffer ROI is
  intentionally looser than the nest ROI. Requiring the *exit* point to only
  be within the wider buffer (rather than the strict nest box) tolerates
  ordinary positional noise right at the edge of tracking loss, while still
  requiring the *entry* point (start of the gap) to be strictly within the
  nest. 
- **The algorithm only ever looks at gap endpoints, never intermediate
  positions** (there are none, that's what makes it a gap). This makes the
  rule fast and fully vectorizable, but it also means it is fundamentally
  unable to detect an entry-and-exit (or exit-and-entry) that both happen
  inside the same gap; such cases are exactly what get pushed downstream as
  `-1`.
- **`ASSUMED` rows are only ever `1` or `-1`, never `0`.** This convention
  is load-bearing: `2.lmt_binary_search.py` selects its work queue with
  `df[df["IN_NEST"] == -1]`, and would silently skip frames if this script
  ever wrote a plain `0` for an assumed frame.

### Do NOT Modify
- ROI membership tests use strict inequality (`<`, not `<=`); changing this
  changes which frames are considered "at the boundary" and shifts results
  in a way no downstream script expects.
- `IN_NEST = -1` must remain the exact sentinel for "uncertain", both
  `2.lmt_binary_search.py` and `4.lmt_qc_validator.py` filter on this exact
  value.
- Table name `GAP_FILL_ANALYSIS` and all six output columns
  (`FRAMENUMBER`, `IN_NEST`, `ASSUMPTION_TYPE`, `GAP_START_FRAME`,
  `GAP_END_FRAME`, `ANIMALID`) must keep these exact names:
  `2.lmt_binary_search.py` loads the whole table with `SELECT *` (no
  column names hardcoded in the query itself), but then accesses each of
  these columns by name throughout, so renaming or dropping any of them
  breaks that script.
- `GAP_START_FRAME` / `GAP_END_FRAME` must remain "last detected frame
  before" / "first detected frame after". Script 2 groups and classifies
  gaps using this exact definition.

### Open Source Notes
- **External dependencies**: `pandas`, `numpy` (used for the vectorized
  gap-fill computation).
- **Standard library**: `argparse`, `os`, `sqlite3`, `sys`, `datetime`.
- **Configuration files / environment variables**: none; animal ID and ROI
  bounds are CLI arguments (`--animal-id`, `--nest-*`, `--buffer-*`), all
  required with no defaults — see the **CLI Reference** section above.
- **Expected directory structure**: none required beyond a writable output
  folder.
- **Platform assumptions**: none, fully headless, no GUI toolkit is used.

---

## Script: `2.lmt_binary_search.py`

### Overview
This script exists to resolve exactly the frames `1.lmt_gap_fill.py` could
not: gaps where the animal's state genuinely changed (or might have) and
there is no way to know when without looking at video. Rather than asking a
human to scrub through every second of every ambiguous gap, it uses a
**binary search over frame numbers** to find the transition point with a
small, logarithmic number of targeted clicks, then writes the fully resolved
timeline back out for the rest of the pipeline.

Loads the `1.lmt_gap_fill.py` output and isolates every `ASSUMED` frame
still marked `IN_NEST = -1`. Groups them into gaps, classifies each gap's
boundary type, and skips the gaps that cannot or need not be searched.
Remaining gaps are queued for an interactive, GUI-driven review: the
reviewer is shown a three-panel view (last detected frame before the gap,
a candidate frame partway through the gap, and the first detected frame
after the gap) and answers "IN NEST" or "OUT OF NEST" or "SKIP," which recursively
narrows the segment until the transition frame is pinned down. Once every
gap has been processed, it writes the final per-frame classification (with
`FILL_SOURCE`) plus a detailed
plain-text summary report with internal integrity checks.

### Inputs

| Input | Type | Purpose |
|---|---|---|
| `lmt_gap_fill_A<animal_id>_<date>.sqlite` (script 1 output) | SQLite database, table `GAP_FILL_ANALYSIS` | Per-frame classification with unresolved (`IN_NEST = -1`) gaps to review. |
| LMT video files (`*.mp4`) | Video | Source frames for the reviewer to visually classify. |
| Output folder | Directory | Where results are written. |

Video filenames must contain a `t<digits>` segment immediately before the
file extension (e.g. `..._t12345.mp4`), giving the video's starting global
frame number. Videos are assumed to run at `DB_FPS / FRAME_CONVERSION`
(default `30 / 2 = 15` fps); a video whose actual frame rate deviates from
this by more than a small tolerance is flagged to the user, and a video
whose filename cannot be parsed is excluded and reported rather than
silently dropped.

### Outputs

| Output | Type | Purpose |
|---|---|---|
| `lmt_binary_search_A<animal_id>_<YYYY-MM-DD_HH-MM-SS>.sqlite` (or `lmt_binary_search_Aunknown_...` if the source lacked `ANIMALID`) | SQLite database, table `GAP_FILL_ANALYSIS` | Final per-frame classification with fill-method bookkeeping, for QC sampling in script 3. |
| `LMT_Summary_A<animal_id>_<YYYY-MM-DD_HH-MM-SS>.txt` | Text report | Audit of detection counts, gap types, binary-search results, and internal balance/integrity checks. |
| `_binsearch_tmp/` | Temp image cache | Scratch PNGs extracted during review; deleted on successful completion or if the window is closed early. |

`GAP_FILL_ANALYSIS` columns (extends script 1's schema):

| Column | Meaning |
|---|---|
| `FRAMENUMBER`, `ASSUMPTION_TYPE`, `GAP_START_FRAME`, `GAP_END_FRAME`, `ANIMALID` | Carried over from script 1. |
| `IN_NEST` | Final classification (`1`, `0`, or `-1` for a frame that was routed to the interactive reviewer but explicitly marked "cannot judge," or that fell in a gap type/duration this script does not review). |
| `FILL_SOURCE` | `"DETECTED"`, `"LOGIC"` (filled by script 1), `"BINARY_SEARCH"` (routed to the interactive reviewer here, regardless of whether it resolved to `0`/`1` or was explicitly skipped and left `-1`), or `"UNKNOWN"` (never routed to the reviewer at all: below the duration threshold, or a type-11 gap). |

This script no longer writes a separate `BINARY_SEARCH` integer column.
`FILL_SOURCE == "BINARY_SEARCH"` alone identifies every frame routed to
the interactive reviewer; a `BINARY_SEARCH` column may still be present
and readable on older files produced before this change (see script 3's
"Detect the source schema" step and `lmt_common.py`'s `compute_qc_pool_mask()`).

### Processing Steps

**1. Load and group unresolved gaps.** Every `ASSUMED` row with
`IN_NEST = -1` is pulled from script 1's output and grouped by
`(GAP_START_FRAME, GAP_END_FRAME)` into distinct gaps.

**2. Classify each gap's boundary type.** Using the `IN_NEST` state of the
detected frame immediately before the gap and the detected frame
immediately after it, every gap is labeled with one of four types:

- **`00`** (out → out): no directional information is available from the
  endpoints alone, the animal could have stayed out the whole time, or
  briefly entered and left again, and there is no way to tell from the
  boundary states, so **binary search cannot be used** (there's no known
  transition to bisect toward). Above `MIN_GAP_DURATION_FOR_BINARY_SEARCH`,
  these gaps are instead reviewed via left-to-right **checkpoint
  sampling** (see step 4) rather than left unresolved; at or below the
  threshold, they're still **skipped** (left `-1`), same as before.
- **`11`** (in → in): script 1's fill rule requires the frame *before* the
  gap to be strictly inside the nest ROI and the frame *after* it to be
  inside the (wider) buffer ROI, and the buffer ROI is validated to fully
  contain the nest ROI. A frame strictly inside the nest ROI is therefore
  always also inside the buffer ROI, so this fill condition is always
  satisfied for a type-11 gap's endpoints. Script 1 always resolves these
  to `IN_NEST = 1`, and a `-1` frame in a type-11 gap should never reach
  this script. The check here is a defensive one (e.g. against a
  hand-edited or otherwise inconsistent input file, not a case this
  pipeline is expected to produce): if it's ever hit, the gap is skipped
  and its frames are counted separately in the summary report, rather than
  silently miscounted or crashing.
- **`01`** (out → in) and **`10`** (in → out): these are the gaps that
  matter, the animal's state is known to differ between the two
  endpoints, so **exactly one transition occurred somewhere inside the
  gap**. These are the only gap types eligible for binary search. Note
  that script 1's fill rule only requires the *after* frame to be inside
  the looser buffer ROI, not the strict nest ROI, so some `10`-boundary
  gaps (in-nest before, but only buffer-adjacent (not strictly in-nest)
  after) are already resolved by script 1 and never reach this script at
  all; the `10` gaps seen and reviewed here are only the ones where that
  buffer test failed.
  (Type-00 gaps are eligible for a different review mechanism, see
  step 4)

**3. Filter by duration.** For `01`,`10`, and `00` gaps, any at or below
`MIN_GAP_DURATION_FOR_BINARY_SEARCH` (default 30 seconds) are left `-1`
rather than queued for review, a gap this short contributes little to the
overall time-in-nest estimate relative to the reviewer time it would cost
to resolve precisely.

**4. Checkpoint-review the remaining `00` gaps.** Since there's no known
transition to bisect toward, type-00 gaps aren't handled by binary search
from the start. Instead, above-threshold `00` gaps are sampled left-to-right
at `TYPE00_REVIEW_INTERVAL_SECONDS` (default 60 seconds): starting from the
gap's left edge, the reviewer is shown a frame every interval and asked
whether the animal is in the nest. The final checkpoint in the precomputed
sequence always lands exactly on the gap's right edge regardless of
interval alignment, so that edge is always explicitly reviewed rather than
extrapolated. A gap no longer than one interval gets a single checkpoint
covering the whole gap.

Each checkpoint answer is handled one of two ways:

- **OUT OF NEST** fills backward from the previous checkpoint (or the gap's
  start, for the first one) with `0`, and checkpoint sampling continues to
  the next checkpoint in the precomputed sequence.
- **IN NEST** — the first checkpoint answered this way stops checkpoint
  sampling for the gap immediately: any remaining precomputed checkpoints
  for this gap are discarded, the sampled segment up to and including this
  checkpoint is filled `1`, and the *rest* of the gap (from just after this
  checkpoint through the gap's right edge, which is already known
  out-of-nest) is handed off to a `10` (in-nest → out-of-nest) binary
  search instead of continuing to sample every remaining interval. This
  hand-off is possible because the checkpoint that was just answered IN
  is now a known left boundary, turning the remainder of the gap into an
  ordinary `10` sub-problem.

Unlike a `01`/`10` binary search, the checkpoint sequence itself is flat and
precomputed up front (no recursive subdivision), but on an IN answer, the
gap's *remaining* portion switches to the same recursive binary-search
mechanism used for `01`/`10` gaps (see step 5).

**5. Binary search the remaining gaps.** This is the core algorithm, and it
relies on one key assumption: **within a `01` or `10` gap, the animal's
in-nest state is monotonic**, it changes exactly once, at some unknown
frame, from the state at the gap's start to the (different) state at the
gap's end. This turns "find the transition frame" into the same problem as
searching a sorted array for the boundary between two runs of different
values, which is exactly what binary search is designed for: each answer
the reviewer gives eliminates roughly half of the remaining candidate
frames, so a gap spanning thousands of frames typically resolves in well
under twenty clicks rather than requiring the reviewer to scrub through it
frame by frame.

Concretely, for a segment `[seg_start, seg_end]` still being searched, the
reviewer is shown the frame at the midpoint and asked whether the animal is
currently in the nest. What that answer implies and which half of the
segment gets filled immediately versus searched further depends on which
direction the gap runs:

- **`10` gap (in → out of nest).** The segment starts *known in-nest* and
  ends *known out-of-nest*. If the reviewer answers **IN NEST**, the exit
  has not happened yet by the midpoint, so every frame from the segment
  start through the midpoint is safely filled `IN_NEST = 1`, and the search
  continues on the *right* half (midpoint + 1 through segment end) to keep
  looking for the exit. If the reviewer answers **OUT OF NEST**, the exit
  already happened at or before the midpoint, so the search continues on
  the *left* half (segment start through midpoint − 1); once there is no
  room left to search further left, every remaining frame from the
  midpoint through the segment end is filled `IN_NEST = 0`.
- **`01` gap (out of nest → in).** The same logic applies mirrored: an
  **IN NEST** answer means the entry has already happened by the midpoint,
  so the *right* half (midpoint through segment end) is filled `1`
  immediately, and the search continues on the *left* half looking for
  exactly when entry occurred. An **OUT OF NEST** answer means entry hasn't
  happened yet, so the search continues on the *right* half, and once no
  room remains to search further, everything up through the midpoint is
  filled `0`.

In both directions the reviewer is answering the same underlying question,
"has the transition occurred by this frame?," but which half is resolved
immediately and which half continues to be searched is flipped, because
which endpoint state is "known" differs between an entry and an exit.

**6. Convergence and termination.** Recursion on a gap's subtasks
terminates in one of two ways:

- The segment being searched shrinks to zero width (`seg_start > seg_end`),
  at which point the two already-filled halves fully account for every
  frame originally in the gap, every subtask either fills a contiguous
  block outright or hands off exactly the untouched remainder to a new
  subtask, so no frame is ever double-counted or dropped.
- As a practical shortcut, once a candidate `01`/`10` segment's *duration*
  drops to or below `FILL_ENTIRE_SEGMENT_IF_DURATION_LESS_THAN_IN_MINUTES`
  (default 1 minute: despite the name, the actual comparison is inclusive:
  exactly 1 minute also qualifies), the entire remaining segment is filled
  from a single answer instead of continuing to subdivide down to
  individual frames, an OUT OF NEST answer fills the whole segment `0`,
  an IN NEST answer fills it `1`, symmetrically. Sub-minute precision on
  exactly which frame a transition occurred is not meaningful for this
  pipeline's purposes, so this trades a small amount of possible
  imprecision for a large reduction in reviewer clicks.
- **This shortcut is deliberately skipped for type-00 checkpoints**,
  regardless of segment duration. `TYPE00_REVIEW_INTERVAL_SECONDS`
  (default 60s) is the same length as this shortcut's default threshold
  (1 minute), so most type-00 checkpoint segments would otherwise land
  exactly on this boundary and short-circuit here, which would silently
  skip the type-10 hand-off on an IN answer (step 4), since this shortcut
  only fills and advances and never hands off. Type-00 checkpoints always
  go through their own answer-handling path instead (step 4), which fills
  identically to this shortcut on an OUT answer but adds the hand-off on
  an IN answer, independent of segment duration.

**7. Determine the final gap classification.** Once every subtask for
every gap has been answered, all resulting frame decisions are merged with
the unchanged `DETECTED` frame states into the authoritative final
`IN_NEST` value for every frame, along with `FILL_SOURCE` bookkeeping.
This merge is a mix of vectorized NumPy
boolean-mask assignment (for the bulk of the work: splitting already-valid
`DETECTED` frames from the ones needing a decision) and a few small,
per-frame Python lookups (checking `skipped_frames`/`searchable_frames`
set membership and reading each frame's answer out of the `decisions`
dict) — those lookups are against plain Python sets/dicts, not
`numpy`-friendly structures, so they aren't vectorized, but they only run
over the `ASSUMED` frames actually needing resolution, not the full
per-frame dataset.

**8. Write outputs and report.** The final table is written to a new
SQLite file, and a detailed summary report is generated with multiple
internal balance checks (e.g. that every accounted-for frame category sums
back to the original total), if any check fails, an `IntegrityError` is
raised rather than silently emitting a report with unexplained
inconsistencies.

### Key Design Decisions & Assumptions
- **Binary search is appropriate here specifically because `01`/`10` gaps
  are assumed to contain exactly one transition.**
  This monotonicity assumption is what makes "ask about the midpoint, then
  recurse on one half" valid, it would not be valid for `00` gaps (no
  known transition at all) which is exactly why those gaps are never
  binary-searched. Above-threshold `00` gaps are instead handled by a
  separate checkpoint-sampling mechanism that doesn't assume monotonicity
  (see step 4); at or below the duration threshold, they're still
  filtered out before any review begins.
- **Nearest-frame video resolution, not backward-only or exact-only.**
  When a requested global frame doesn't map exactly onto any loaded video's
  coverage, the script resolves to the nearer of the closest preceding or
  succeeding available frame (preceding wins exact ties) rather than
   failing outright. This same
  resolution strategy is reused in scripts 3 and 4 so that a QC reviewer
  later sees the same substitute frame the original binary-search reviewer
  saw for the same gap.
- **Video files are opened once and cached**, not reopened for every frame
  extraction, since a single review session may request hundreds of frames
  from the same handful of video files.
- **Integrity checks are deliberately strict.** The summary report
  recomputes several frame-count totals through independent paths and
  raises rather than continues if they disagree. The report's numbers are
  used as the pipeline's audit trail, so an inconsistency there should stop
  the run rather than be written down as if it were trustworthy.

### Do NOT Modify
- `DB_FPS = 30` and `FRAME_CONVERSION = 2` encode the relationship between
  the LMT database frame rate and the video frame rate; wrong values
  silently extract the wrong frames.
- Video filename parsing (`get_start_frame()`) requires a `t<digits>`
  segment immediately before the file extension; any other convention
  breaks video-to-frame mapping.
- `FILL_SOURCE == "BINARY_SEARCH"` must be set only for frames in gaps
  that were actually routed to the reviewer (i.e. `01`/`10` gaps above the
  duration threshold, and `00` gaps above the same threshold via
  checkpoint review), `3.lmt_qc_sampler.py` and `4.lmt_qc_validator.py`
  rely on this distinction.
- Table name `GAP_FILL_ANALYSIS` and the `FILL_SOURCE` column are read by
  name in `3.lmt_qc_sampler.py` and `4.lmt_qc_validator.py`. This script no
  longer writes a `BINARY_SEARCH` column, but `lmt_common.py`'s
  `compute_qc_pool_mask()` still reads one if present, purely to keep
  reading older files generated before this change (do not remove that
  fallback).

### Open Source Notes
- **External dependencies**: `opencv-python` (`cv2`), `numpy`, `pandas`,
  `Pillow` (`PIL.Image`, `PIL.ImageTk`), `tkinter`.
- **Standard library**: `argparse`, `os`, `re`, `copy`, `sqlite3`, `sys`,
  `datetime`, `time`.
- **Configuration files / environment variables**: none; database, video(s),
  and output folder are CLI arguments (`-i/--input`, `-v/--videos`,
  `-o/--output-folder`). `MIN_GAP_DURATION_FOR_BINARY_SEARCH = 30s`,
  `FILL_ENTIRE_SEGMENT_IF_DURATION_LESS_THAN_IN_MINUTES = 1 min`, and
  `TYPE00_REVIEW_INTERVAL_SECONDS = 60s` are hardcoded module-level
  constants in this script. `DB_FPS`, `FRAME_CONVERSION`, and
  `FPS_TOLERANCE = 0.5` are not hardcoded here, they're imported from
  `lmt_common.py`, the module shared with scripts 3 and 4.
- **Expected directory structure**: none required beyond a writable output
  folder (`_binsearch_tmp` is created automatically and cleaned up
  automatically, including on early window close).
- **Platform assumptions**: CLI argument parsing and input validation are
  headless (no display needed to see a `--help` message or an invalid-path
  error), but the review step itself still requires Tkinter + a working
  OpenCV video backend once setup succeeds; not headless-safe end-to-end.
  Video codec support depends on the local OpenCV build. Because video
  files are cached open for the review session, reviewing a very large
  number of distinct video files in one sitting could approach a
  platform's open-file-handle limit.

---

## Script: `3.lmt_qc_sampler.py`

### Overview
This script exists to make the pipeline's output auditable. Scripts 1 and 2
each make decisions (one by static geometric rule, one by human binary
search) and both could still be systematically wrong in ways that are hard
to notice by inspection alone. Rather than trusting either process blindly,
this script draws independently-sized random samples from each category of
decision (raw detections, logic-filled gaps, binary-search-filled gaps) so
that `4.lmt_qc_validator.py` can measure each one's real-world accuracy
separately.

Loads a `2.lmt_binary_search.py` output, splits
rows into three QC "pools" (`DETECTED`, `BINARY_SEARCH`, or `LOGIC`) based
on how each frame's classification was produced, draws a proportionally
stratified sample of a user-specified total size from each selected pool
(allocated across that pool's gaps in proportion to each gap's size, not a
uniform random draw across all its frames), extracts the corresponding
video frame as a screenshot, and records everything in a new SQLite table
for manual review.

### Inputs

| Input | Type | Purpose |
|---|---|---|
| `lmt_binary_search_A<animal_id>_<YYYY-MM-DD_HH-MM-SS>.sqlite` (script 2 output) | SQLite database, table `GAP_FILL_ANALYSIS` | Fully classified per-frame data to draw QC samples from. Must contain an `ANIMALID` column — see Animal ID below. |
| LMT video files (`*.mp4`) | Video | Source of the screenshot images for each sampled frame. |
| Output folder | Directory | Where per-pool results are written. |
| Sample count | Integer | Applied independently to each selected pool. |
| QC pool selection | List (0 or more of `DETECTED`/`BINARY_SEARCH`/`LOGIC`) | Any of `DETECTED`, `BINARY_SEARCH`, `LOGIC` rows. |

Animal ID is not a user-supplied input: it's read automatically from the
input SQLite's `ANIMALID` column (present since script 1 started writing
it). The script hard-fails with a clear error if that column is missing
(an older, pre-`ANIMALID` file), or if the file unexpectedly contains more
than one distinct Animal ID.

### Outputs

Per selected pool, written to `output_folder/{qc_mode}_A<animal_id>_{timestamp}/`:

| Output | Type | Purpose |
|---|---|---|
| `lmt_qc_sampler_<qc_mode>_A<animal_id>_<YYYY-MM-DD_HH-MM-SS>.sqlite` | SQLite database, table `QC_ASSUMED_SAMPLES` | Metadata for the drawn QC sample. |
| `Screenshots/S####_A<animal_id>_G<frame>_<video>.png` | PNG image | Extracted video frame for each sampled row. |

`QC_ASSUMED_SAMPLES` columns:

| Column | Meaning |
|---|---|
| `sample_id` | 1-based counter, also embedded in the screenshot filename. |
| `animal_id` | Animal ID, read from the source database's `ANIMALID` column. |
| `video` | Basename of the video the screenshot came from. |
| `frame_global` | The actual (possibly nearest-neighbor-resolved) frame captured. |
| `requested_frame` | The originally sampled `FRAMENUMBER`, before any resolution. |
| `IN_NEST` | Classification carried over from the source row. |
| `ASSUMPTION_TYPE` | `"DETECTED"` or `"ASSUMED"`, carried over. |
| `FILL_SOURCE` | Carried over if present in the source; `null` if the source predates this column. |
| `GAP_START_FRAME` / `GAP_END_FRAME` | Carried over; populated only for `ASSUMED` rows. |
| `screenshot` | Filename of the extracted PNG, relative to `Screenshots/`. |
| `QC_MODE` | Which pool this row belongs to (`"DETECTED"` / `"BINARY_SEARCH"` / `"LOGIC"`). |

### Processing Steps
1. **Configure the run** via CLI arguments: source database, videos, output
   folder, sample size, and which pools to draw from. Animal ID is read
   automatically from the source database (see Inputs above), not
   supplied here.
2. **Guard existing output.** If a pool's output folder for today's date
   already contains files (e.g. from an earlier run), abort with a clear
   error unless `--overwrite` was passed, rather than silently overwriting.
3. **Detect the source schema.** Whether the file has the modern
   `FILL_SOURCE` column, a legacy `BINARY_SEARCH` flag, or neither, and
   load `GAP_FILL_ANALYSIS` (or the legacy `ASSUMED_FRAMES` table)
   accordingly.
4. **Filter to the requested pool.** `DETECTED` selects LMT-observed rows
   outright; `BINARY_SEARCH` and `LOGIC` each select `ASSUMED` rows with a
   resolved (`0`/`1`) `IN_NEST` value and a matching `FILL_SOURCE`,
   deliberately excluding any row still stuck at `-1`. Attempting to sample
   a pool the source file has no data for (e.g. `BINARY_SEARCH` from a
   file that skipped script 2) produces a clear, actionable error rather
   than a raw `KeyError`.

   **The `BINARY_SEARCH` pool is methodologically mixed.** `FILL_SOURCE ==
   "BINARY_SEARCH"` is set identically for a `01`/`10` gap's recursive
   binary-search decisions and for an above-threshold `00` gap's
   checkpoint-review decisions (see script 2's step 4), there is no
   separate value, flag, or column distinguishing the two. This script
   samples and this pool's frames indistinguishably: a `00` gap and a
   `10` gap compete for the same proportional sample budget, and
   `4.lmt_qc_validator.py` reports one accuracy number across both. If
   checkpoint-review accuracy and binary-search accuracy need to be
   measured separately, that split isn't available from this pool as-is.
5. **Draw a proportionally stratified sample**, bounded to the pool's
   actual size. The requested sample count is allocated across the pool's
   distinct gaps in proportion to each gap's frame count (largest-remainder
   apportionment, so the allocation sums exactly to the request), then that
   many frames are drawn uniformly at random *within* each gap, this
   avoids a plain uniform draw over all frames being dominated by a
   handful of very large gaps. An explicit, freshly-generated random seed
   is used and reported back to the user: the draw is still effectively
   random every run, but the exact sample can be reproduced later if the
   seed is recorded.
6. **Resolve and extract a screenshot for every sampled frame**, using the
   same nearest-available-frame strategy as script 2, recording both the
   requested and resolved frame numbers so a reviewer can see whether (and
   how far) a substitution was made.
7. **Write the pool's SQLite table and screenshot folder**, and report a
   per-pool summary including the sampling seed used.

### Key Design Decisions & Assumptions
- **Pool-based sampling, not one pooled-together sample.** `DETECTED`,
  `LOGIC`, and `BINARY_SEARCH` rows represent three structurally different
  sources of potential error (raw detector noise, a static geometric
  heuristic, and human judgment), so each is sampled and later scored
  independently rather than being mixed into a single accuracy number that
  would obscure which stage of the pipeline is actually underperforming.
- **Frame resolution logic is intentionally identical to script 2's**, so
  that whatever substitution happened for a given frame in binary search is
  reproduced consistently here rather than independently re-derived (and
  potentially diverging).
- **Video files are cached open for the duration of a sampling run** and
  released once all selected pools finish, for the same performance reason
  as script 2.

### Do NOT Modify
- Table name `QC_ASSUMED_SAMPLES` and all listed column names are required
  by `4.lmt_qc_validator.py`'s `load_database()`.
- The screenshot filename format
  (`S{counter:04d}_A{animal_id}_G{resolved_frame}_{video_name}.png`) must
  remain resolvable relative to the `Screenshots/` subfolder for
  `4.lmt_qc_validator.py` to locate images.
- Every row written for a given output file must carry the same `QC_MODE`
  value, script 4 reads it from the first row only and does not support a
  mixed-mode file.

### Open Source Notes
- **External dependencies**: `opencv-python` (`cv2`), `pandas`.
- **Standard library**: `argparse`, `os`, `re`, `random`, `sys`, `sqlite3`,
  `datetime`.
- **Configuration files / environment variables**: none; database,
  video(s), output folder, sample size, and pool selection are CLI
  arguments (`-i/--input`, `-v/--videos`, `-o/--output-folder`,
  `-n/--samples`, `--pools`); `DB_FPS`, `FRAME_CONVERSION`, and
  `FPS_TOLERANCE` are imported from `lmt_common.py` (the module shared by
  scripts 2, 3, and 4), not hardcoded here; the three `QC_MODE_*`
  constants are likewise imported from `lmt_common.py`, not duplicated
  from `2.lmt_binary_search.py` (which doesn't define them at all).
- **Expected directory structure**: creates
  `{output_folder}/{qc_mode}_A<animal_id>_{timestamp}/Screenshots/`
  automatically.
- **Platform assumptions**: none — fully headless, no GUI toolkit is used.

---

## Script: `4.lmt_qc_validator.py`

### Overview
This script exists to close the loop: it is the only part of the pipeline
that produces an actual accuracy number. Everything upstream is a mechanism 
for producing a classification; this script is where a human directly 
compares that classification against what they can see with their own eyes, 
for a statistically meaningful sample, and where the pipeline's real-world 
error rate is finally measured rather than assumed.

Loads a `3.lmt_qc_sampler.py` output, determines the active QC mode from the
`QC_MODE` column (with legacy fallbacks), filters to the eligible rows for
that mode, and presents each sampled screenshot, plus, for `ASSUMED`-type
modes, the gap's before/after boundary frames re-extracted from video, to a
human reviewer for manual "IN NEST"/"OUT OF NEST" labeling. Saves progress
after every label, and on completion computes a two-class confusion matrix
(algorithm prediction = `IN_NEST` vs. human ground truth = `MANUAL_QC`) and
writes a text validation report.

### Inputs

| Input | Type | Purpose |
|---|---|---|
| `lmt_qc_sampler_<qc_mode>_A<animal_id>_<YYYY-MM-DD_HH-MM-SS>.sqlite` (script 3 output) | SQLite database, table `QC_ASSUMED_SAMPLES` | The drawn QC sample to label. |
| Screenshot folder | Directory of PNGs | Location of images referenced by the `screenshot` column. |
| LMT video files (optional) | Video | Enables re-extracting gap boundary frames for the three-panel view. |

### Outputs

| Output | Type | Purpose |
|---|---|---|
| `lmt_qc_validator_A<animal_id>_<YYYY-MM-DD_HH-MM-SS>.sqlite` (or `lmt_qc_validator_Aunknown_...` if the source lacked an `animal_id` column; written into the screenshot folder) | SQLite database, table `QC_ASSUMED_SAMPLES` | The same sample table with human labels recorded. |
| `lmt_qc_validator_A<animal_id>_<YYYY-MM-DD_HH-MM-SS>.txt` | Text report | Confusion matrix and accuracy/error-rate/sensitivity/specificity metrics, plus false-positive/false-negative screenshot lists. |

Columns are identical to script 3's output table, plus:

| Column | Meaning |
|---|---|
| `MANUAL_QC` | `1` = human says IN NEST, `0` = human says OUT OF NEST, `null`/`NaN` = not yet labeled. |

### Processing Steps
1. **Load the sample** and resolve the active QC mode from the first row's
   `QC_MODE` value (falling back to a legacy `"ASSUMED"` mode for older
   files), then re-apply the same eligibility filter script 3 used to build
   that pool, a defensive re-check against a hand-edited or unexpected
   input file.
2. **Present one sample at a time.** `DETECTED`-mode samples show the
   single pre-extracted screenshot. `BINARY_SEARCH`/`LOGIC`/legacy
   `ASSUMED` samples show a three-panel view, last known frame before the
   gap, the sampled QC frame, and first known frame after the gap, all
   re-extracted live from video using the same nearest-frame resolution
   strategy as scripts 2 and 3, so the reviewer sees the same context the
   original binary-search reviewer had for that gap.
3. **Record each label immediately.** Pressing IN NEST or OUT OF NEST
   writes to `MANUAL_QC` and persists it before advancing; the first label
   of a session creates the output file with a full save, and every
   subsequent label updates only that one row rather than rewriting the
   whole table.
4. **Compute the confusion matrix** once every sample is labeled: positive
   class = IN NEST, negative class = OUT OF NEST, algorithm prediction =
   `IN_NEST`, ground truth = `MANUAL_QC`. Any row whose `IN_NEST` is
   outside `{0, 1}` (which should never happen given script 3's filtering,
   but is checked defensively) is excluded and reported rather than
   silently folded into "predicted OUT."
5. **Write the report**, including accuracy, error rate, sensitivity, and
   specificity, and explicit lists of which screenshots were false
   positives/negatives, so a reviewer can go back and visually audit the
   pipeline's specific mistakes rather than only seeing an aggregate score.

### Key Design Decisions & Assumptions
- **The same nearest-frame video resolution logic is used across scripts
  2, 3, and 4 on purpose.** Accuracy validation is only meaningful if the
  reviewer here is looking at the same substitute frame the pipeline
  actually used to make its decision (or the same one the binary-search
  reviewer originally judged).
- **Metrics defensively exclude out-of-range predictions rather than
  mis-binning them.** A stray `IN_NEST` value outside `{0, 1}` reaching
  this stage indicates a bug upstream, not a genuine "predicted OUT" case,
  and should be visible as a warning rather than quietly skewing the
  reported accuracy.
- **Per-row incremental saves.** Since a validation session can involve
  hundreds of individual label clicks, persisting only the one row that
  changed (via an `UPDATE`) rather than rewriting the entire table on every
  click keeps the save cost independent of the total sample size.

### Do NOT Modify
- This is the terminal script in the pipeline; its report format (TP/FP/TN/FN,
  accuracy, error rate, sensitivity, specificity) is the pipeline's
  canonical accuracy measurement.
- The `QC_MODE` value is read from the **first row only** of the loaded
  table to choose filter/display logic for the whole session, input files
  must not mix QC modes.
- `screenshot` values must remain resolvable as
  `os.path.join(screenshot_folder, screenshot_nm)`, this script must be
  pointed at the same folder script 3 wrote screenshots into.
- The incremental per-row save relies on `sample_id` existing and being
  unique per row, as guaranteed by script 3's output.

### Open Source Notes
- **External dependencies**: `opencv-python` (`cv2`), `pandas`, `Pillow`
  (`PIL.Image`, `PIL.ImageTk`), `tkinter`.
- **Standard library**: `argparse`, `os`, `re`, `sys`, `sqlite3`, `datetime`,
  `tempfile`, `uuid`.
- **Configuration files / environment variables**: none; database, optional
  video(s), and screenshot folder are CLI arguments (`-i/--input`,
  `-v/--videos`, `-o/--screenshot-folder`); `DB_FPS`, `FRAME_CONVERSION`,
  `FPS_TOLERANCE`, and the QC mode constants are imported from
  `lmt_common.py` (the module shared by scripts 2, 3, and 4), not
  hardcoded or duplicated from either script.
- **Expected directory structure**: expects the screenshot folder produced
  by `3.lmt_qc_sampler.py`.
- **Platform assumptions**: CLI argument parsing and input validation are
  headless (no display needed to see a `--help` message or an invalid-path
  error), but the manual labeling step itself still requires Tkinter +
  OpenCV once setup succeeds; not headless-safe end-to-end. Uses the OS
  temp directory for scratch boundary-frame images (always cleaned up,
  including on error). Video files are cached open for the session and
  released when the window is closed.

---
## Script: `lmt_common.py`

### Overview

This module exists to guarantee one thing the pipeline's accuracy depends on: that scripts `2.lmt_binary_search.py`, `3.lmt_qc_sampler.py`, and `4.lmt_qc_validator.py` resolve a given video filename and a given requested global frame number to exactly the same substitute frame, every time. It is not a script, it has no GUI, no `__main__` entry point, and is never run directly. It is a shared library, imported by all three scripts above, holding the video/frame-resolution logic and the QC-pool eligibility rules that used to be three independently hand-maintained copies of the same code.

`3.lmt_qc_sampler.py` draws its QC sample pool using this module's frame-resolution logic, and `4.lmt_qc_validator.py` measures accuracy by comparing a human's manual label against the label the pipeline assigned, that comparison is only meaningful if both scripts (and `2.lmt_binary_search.py`, which produced the underlying `FILL_SOURCE` bookkeeping (plus a legacy `BINARY_SEARCH` column on files predating it) in the first place) agree on how a frame number maps to actual video content. Before this module existed, that agreement was enforced by hand-editing three copies in lockstep; this module makes it structural instead.

### Provided to callers

| Export | Kind | Purpose |
|---|---|---|
| `DB_FPS`, `FRAME_CONVERSION`, `EXPECTED_VIDEO_FPS`, `FPS_TOLERANCE` | Constants | Encode the relationship between the LMT database frame rate and the video frame rate, and how much a video's actual fps may deviate from that before it's flagged. |
| `QC_MODE_DETECTED`, `QC_MODE_BINARY_SEARCH`, `QC_MODE_LOGIC`, `QC_MODE_ASSUMED` | Constants | Identify which QC sample pool a caller is filtering for or sampling from. |
| `get_start_frame(video_name)` | Function | Parses a video filename's `t<digits>` segment into its starting global frame number. |
| `get_video_frame_count_and_fps(video_path)` | Function | Opens a video once and returns `(frame_count, fps)`. |
| `build_video_map(video_paths)` | Function | Builds the sorted `{start, end, path}` map used to route a global frame number to a specific video file, and reports any videos that were excluded (unparseable filename) or whose fps deviates beyond tolerance. |
| `find_nearest_frame_candidates(video_map, global_frame)` | Function | Resolves a requested global frame to the video(s) that can actually supply it, including the nearest-available-frame fallback logic used when the exact frame isn't covered by any video. |
| `_read_frame_from_video(video_entry, resolved_frame, out_path)` | Function | Seeks to and writes a single resolved frame out to disk, using the shared, cached `cv2.VideoCapture` handles. |
| `_release_all_captures()` | Function | Releases every cached `cv2.VideoCapture` handle; each script's cleanup/close path calls this. |
| `compute_qc_pool_mask(df_full, qc_mode)` | Function | Returns `(mask, label)`: the boolean row mask and a human-readable pool name for a requested QC mode (`DETECTED` / `BINARY_SEARCH` / `LOGIC` / `ASSUMED`), given a `GAP_FILL_ANALYSIS`-shaped DataFrame. Includes the fallback logic for older-format outputs that predate the `FILL_SOURCE` column. |

Each script also keeps its own thin, script-specific wrapper around this module's frame-reading primitives (`extract_frame_to_path` in script 2, `extract_frame` in script 3, `extract_frame_to_label` in script 4) — these differ in return signature and, for script 4, drive a GUI widget directly, so they are intentionally not part of this shared module.

### Key Design Decisions & Assumptions

- **Extracted, not reimplemented.** Every function and constant in this module is a verbatim move from `2.lmt_binary_search.py`'s original copy; nothing was rewritten or altered during extraction, to keep this refactor behavior-invariant.
- **A single in-process video-capture cache.** `_get_capture()`'s cache and `_release_all_captures()` are now shared across every script that imports this module within one process, this matches each script's existing single-session, single-process usage pattern and requires no change to caller behavior.
- **`compute_qc_pool_mask()` centralizes pool eligibility, not just frame resolution.** This was originally two independent implementations (`3.lmt_qc_sampler.py`'s `filter_pool()` and an inline block in `4.lmt_qc_validator.py`'s `load_database()`) that had already begun to drift — script 4 had a `QC_MODE_ASSUMED` legacy-pool branch script 3 did not. Both call sites now go through the same function.

### Do NOT Modify

- **Any change to `get_start_frame()`, `build_video_map()`, or `find_nearest_frame_candidates()` changes frame resolution for all three calling scripts simultaneously.**
- **`DB_FPS = 30` and `FRAME_CONVERSION = 2` must stay in sync** with the actual LMT database and video export frame rates; wrong values silently extract the wrong frames across every script that imports this module.
- **`compute_qc_pool_mask()`'s fallback branches (used when `FILL_SOURCE` is absent) must stay behaviorally identical to script 3's original `filter_pool()` logic** they exist specifically to keep older, pre-`FILL_SOURCE` `2.lmt_binary_search.py` outputs sampling/validating correctly.

### Open Source Notes

- **External dependencies**: `opencv-python` (`cv2`).
- **Standard library**: `os`, `re`.
- **Configuration files / environment variables**: none; all constants are hardcoded module-level values, unchanged from their original per-script definitions.
- **Expected directory structure**: none — this module has no file I/O of its own beyond frame extraction to a caller-supplied path.
- **Platform assumptions**: same as any importing script — depends on a working OpenCV video backend; not headless-relevant on its own since this module has no GUI code.

---
## Testing (tests/)

### Overview

The correctness-critical pure logic in this pipeline (gap-type classification, binary-search and type-00-checkpoint task generation/hand-off, frame resolution, and the gap-fill ROI/vectorization core) has an automated pytest suite covering it, independent of the Tkinter GUIs and real video/database files. This exists so a regression in that logic (e.g. the kind of silent defaulting bug fixed elsewhere in this pipeline's history) surfaces as a fast, deterministic test failure instead of only being catchable by manually reviewing GUI output against real data. The generic `_check` integrity-assertion helper is unit-tested in isolation (see What's Covered below), but the actual integrity-accounting code paths that call it — `write_summary_report`'s balance checks and `_finish`'s unresolved-frame check — are not exercised by the suite.

### How to Run
```bash
uv run pytest tests/ -v
```
No video files, SQLite databases, or GUI interaction are required, the suite runs fully headless.

### What's Covered
- `tests/test_binary_search_logic.py` (2.lmt_binary_search.py): `classify_gap_type`, the generic `_check` helper (tested standalone with arbitrary values, not through an actual report/accounting call site), `build_initial_tasks`, `find_nearest_frame_candidates` (via `lmt_common.py`).
- `tests/test_gap_fill_logic.py` (1.lmt_gap_fill.py): the gap-expansion and ROI-membership vectorization core.
- `tests/test_type00_gap_review.py` (2.lmt_binary_search.py): `_build_type00_checkpoint_tasks`, the type-00 branch of `_handle_answer` (including the checkpoint-to-binary-search hand-off on the first IN answer), and Skip/Undo/Redo against type-00 tasks.
- `tests/test_preprocessing_dedup.py` (0.Preprocessing.py, plus one end-to-end case into 1.lmt_gap_fill.py): `process_database`'s deduplication (Case A exact duplicates, Case B conflicting rows, distinct-`ANIMALID` rows left alone, `-1` `FRONT_*`/`BACK_*` rows no longer removed, a surrogate primary key excluded from identity comparison), the original file being left untouched, the overwrite guard, a missing `DETECTION` table, and `1.lmt_gap_fill.py`'s duplicate-`FRAMENUMBER` defensive check (Issue #5) both firing on raw non-deduplicated input and staying silent on deduplicated input. Runs against real temporary SQLite files (via `tmp_path`), not synthetic in-memory DataFrames, since deduplication is fundamentally a file-level operation.
- **Not covered**: `write_summary_report`'s and `_finish`'s integrity/balance-check logic, any of the four scripts' CLI argument parsing, and the interactive Tkinter GUI code paths in scripts 2 and 4 (image display, button/keyboard callbacks wiring, window lifecycle).
  
### Key Design Decisions & Assumptions
- Scripts are loaded by file path, not imported normally. The pipeline's numerically-prefixed filenames (1.lmt_gap_fill.py, etc.) aren't valid Python module names, so tests/conftest.py loads them via importlib.util.spec_from_file_location rather than a standard import statement.
- The repo root is added to sys.path for the duration of the test session. A script loaded this way still needs its own top-level imports (e.g. 2.lmt_binary_search.py's from lmt_common import ...) to resolve; python script.py gets this for free by putting the script's own directory on sys.path automatically, but importlib-based loading does not, so conftest.py does it explicitly.
- 1.lmt_gap_fill.py and 3.lmt_qc_sampler.py guard their CLI entry point (argparse parsing + execution) behind `if __name__ == "__main__":`, and 4.lmt_qc_validator.py guards its GUI bootstrap the same way; 0.Preprocessing.py and 2.lmt_binary_search.py already followed this pattern. This is required for these files to be importable at all without triggering `argparse`'s `sys.exit()` (scripts 0, 1, and 3) or opening a live Tkinter window (script 4). Interactive/CLI behavior (`uv run python <script>.py ...`) is unchanged by this guard.
- The gap-fill vectorization test re-implements the core ROI/gap-expansion logic inline (tests/test_gap_fill_logic.py's _run_core_logic helper) rather than calling run_analysis() directly, since that function also performs DB I/O not relevant to the logic under test.

### Do NOT Modify
- tests/conftest.py's sys.path insertion: removing it will reintroduce ModuleNotFoundError: lmt_common for any test that loads 2.lmt_binary_search.py.
- The __main__ guards in 0.Preprocessing.py, 1.lmt_gap_fill.py, 3.lmt_qc_sampler.py, and 4.lmt_qc_validator.py, removing them breaks headless test loading for those scripts (and, for any future test added against them, would trigger CLI argument parsing or open a GUI window during pytest collection).
  
### Open Source Notes
- External dependencies: pytest>=8.0.0 (dev dependency only, not required to run the pipeline itself).
- Standard library: importlib.util, pathlib, sys.
- Configuration files: pyproject.toml's [dependency-groups] dev section.
- Platform assumptions: none beyond what the pipeline scripts themselves require. The suite is fully headless and does not depend on a display server, video codec support, or a live SQLite file
