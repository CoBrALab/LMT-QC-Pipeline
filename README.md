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

1. `0.Preprocessing.py`: clean raw LMT SQLite (remove invalid detections).
2. `1.lmt_gap_fill.py`: classify detected frames + logic-fill/flag gaps per animal.
3. `2.lmt_binary_search.py`: human-in-the-loop resolution of ambiguous gaps via video.
4. `3.lmt_qc_sampler.py`: draw random QC samples per pool + extract screenshots.
5. `4.lmt_qc_validator.py`: manual labeling of QC samples + accuracy metrics.

Each script's SQLite output is the next script's SQLite input; video files are
re-supplied independently at each step that needs them (scripts 2, 3, and 4)
rather than being passed along in the database.

---
## Script: `0.Preprocessing.py`

### Overview
This script exists to give every downstream script a single, well-defined
notion of "a valid detection row." The raw LMT detector occasionally emits
placeholder rows for a frame it could not track at all, marked with sentinel
coordinate values rather than a real position. Every later script in this
pipeline (gap-filling, binary search, sampling, validation) assumes it is
working with genuinely observed positions, so this cleanup has to happen
first, once, rather than being re-implemented as a filter in every other
script.

The script creates a cleaned **copy** of a raw LMT Output SQLite database,
it never modifies the original file, and removes rows from the `DETECTION`
table where:

```sql
FRONT_X = -1
```

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
| `FRONT_X` | Determines which rows are deleted (`FRONT_X = -1`). |
| `FRONT_Y`, `FRONT_Z`, `BACK_X`, `BACK_Y`, `BACK_Z` | Used only to validate the assumption that a `FRONT_X = -1` row is *fully* invalid, not to decide deletion. |

All other tables/columns in the source database are untouched and are not
inspected by this script.

### Outputs

| Output | Type | Purpose |
|---|---|---|
| `{original_name}_processed.sqlite` | SQLite database (full copy of input, `DETECTION` rows removed) | Cleaned input for `1.lmt_gap_fill.py`. |

### Processing Steps
1. **Select input and output** via the GUI file/folder dialogs.
2. **Overwrite check** — if `{original_name}_processed.sqlite` already exists
   in the output folder, ask the user to confirm before overwriting it,
   rather than silently replacing a previous run's output.
3. **Copy the file** with `shutil.copy2`, a single filesystem-level copy,
   rather than reading and rewriting rows in Python. This is both far
   faster and guarantees every table/column the script doesn't know about
   is preserved byte-for-byte.
4. **Verify the `DETECTION` table exists** in the copy before running any
   query against it, so a non-LMT or corrupted file produces a clear error
   instead of a raw SQLite exception.
5. **Count candidate rows** (`FRONT_X = -1`) so the run can report how many
   rows will be affected, and exit early (no-op) if there are none.
6. **Validate the invalidity assumption** by checking whether every row with
   `FRONT_X = -1` also has `FRONT_Y`, `FRONT_Z`, `BACK_X`, `BACK_Y`, and
   `BACK_Z` all equal to `-1`. If any row violates this, the mismatch count
   is reported and the user is asked whether to proceed anyway; the
   deletion filter itself always remains `FRONT_X = -1`, since that is the
   convention every downstream script expects.
7. **Delete in bulk** with a single `DELETE ... WHERE FRONT_X = -1`
   statement, a set-based SQL operation rather than a per-row Python loop,
   which matters when a session has hundreds of thousands of frames.
8. **`VACUUM`** to physically reclaim the freed space.
9. **Report a timing summary** (copy / delete / vacuum durations) so a user
   running this against a very large database understands where the time
   went.

### Key Design Decisions & Assumptions
- **Copy-then-modify, never mutate the source.** The original LMT database
  is treated as an immutable, re-runnable source of truth; every later stage
  operates on derived files, so a mistake anywhere downstream never requires
  re-exporting from LMT.
- **`FRONT_X = -1` is the invalidity sentinel**, not a `NULL` or a separate
  status column, this mirrors how the LMT detector itself flags an
  undetected frame. The assumption-validation step exists specifically
  because this sentinel convention is a property of the *data*, not
  something this script can guarantee from the schema alone.
- **All database work is wrapped in `try`/`except`/`finally`** so the SQLite
  connection is always closed, even if a step fails partway through, and so
  a failure produces a readable error dialog instead of a console traceback.

### Do NOT Modify
- The deletion filter must remain `FRONT_X = -1`. Every downstream script
  assumes preprocessing has already been applied using this exact rule.
- The output naming convention `{original_name}_processed.sqlite` is a soft
  convention (no downstream script hardcodes it, they all use file-picker
  dialogs), but changing it will surprise users who rely on it to identify
  cleaned files.
- The original input database must never be modified in place.

### Open Source Notes
- **External dependencies**: `tkinter` (GUI; may require the `python3-tk`
  system package on some Linux distributions). No third-party pip packages.
- **Standard library**: `os`, `shutil`, `sqlite3`, `time`.
- **Configuration files / environment variables**: none.
- **Expected directory structure**: none required; input file and output
  folder are chosen interactively.
- **Platform assumptions**: requires a desktop environment capable of
  displaying a Tkinter window (not headless-safe). `VACUUM` runs
  synchronously on the GUI thread, so the window may appear unresponsive
  during this step on very large databases.

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
interactively narrowing down where the animal crossed a boundary — including
how it distinguishes an entry from an exit and how it converges on an exact
transition frame — is implemented entirely in `2.lmt_binary_search.py`; see
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
| `lmt_gap_fill_<YYYY-MM-DD_HH-MM-SS>.sqlite` | SQLite database, table `GAP_FILL_ANALYSIS` | Complete per-frame in-nest classification for one animal, including unresolved gaps flagged for script 2. |

`GAP_FILL_ANALYSIS` columns:

| Column | Meaning |
|---|---|
| `FRAMENUMBER` | Frame index. |
| `IN_NEST` | `1` = in nest, `0` = out of nest (both only for `DETECTED` rows); for `ASSUMED` rows, `1` = confidently logic-filled in-nest, `-1` = uncertain, deferred to script 2. `ASSUMED` rows never receive a plain `0` from this script. |
| `ASSUMPTION_TYPE` | `"DETECTED"` (an actual LMT observation) or `"ASSUMED"` (a filled-in gap frame). |
| `GAP_START_FRAME` | For `ASSUMED` rows, the last detected frame *before* the gap; `None` for `DETECTED` rows. |
| `GAP_END_FRAME` | For `ASSUMED` rows, the first detected frame *after* the gap; `None` for `DETECTED` rows. |

### Processing Steps
1. **Load one animal's detections**, ordered by `FRAMENUMBER`, using a
   parameterized query (`WHERE ANIMALID = ?`) rather than string
   interpolation.
2. **Validate frame ordering** — consecutive detected frames for this animal
   must have strictly increasing `FRAMENUMBER` values; a duplicate or
   out-of-order pair indicates a data problem and stops the run with a
   specific, actionable error rather than silently treating it as "no gap."
3. **Classify every detected frame** directly, using the nest ROI: a
   detected frame's `IN_NEST` value is simply whether its `(MASS_X, MASS_Y)`
   position falls strictly inside the nest bounding box. This is the ground
   truth the rest of the pipeline is built on.
4. **Walk every consecutive pair of detected frames** and compute the frame
   distance between them. A distance of `1` means no gap. A distance greater
   than `1` means one or more frames went undetected in between — a *gap* —
   and every missing `FRAMENUMBER` in that range becomes an `ASSUMED` row.
5. **Decide each gap's fate using only its two endpoints** (this is the
   core heuristic, and the reason a human is not required for most gaps):
   - Test whether the animal was inside the **nest ROI** at the last frame
     *before* the gap.
   - Test whether the animal was inside the wider **buffer ROI** at the
     first frame *after* the gap.
   - If **both** are true, every frame in the gap is filled with
     `IN_NEST = 1`. The reasoning: the animal was already home when
     tracking was lost, and was still within a generous margin of home the
     moment tracking resumed — the most plausible explanation is that it
     simply held still in or near the nest while briefly occluded (e.g. by
     nesting material), rather than having left and returned undetected.
   - If **either** test fails, the gap is left as `IN_NEST = -1` — the
     script deliberately does not guess in this case, because it has no
     visual information about what happened during the gap and a wrong
     guess here would silently corrupt the in-nest time estimate.
6. **Reassemble the full timeline** by combining the detected rows and the
   generated assumed rows and sorting by `FRAMENUMBER`, then write the
   result to a new, timestamped SQLite file.

### Key Design Decisions & Assumptions
- **Two independent, asymmetric ROI tests, not one.** The buffer ROI is
  intentionally looser than the nest ROI. Requiring the *exit* point to only
  be within the wider buffer (rather than the strict nest box) tolerates
  ordinary positional noise right at the edge of tracking loss, while still
  requiring the *entry* point (start of the gap) to be strictly within the
  nest — the two ends of a gap are not treated symmetrically because they
  represent different questions ("was it definitely home when we lost it?"
  vs. "was it still plausibly nearby when we found it again?").
- **The algorithm only ever looks at gap endpoints, never intermediate
  positions** (there are none — that's what makes it a gap). This makes the
  rule fast and fully vectorizable, but it also means it is fundamentally
  unable to detect an entry-and-exit (or exit-and-entry) that both happen
  inside the same gap; such cases are exactly what get pushed downstream as
  `-1`.
- **Known cross-script edge case**: this script's logic-fill condition
  (nest ROI at gap start **and** buffer ROI at gap end) can occasionally
  disagree with `2.lmt_binary_search.py`'s later boundary-type
  classification, which looks only at the strict nest ROI state of the two
  boundary frames. A gap can have both boundary frames strictly inside the
  nest (script 2's "type 11") and still be left `-1` by this script if the
  buffer condition at the gap's end frame fails. Script 2 explicitly detects
  and reports this situation rather than silently mis-filing it — see its
  **Key Design Decisions & Assumptions** section.
- **`ASSUMED` rows are only ever `1` or `-1`, never `0`.** This convention
  is load-bearing: `2.lmt_binary_search.py` selects its work queue with
  `df[df["IN_NEST"] == -1]`, and would silently skip frames if this script
  ever wrote a plain `0` for an assumed frame.

### Do NOT Modify
- ROI membership tests use strict inequality (`<`, not `<=`); changing this
  changes which frames are considered "at the boundary" and shifts results
  in a way no downstream script expects.
- `IN_NEST = -1` must remain the exact sentinel for "uncertain" — both
  `2.lmt_binary_search.py` and `4.lmt_qc_validator.py` filter on this exact
  value.
- Table name `GAP_FILL_ANALYSIS` and all five output columns
  (`FRAMENUMBER`, `IN_NEST`, `ASSUMPTION_TYPE`, `GAP_START_FRAME`,
  `GAP_END_FRAME`) are hardcoded in `2.lmt_binary_search.py`'s read query.
- `GAP_START_FRAME` / `GAP_END_FRAME` must remain "last detected frame
  before" / "first detected frame after" — script 2 groups and classifies
  gaps using this exact definition.

### Open Source Notes
- **External dependencies**: `pandas`, `numpy` (used for the vectorized
  gap-fill computation), `tkinter`.
- **Standard library**: `os`, `sqlite3`, `datetime`.
- **Configuration files / environment variables**: none; animal ID and ROI
  bounds are entered via the GUI at runtime.
- **Expected directory structure**: none required beyond a writable output
  folder.
- **Platform assumptions**: requires Tkinter GUI support (not
  headless-safe).

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
after the gap) and answers "IN NEST" or "OUT OF NEST," which recursively
narrows the segment until the transition frame is pinned down. Once every
gap has been processed, it writes the final per-frame classification (with
`FILL_SOURCE`/`BINARY_SEARCH` bookkeeping columns) plus a detailed
plain-text summary report with internal integrity checks.

### Inputs

| Input | Type | Purpose |
|---|---|---|
| `lmt_gap_fill_<date>.sqlite` (script 1 output) | SQLite database, table `GAP_FILL_ANALYSIS` | Per-frame classification with unresolved (`IN_NEST = -1`) gaps to review. |
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
| `lmt_binary_search_<YYYY-MM-DD>.sqlite` | SQLite database, table `GAP_FILL_ANALYSIS` | Final per-frame classification with fill-method bookkeeping, for QC sampling in script 3. |
| `LMT_Summary_<YYYY-MM-DD>.txt` | Text report | Audit of detection counts, gap types, binary-search results, and internal balance/integrity checks. |
| `_binsearch_tmp/` | Temp image cache | Scratch PNGs extracted during review; deleted on successful completion or if the window is closed early. |

`GAP_FILL_ANALYSIS` columns (extends script 1's schema):

| Column | Meaning |
|---|---|
| `FRAMENUMBER`, `ASSUMPTION_TYPE`, `GAP_START_FRAME`, `GAP_END_FRAME` | Carried over from script 1. |
| `IN_NEST` | Final classification (`1`, `0`, or, only for gaps that were never resolvable, `-1`). |
| `BINARY_SEARCH` | `1` if this frame was routed to the interactive reviewer, else `0`. |
| `FILL_SOURCE` | `"DETECTED"`, `"LOGIC"` (filled by script 1), `"BINARY_SEARCH"` (resolved here), or `"UNKNOWN"` (still unresolved). |

### Processing Steps

**1. Load and group unresolved gaps.** Every `ASSUMED` row with
`IN_NEST = -1` is pulled from script 1's output and grouped by
`(GAP_START_FRAME, GAP_END_FRAME)` into distinct gaps.

**2. Classify each gap's boundary type.** Using the `IN_NEST` state of the
detected frame immediately before the gap and the detected frame
immediately after it, every gap is labeled with one of four types:

- **`00`** (out → out): no directional information is available from the
  endpoints alone — the animal could have stayed out the whole time, or
  briefly entered and left again, and there is no way to tell from the
  boundary states. These gaps are **skipped** (left `-1`) rather than
  guessed.
- **`11`** (in → in): under normal conditions, script 1's logic-fill rule
  should already have resolved these to `IN_NEST = 1`. If a `-1` frame is
  still found in a type-11 gap here, it is treated as an expected but
  logged anomaly (see script 1's documented edge case) and skipped rather
  than silently miscounted.
- **`01`** (out → in) and **`10`** (in → out): these are the gaps that
  matter — the animal's state is known to differ between the two
  endpoints, so **exactly one transition occurred somewhere inside the
  gap**. These are the only gap types eligible for binary search.

**3. Filter by duration.** Among `01`/`10` gaps, any shorter than
`MIN_GAP_DURATION_FOR_BINARY_SEARCH` (default 30 seconds) are left `-1`
rather than queued for review — a gap this short contributes little to the
overall time-in-nest estimate relative to the reviewer time it would cost
to resolve precisely.

**4. Binary search the remaining gaps.** This is the core algorithm, and it
relies on one key assumption: **within a `01` or `10` gap, the animal's
in-nest state is monotonic** — it changes exactly once, at some unknown
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
currently in the nest. What that answer implies — and which half of the
segment gets filled immediately versus searched further — depends on which
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

In both directions the reviewer is answering the same underlying question —
"has the transition occurred by this frame?" — but which half is resolved
immediately and which half continues to be searched is flipped, because
which endpoint state is "known" differs between an entry and an exit.

**5. Convergence and termination.** Recursion on a gap's subtasks
terminates in one of two ways:

- The segment being searched shrinks to zero width (`seg_start > seg_end`),
  at which point the two already-filled halves fully account for every
  frame originally in the gap — every subtask either fills a contiguous
  block outright or hands off exactly the untouched remainder to a new
  subtask, so no frame is ever double-counted or dropped.
- As a practical shortcut, once a candidate segment's *duration* drops
  below `FILL_ENTIRE_SEGMENT_IF_DURATION_LESS_THAN_IN_MINUTES` (default 1
  minute), the entire remaining segment is filled from a single answer
  instead of continuing to subdivide down to individual frames — sub-minute
  precision on exactly which frame a transition occurred is not meaningful
  for this pipeline's purposes, so this trades a small amount of possible
  imprecision for a large reduction in reviewer clicks.

**6. Determine the final gap classification.** Once every subtask for
every gap has been answered, all resulting frame decisions are merged with
the unchanged `DETECTED` frame states (built with vectorized NumPy
operations rather than a per-row loop) into the authoritative final
`IN_NEST` value for every frame, along with `BINARY_SEARCH` and
`FILL_SOURCE` bookkeeping.

**7. Write outputs and report.** The final table is written to a new
SQLite file, and a detailed summary report is generated with multiple
internal balance checks (e.g. that every accounted-for frame category sums
back to the original total) — if any check fails, an `IntegrityError` is
raised rather than silently emitting a report with unexplained
inconsistencies.

### Key Design Decisions & Assumptions
- **Binary search is appropriate here specifically because `01`/`10` gaps
  are guaranteed (by construction) to contain exactly one transition.**
  This monotonicity assumption is what makes "ask about the midpoint, then
  recurse on one half" valid — it would not be valid for `00` gaps (no
  known transition at all) or gaps that could contain multiple transitions,
  which is exactly why those cases are filtered out *before* the search
  begins rather than handled by it.
- **Nearest-frame video resolution, not backward-only or exact-only.**
  When a requested global frame doesn't map exactly onto any loaded video's
  coverage, the script resolves to the nearer of the closest preceding or
  succeeding available frame (preceding wins exact ties) rather than
  arbitrarily using the first video or failing outright. This same
  resolution strategy is reused in scripts 3 and 4 so that a QC reviewer
  later sees the same substitute frame the original binary-search reviewer
  saw for the same gap.
- **Video files are opened once and cached**, not reopened for every frame
  extraction, since a single review session may request hundreds of frames
  from the same handful of video files.
- **Integrity checks are deliberately strict.** The summary report
  recomputes several frame-count totals through independent paths and
  raises rather than continues if they disagree — the report's numbers are
  used as the pipeline's audit trail, so an inconsistency there should stop
  the run rather than be written down as if it were trustworthy.

### Do NOT Modify
- `DB_FPS = 30` and `FRAME_CONVERSION = 2` encode the relationship between
  the LMT database frame rate and the video frame rate; wrong values
  silently extract the wrong frames.
- Video filename parsing (`get_start_frame()`) requires a `t<digits>`
  segment immediately before the file extension; any other convention
  breaks video-to-frame mapping.
- `BINARY_SEARCH = 1` must be set only for frames in gaps that were
  actually routed to the reviewer (i.e. `01`/`10` gaps above the duration
  threshold) — `4.lmt_qc_validator.py` relies on this distinction.
- Table name `GAP_FILL_ANALYSIS` and the `BINARY_SEARCH`/`FILL_SOURCE`
  columns are read by name in `3.lmt_qc_sampler.py` and
  `4.lmt_qc_validator.py`.

### Open Source Notes
- **External dependencies**: `opencv-python` (`cv2`), `numpy`, `pandas`,
  `Pillow` (`PIL.Image`, `PIL.ImageTk`), `tkinter`.
- **Standard library**: `os`, `re`, `copy`, `sqlite3`, `datetime`, `time`.
- **Configuration files / environment variables**: none; all thresholds
  (`MIN_GAP_DURATION_FOR_BINARY_SEARCH = 30s`,
  `FILL_ENTIRE_SEGMENT_IF_DURATION_LESS_THAN_IN_MINUTES = 1 min`, `DB_FPS`,
  `FRAME_CONVERSION`, `FPS_TOLERANCE = 0.5`) are hardcoded module-level
  constants.
- **Expected directory structure**: none required beyond a writable output
  folder (`_binsearch_tmp` is created automatically and cleaned up
  automatically, including on early window close).
- **Platform assumptions**: requires Tkinter + a working OpenCV video
  backend; not headless-safe. Video codec support depends on the local
  OpenCV build. Because video files are cached open for the review session,
  reviewing a very large number of distinct video files in one sitting
  could approach a platform's open-file-handle limit.

---

## Script: `3.lmt_qc_sampler.py`

### Overview
This script exists to make the pipeline's output auditable. Scripts 1 and 2
each make decisions — one by static geometric rule, one by human binary
search — and both could still be systematically wrong in ways that are hard
to notice by inspection alone. Rather than trusting either process blindly,
this script draws independently-sized random samples from each category of
decision (raw detections, logic-filled gaps, binary-search-filled gaps) so
that `4.lmt_qc_validator.py` can measure each one's real-world accuracy
separately.

Loads a `2.lmt_binary_search.py` output (or a legacy equivalent), splits
rows into three QC "pools" — `DETECTED`, `BINARY_SEARCH`, or `LOGIC` — based
on how each frame's classification was produced, draws a random sample of a
user-specified size from each selected pool, extracts the corresponding
video frame as a screenshot, and records everything in a new SQLite table
for manual review.

### Inputs

| Input | Type | Purpose |
|---|---|---|
| `lmt_binary_search_<date>.sqlite` (script 2 output) | SQLite database, table `GAP_FILL_ANALYSIS` | Fully classified per-frame data to draw QC samples from. |
| LMT video files (`*.mp4`) | Video | Source of the screenshot images for each sampled frame. |
| Output folder | Directory | Where per-pool results are written. |
| Animal ID | Integer | Recorded alongside each sample. |
| Sample count | Integer | Applied independently to each selected pool. |
| QC pool selection | Checkboxes | Any of `DETECTED`, `BINARY_SEARCH`, `LOGIC` rows. |

### Outputs

Per selected pool, written to `output_folder/{qc_mode}_{timestamp}/`:

| Output | Type | Purpose |
|---|---|---|
| `lmt_qc_sampler_<qc_mode>_<YYYY-MM-DD>.sqlite` | SQLite database, table `QC_ASSUMED_SAMPLES` | Metadata for the drawn QC sample. |
| `Screenshots/S####_A<animal_id>_G<frame>_<video>.png` | PNG image | Extracted video frame for each sampled row. |

`QC_ASSUMED_SAMPLES` columns:

| Column | Meaning |
|---|---|
| `sample_id` | 1-based counter, also embedded in the screenshot filename. |
| `animal_id` | Animal ID entered by the user. |
| `video` | Basename of the video the screenshot came from. |
| `frame_global` | The actual (possibly nearest-neighbor-resolved) frame captured. |
| `requested_frame` | The originally sampled `FRAMENUMBER`, before any resolution. |
| `IN_NEST` | Classification carried over from the source row. |
| `ASSUMPTION_TYPE` | `"DETECTED"` or `"ASSUMED"`, carried over. |
| `FILL_SOURCE` | Carried over if present in the source; otherwise defaults to the requested pool. |
| `GAP_START_FRAME` / `GAP_END_FRAME` | Carried over; populated only for `ASSUMED` rows. |
| `screenshot` | Filename of the extracted PNG, relative to `Screenshots/`. |
| `QC_MODE` | Which pool this row belongs to (`"DETECTED"` / `"BINARY_SEARCH"` / `"LOGIC"`). |

### Processing Steps
1. **Configure the run** via the GUI: source database, videos, output
   folder, animal ID, sample size, and which pools to draw from.
2. **Guard existing output** — if a pool's output folder for today's date
   already contains files (e.g. from an earlier run), ask for confirmation
   before continuing rather than silently overwriting.
3. **Detect the source schema** — whether the file has the modern
   `FILL_SOURCE` column, a legacy `BINARY_SEARCH` flag, or neither — and
   load `GAP_FILL_ANALYSIS` (or the legacy `ASSUMED_FRAMES` table)
   accordingly.
4. **Filter to the requested pool.** `DETECTED` selects LMT-observed rows
   outright; `BINARY_SEARCH` and `LOGIC` each select `ASSUMED` rows with a
   resolved (`0`/`1`) `IN_NEST` value and a matching `FILL_SOURCE`,
   deliberately excluding any row still stuck at `-1`. Attempting to sample
   a pool the source file has no data for (e.g. `BINARY_SEARCH` from a
   file that skipped script 2) produces a clear, actionable error rather
   than a raw `KeyError`.
5. **Draw a random sample**, bounded to the pool's actual size, using an
   explicit, freshly-generated random seed that is reported back to the
   user — the draw is still effectively random every run, but the exact
   sample can be reproduced later if the seed is recorded.
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
  value — script 4 reads it from the first row only and does not support a
  mixed-mode file.

### Open Source Notes
- **External dependencies**: `opencv-python` (`cv2`), `pandas`, `tkinter`.
- **Standard library**: `os`, `re`, `random`, `sqlite3`, `datetime`.
- **Configuration files / environment variables**: none; `DB_FPS`,
  `FRAME_CONVERSION`, `FPS_TOLERANCE`, and the three `QC_MODE_*` constants
  are hardcoded and duplicated from `2.lmt_binary_search.py`.
- **Expected directory structure**: creates
  `{output_folder}/{qc_mode}_{timestamp}/Screenshots/` automatically.
- **Platform assumptions**: requires Tkinter + OpenCV; not headless-safe.

---

## Script: `4.lmt_qc_validator.py`

### Overview
This script exists to close the loop: it is the only part of the pipeline
that produces an actual accuracy number. Everything upstream — geometric
heuristics, binary search, random sampling — is a mechanism for producing a
classification; this script is where a human directly compares that
classification against what they can see with their own eyes, for a
statistically meaningful sample, and where the pipeline's real-world error
rate is finally measured rather than assumed.

Loads a `3.lmt_qc_sampler.py` output, determines the active QC mode from the
`QC_MODE` column (with legacy fallbacks), filters to the eligible rows for
that mode, and presents each sampled screenshot — plus, for `ASSUMED`-type
modes, the gap's before/after boundary frames re-extracted from video — to a
human reviewer for manual "IN NEST"/"OUT OF NEST" labeling. Saves progress
after every label, and on completion computes a two-class confusion matrix
(algorithm prediction = `IN_NEST` vs. human ground truth = `MANUAL_QC`) and
writes a text validation report.

### Inputs

| Input | Type | Purpose |
|---|---|---|
| `lmt_qc_sampler_<qc_mode>_<timestamp>.sqlite` (script 3 output) | SQLite database, table `QC_ASSUMED_SAMPLES` | The drawn QC sample to label. |
| Screenshot folder | Directory of PNGs | Location of images referenced by the `screenshot` column. |
| LMT video files (optional) | Video | Enables re-extracting gap boundary frames for the three-panel view. |

### Outputs

| Output | Type | Purpose |
|---|---|---|
| `lmt_qc_validator_<YYYY-MM-DD>.sqlite` (written into the screenshot folder) | SQLite database, table `QC_ASSUMED_SAMPLES` | The same sample table with human labels recorded. |
| `lmt_qc_validator_<YYYY-MM-DD>.txt` | Text report | Confusion matrix and accuracy/error-rate/sensitivity/specificity metrics, plus false-positive/false-negative screenshot lists. |

Columns are identical to script 3's output table, plus:

| Column | Meaning |
|---|---|
| `MANUAL_QC` | `1` = human says IN NEST, `0` = human says OUT OF NEST, `null`/`NaN` = not yet labeled. |

### Processing Steps
1. **Load the sample** and resolve the active QC mode from the first row's
   `QC_MODE` value (falling back to a legacy `"ASSUMED"` mode for older
   files), then re-apply the same eligibility filter script 3 used to build
   that pool — a defensive re-check against a hand-edited or unexpected
   input file.
2. **Present one sample at a time.** `DETECTED`-mode samples show the
   single pre-extracted screenshot. `BINARY_SEARCH`/`LOGIC`/legacy
   `ASSUMED` samples show a three-panel view — last known frame before the
   gap, the sampled QC frame, and first known frame after the gap — all
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
  2, 3, and 4 on purpose** — accuracy validation is only meaningful if the
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
  table to choose filter/display logic for the whole session — input files
  must not mix QC modes.
- `screenshot` values must remain resolvable as
  `os.path.join(screenshot_folder, screenshot_nm)` — this script must be
  pointed at the same folder script 3 wrote screenshots into.
- The incremental per-row save relies on `sample_id` existing and being
  unique per row, as guaranteed by script 3's output.

### Open Source Notes
- **External dependencies**: `opencv-python` (`cv2`), `pandas`, `Pillow`
  (`PIL.Image`, `PIL.ImageTk`), `tkinter`.
- **Standard library**: `os`, `re`, `sqlite3`, `datetime`, `tempfile`,
  `uuid`.
- **Configuration files / environment variables**: none; `DB_FPS`,
  `FRAME_CONVERSION`, `FPS_TOLERANCE`, and QC mode constants are hardcoded
  and duplicated from `2.lmt_binary_search.py` / `3.lmt_qc_sampler.py`.
- **Expected directory structure**: expects the screenshot folder produced
  by `3.lmt_qc_sampler.py`.
- **Platform assumptions**: requires Tkinter + OpenCV; uses the OS temp
  directory for scratch boundary-frame images (always cleaned up, including
  on error); not headless-safe. Video files are cached open for the
  session and released when the window is closed.

