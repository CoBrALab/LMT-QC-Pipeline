# LMT QC Pipeline Documentation

This README documents a 5-script pipeline that takes a Live Mouse Tracker (LMT)
SQLite output, cleans it, infers a mouse's in-nest/out-of-nest state for frames
where the tracker lost detection, resolves ambiguous gaps via a human-in-the-loop
binary-search video review, and then draws QC samples to measure the pipeline's
accuracy against human judgment.

Scripts: `0.Preprocessing.py`, `1.lmt_gap_fill.py`,
`2.lmt_binary_search.py`, `3.lmt_qc_sampler.py`, `4.lmt_qc_validator.py`.

---
## Script: `0.Preprocessing.py`

### Overview
This script creates a cleaned copy of a raw LMT Output SQLite database **without modifying the original file**. It removes invalid rows from the `DETECTION` table where:

```sql
FRONT_X = -1
```

After deletion, it runs `VACUUM` to reclaim disk space.

Before deleting any rows, the script validates the documented assumption that:

```text
FRONT_X = -1
```

also implies:

```text
FRONT_Y = -1
FRONT_Z = -1
BACK_X  = -1
BACK_Y  = -1
BACK_Z  = -1
```

If this assumption is violated, the script reports the number of mismatched rows and asks the user whether to continue.

Additional safety features include:

- Verifies that the `DETECTION` table exists before processing.
- Prevents accidental overwriting of an existing output file by requesting confirmation.
- Wraps all database operations in exception handling to ensure the SQLite connection is always closed.

### Inputs
- LMT Output SQLite
  - Table `DETECTION`
    - Columns
      - `FRONT_X` (Determines which rows are deleted)
      - `FRONT_Y` (Used only for assumption validation)
      - `FRONT_Z` (Used only for assumption validation)
      - `BACK_X` (Used only for assumption validation)
      - `BACK_Y` (Used only for assumption validation)
      - `BACK_Z` (Used only for assumption validation))
- Output folder path

### Outputs
- `{original_name}_processed.sqlite` 

---

## Script: `1.lmt_gap_fill.py`

### Overview
This script processes detection data for a single animal (`ANIMALID`) and fills gaps between consecutive detections using rule-based logic.

For every pair of consecutive detected frames:

- Determines whether missing frames (a gap) exist.
- Records detected frames as `ASSUMPTION_TYPE = "DETECTED"` using their actual in-nest status based on the user-defined nest ROI.
- Records missing frames as `ASSUMPTION_TYPE = "ASSUMED"`.

For assumed frames:

- `IN_NEST = 1` (logic-filled) only if
  - the animal is inside the nest ROI at the start of the gap **and**
  - remains inside the larger nest buffer ROI at the end of the gap.
- Otherwise:
  - `IN_NEST = -1` (uncertain), allowing `2.lmt_binary_search.py` to resolve the gap using binary search.

Results are written to a new SQLite database.

### Inputs
- `{original_name}_processed.sqlite`
  - Table `DETECTION`
    - Columns
      - `FRAMENUMBER`
      - `MASS_X`
      - `MASS_Y`
      - `ANIMALID`
- Animal ID (integer)
- Nest ROI: xmin, xmax, ymin, ymax (float)
- Buffer ROI: xmin, xmax, ymin, ymax (float) (must be larger than the nest ROI)
- Output folder path

### Outputs
- `lmt_gap_fill_<YYYY-MM-DD_HH-MM-SS>.sqlite`
  - Table `GAP_FILL_ANALYSIS`
    - Columns
      - `FRAMENUMBER`
      - `IN_NEST` (1 = in nest, 0 = out of nest (detected rows), -1 = uncertain)
      - `ASSUMPTION_TYPE` ("DETECTED" / "ASSUMED")
      - `GAP_START_FRAME` (Last detected frame before a gap (NULL for detected rows))
      - `GAP_END_FRAME` (First detected frame after a gap (NULL for detected rows))

### Assumed Frame Convention
`ASSUMED` rows must only contain:

- `IN_NEST = 1`
- `IN_NEST = -1`

They must **never** contain `0`.

`2.lmt_binary_search.py` relies on this convention when selecting:

```python
df_neg = df[df["IN_NEST"] == -1]
```

### Gap Boundary Meaning
The following definitions must remain unchanged:

- `GAP_START_FRAME` = last detected frame before the gap
- `GAP_END_FRAME` = first detected frame after the gap

`2.lmt_binary_search.py` depends on these definitions for gap grouping and classification.

### Do Not Modify
- ROI test uses strict inequality (`<`, not `<=`), changing to `<=` alters boundary behaviour
- `IN_NEST = −1` must remain −1; downstream scripts (`2.lmt_binary_search.py`, `4.lmt_qc_validator.py`) filter on this exact value
- Table name `GAP_FILL_ANALYSIS` is hardcoded in 2.lmt_binary_search.py's read query

---

## Script: 2.lmt_binary_search.py

### Overview
Loads a `1.lmt_gap_fill.py` output and isolates all ASSUMED frames still marked IN_NEST = -1 (uncertain). Groups them into gaps, classifies each gap's boundary type (00/01/10/11), and skips gaps that carry no directional information (00), are already implicitly resolved (11), or are shorter than a configurable duration threshold. Remaining gaps are queued for an interactive, GUI-driven binary search: the reviewer is shown a three-panel view and answers "IN NEST" or "OUT OF NEST," recursively narrowing the segment until the entry/exit point is resolved. Once all gaps are processed, it writes a final classification for every frame (with FILL_SOURCE/BINARY_SEARCH bookkeeping columns) plus a detailed plain-text summary report with multiple internal integrity checks.

### Inputs
- `lmt_gap_fill_<date>.sqlite` (If a gap's GAP_START_FRAME/GAP_END_FRAME does not correspond to a detected FRAMENUMBER, or a detected frame's IN_NEST value is anything other than 0/1, gap classification raises a clear "Data Integrity Error" dialog)
- LMT output video files (.mp4)
- Output folder path

### Outputs
- `lmt_binary_search_<YYYY-MM-DD>.sqlite`
  - Table `GAP_FILL_ANALYSIS`
    - Columns
      - `FRAMENUMBER` 
      - `IN_NEST` (1 = in nest, 0 = out of nest, -1 = 00 gaps, shorter than configurable duration threshold)
      - `ASSUMPTION_TYPE` ("DETECTED" / "ASSUMED")
      - `GAP_START_FRAME` (Last detected frame before a gap (NULL for detected rows))
      - `GAP_END_FRAME` (First detected frame after a gap (NULL for detected rows))
      - `BINARY_SEARCH` (0 (Binary search was not performed) / 1 (Binary search was performed))
      - `FILL_SOURCE` ("DETECTED" / "LOGIC" / "BINARY_SEARCH" / "UNKNOWN")
- `LMT_Summary_<YYYY-MM-DD>.txt`
- `_binsearch_tmp/` (Temp image cache; Scratch PNGs extracted from video during interactive review; Deleted at successful completion (or) if the window is closed early)

### Do NOT Modify
- `DB_FPS = 30` and `FRAME_CONVERSION = 2`: these encode the relationship between the LMT database frame rate (30 fps) and the video frame rate (15 fps), wrong values would extract the wrong frames
- Video filename parsing in `get_start_frame()` assumes the pattern `...t<int>.<ext>`, any other naming convention will break video-to-frame mapping
- `BINARY_SEARCH = 1` is set only for frames in searchable gaps (above threshold), this distinction is used by 4.lmt_qc_validator.py

### Open Source Notes

#### External dependencies
External dependencies: opencv-python (cv2), numpy (newly required by this update for vectorized classification), pandas, Pillow (PIL.Image, PIL.ImageTk), tkinter.

Standard library: os, re (newly used for robust filename parsing), copy, sqlite3, datetime, time.

#### Configuration files / environment variables
Configuration files / environment variables: none; all thresholds (MIN_GAP_DURATION_FOR_BINARY_SEARCH = 30s, FILL_ENTIRE_SEGMENT_IF_DURATION_LESS_THAN_IN_MINUTES = 1 min, DB_FPS, FRAME_CONVERSION, and the new FPS_TOLERANCE = 0.5) are hardcoded module-level constants, not exposed via the GUI or a config file.

#### Expected directory structure
Expected directory structure: none required beyond a writable output folder (a _binsearch_tmp subfolder is created automatically, and is now also cleaned up on early window close).

#### Platform assumptions
Platform assumptions: requires Tkinter + a working OpenCV video backend; not headless-safe. Video codec support depends on the local OpenCV build.

Video files are now kept open (cached) for the duration of the review session rather than being reopened per frame — on platforms/filesystems with a low limit on simultaneously open file handles, reviewing a very large number of distinct video files in one session could approach that limit (previously each file was opened and closed immediately, avoiding this, at the cost of much slower repeated re-opening).

---

### Script: `3.lmt_qc_sampler.py`

**Core Logic**
Loads a `2.lmt_binary_search.py` output (or a legacy equivalent), splits rows
into three QC "pools" — `DETECTED`, `BINARY_SEARCH`, or `LOGIC` — based on how
each frame's classification was produced, draws a random sample of a
user-specified size from each selected pool, extracts the corresponding video
frame as a screenshot, and records everything in a new SQLite table for manual
review in script 4.

**Inputs**

| File | Type | Purpose |
|---|---|---|
| `lmt_binary_search_<date>.sqlite` (script 2 output) | SQLite database | Fully classified per-frame data to draw QC samples from. |
| One or more LMT video files (`*.mp4`) | Video | Source of the screenshot images captured for each sampled frame. |

SQLite input details:
- **Filename**: user-selected (expected to be a `2.lmt_binary_search.py`
  output; falls back to legacy naming if needed).
- **Table**: `GAP_FILL_ANALYSIS` (preferred) or legacy `ASSUMED_FRAMES` (used
  if `GAP_FILL_ANALYSIS` is not found in the file).
- **Columns used**: `FRAMENUMBER`, `IN_NEST`, `ASSUMPTION_TYPE`,
  `FILL_SOURCE` (optional/new), `BINARY_SEARCH` (optional/legacy),
  `GAP_START_FRAME`, `GAP_END_FRAME`.

Video input details: same naming convention and frame-rate assumptions as
script 2 (`DB_FPS = 30`, `FRAME_CONVERSION = 2`).

Additional (non-file) GUI inputs: `Animal ID`, number of samples per pool, and
which of the three pools (`DETECTED`/`BINARY_SEARCH`/`LOGIC`) to sample.

**Outputs**

| File | Type | Purpose |
|---|---|---|
| `lmt_qc_sampler_<qc_mode>_<YYYY-MM-DD>.sqlite` (one per selected pool) | SQLite database | Metadata for the drawn QC sample, consumed by script 4. |
| `Screenshots/S####_A<animal_id>_G<frame>_<video>.png` | PNG image | Extracted video frame for each sampled row. |

Output folder structure: `output_folder/{qc_mode}_{timestamp}/` containing the
SQLite file directly, and a `Screenshots/` subfolder with the PNGs.

SQLite output details:
- **Filename**: `lmt_qc_sampler_<qc_mode>_<date>.sqlite`
- **Table**: `QC_ASSUMED_SAMPLES`
- **Columns**:
  - `sample_id` (int) — 1-based counter, also embedded in the screenshot
    filename.
  - `animal_id` (int) — animal ID entered by the user.
  - `video` (str) — basename of the video file the screenshot was extracted
    from.
  - `frame_global` (int) — the actual (possibly nearest-neighbor-resolved)
    global frame number captured.
  - `requested_frame` (int) — the originally sampled `FRAMENUMBER` before any
    nearest-frame resolution.
  - `IN_NEST` (int: `1`/`0`/`-1`) — classification carried over from the
    source row.
  - `ASSUMPTION_TYPE` (str) — `"DETECTED"` or `"ASSUMED"`, carried over.
  - `FILL_SOURCE` (str) — carried over if present in source, else defaults to
    the requested `qc_mode`.
  - `GAP_START_FRAME` / `GAP_END_FRAME` (int or `None`) — carried over; only
    populated for `ASSUMED` rows.
  - `screenshot` (str) — filename of the extracted PNG (relative to the
    `Screenshots/` folder).
  - `QC_MODE` (str: `"DETECTED"`/`"BINARY_SEARCH"`/`"LOGIC"`) — which pool this
    sample belongs to; read by script 4 to determine filtering/display logic.

**Do NOT Modify**
- Table name `QC_ASSUMED_SAMPLES` and all listed column names are required by
  `4.lmt_qc_validator.py`'s `load_database()`.
- The `QC_MODE` value written to every row must be one of the three literal
  strings `DETECTED`/`BINARY_SEARCH`/`LOGIC` — script 4 reads the value from
  the *first row only* (`df_full["QC_MODE"].iloc[0]`) to decide the filtering
  mode for the entire file, so a mixed-mode file is not supported.
- The screenshot filename format
  (`S{counter:04d}_A{animal_id}_G{resolved_frame}_{video_name}.png`) must
  remain resolvable relative to the `Screenshots/` subfolder for script 4 to
  locate images (`os.path.join(screenshot_folder, screenshot_nm)`), where
  `screenshot_folder` is the folder the user points script 4 at.
- Execution order: must run **after** `2.lmt_binary_search.py` and **before**
  `4.lmt_qc_validator.py`.

**Open Source Notes**
- **External dependencies**: `opencv-python` (`cv2`), `pandas`, `tkinter`.
  Standard library: `os`, `sqlite3`, `datetime`.
- **Configuration files / environment variables**: none; `DB_FPS`,
  `FRAME_CONVERSION`, and the three `QC_MODE_*` constants are hardcoded and
  duplicated from script 2.
- **Expected directory structure**: creates
  `{output_folder}/{qc_mode}_{timestamp}/Screenshots/` automatically.
- **Platform assumptions**: requires Tkinter + OpenCV; not headless-safe.

---

### Script: `4.lmt_qc_validator.py`

**Core Logic**
Loads a `3.lmt_qc_sampler.py` output, determines the active QC mode from the
`QC_MODE` column (with legacy fallbacks), filters to the eligible rows for that
mode, and presents each sampled screenshot (plus, for `ASSUMED`-type modes, the
gap's before/after boundary frames re-extracted from video) to a human reviewer
for manual "IN NEST"/"OUT OF NEST" labeling. Saves progress after every label,
and on completion computes a two-class confusion matrix (algorithm prediction
= `IN_NEST` vs. human ground truth = `MANUAL_QC`) and writes a text validation
report.

**Inputs**

| File | Type | Purpose |
|---|---|---|
| `lmt_qc_sampler_<qc_mode>_<timestamp>.sqlite` (script 3 output) | SQLite database | The drawn QC sample to be manually labeled. |
| Screenshot folder | Directory of PNGs | Location of the images referenced by the `screenshot` column. |
| LMT video files (`*.mp4`, optional) | Video | If provided, enables re-extracting the gap's before/after boundary frames for a three-panel review display. |

SQLite input details:
- **Filename**: user-selected (expected to be a `3.lmt_qc_sampler.py` output).
- **Table**: `QC_ASSUMED_SAMPLES`
- **Columns used**: `screenshot`, `video`, `frame_global`, `IN_NEST`,
  `ASSUMPTION_TYPE`, `FILL_SOURCE` (optional), `BINARY_SEARCH` (optional,
  legacy), `GAP_START_FRAME`, `GAP_END_FRAME`, `QC_MODE` (optional — absence
  implies the legacy `"ASSUMED"` mode), `MANUAL_QC` (added by this script if
  absent).

**Outputs**

| File | Type | Purpose |
|---|---|---|
| `lmt_qc_validator_<YYYY-MM-DD>.sqlite` (written into the screenshot folder) | SQLite database | The same sample table with human labels (`MANUAL_QC`) recorded; saved after every label for durability. |
| `lmt_qc_validator_<YYYY-MM-DD>.txt` (written into the screenshot folder) | Text report | Confusion matrix and accuracy/error-rate/sensitivity/specificity metrics, plus lists of false-positive/false-negative screenshot filenames. |

SQLite output details:
- **Filename**: `lmt_qc_validator_<date>.sqlite`
- **Table**: `QC_ASSUMED_SAMPLES`
- **Columns**: identical to the script 3 output table, plus:
  - `MANUAL_QC` (nullable int: `1` = human says IN NEST, `0` = human says OUT
    OF NEST, `null`/`NaN` = not yet labeled).

**Do NOT Modify**
- This is the terminal script in the pipeline; nothing downstream consumes its
  output within this repository, but its report format (TP/FP/TN/FN, accuracy,
  error rate, sensitivity, specificity) is the pipeline's accuracy measurement
  and should be treated as the canonical QC metric definition.
- Relies on the `QC_MODE` value from the **first row** of the loaded table to
  choose the filter/display logic for the entire session — files must not mix
  QC modes.
- Relies on the `screenshot` column values being resolvable as
  `os.path.join(screenshot_folder, screenshot_nm)` — the user must point this
  script at the same folder script 3 wrote screenshots into (the `Screenshots/`
  subfolder), not the parent pool folder.
- Boundary/three-panel display for `ASSUMED`-type modes requires
  `GAP_START_FRAME`/`GAP_END_FRAME` to be present and non-null, and requires
  videos to be loaded; otherwise it silently falls back to single-panel display
  of the pre-extracted screenshot.

**Open Source Notes**
- **External dependencies**: `opencv-python` (`cv2`), `pandas`, `Pillow`
  (`PIL.Image`, `PIL.ImageTk`), `tkinter`. Standard library: `os`, `sqlite3`,
  `datetime`, `tempfile`, `uuid`.
- **Configuration files / environment variables**: none; `DB_FPS`,
  `FRAME_CONVERSION`, and QC mode constants are hardcoded and duplicated from
  scripts 2/3.
- **Expected directory structure**: expects the screenshot folder produced by
  script 3 (i.e., the `Screenshots/` subfolder, or wherever the `screenshot`
  filenames are resolvable relative to).
- **Platform assumptions**: requires Tkinter + OpenCV; uses the OS temp
  directory (`tempfile.gettempdir()`) for scratch boundary-frame images; not
  headless-safe.

---

## 3. Workflow Summary

**Execution order**

1. `0.Preprocessing.py` — clean raw LMT SQLite (remove invalid detections).
2. `1.lmt_gap_fill.py` — classify detected frames + logic-fill/flag gaps per animal.
3. `2.lmt_binary_search.py` — human-in-the-loop resolution of ambiguous gaps via video.
4. `3.lmt_qc_sampler.py` — draw random QC samples per pool + extract screenshots.
5. `4.lmt_qc_validator.py` — manual labeling of QC samples + accuracy metrics.

**Workflow diagram**

```mermaid
flowchart TD
    A["Raw LMT Output SQLite<br/>(DETECTION table)"] --> B["0.Preprocessing.py<br/>remove FRONT_X = -1 rows, VACUUM"]
    B --> C["{name}_processed.sqlite"]
    C --> D["1.lmt_gap_fill.py<br/>per-animal gap detection + nest ROI logic"]
    D --> E["lmt_gap_fill_&lt;timestamp&gt;.sqlite<br/>(GAP_FILL_ANALYSIS)"]
    V1["LMT video files (*.mp4)"] --> F
    E --> F["2.lmt_binary_search.py<br/>interactive binary-search review of<br/>IN_NEST = -1 gaps"]
    F --> G["lmt_binary_search_&lt;date&gt;.sqlite<br/>(GAP_FILL_ANALYSIS + BINARY_SEARCH/FILL_SOURCE)"]
    F --> G2["LMT_Summary_&lt;date&gt;.txt"]
    V2["LMT video files (*.mp4)"] --> H
    G --> H["3.lmt_qc_sampler.py<br/>random sampling per pool<br/>(DETECTED / BINARY_SEARCH / LOGIC)"]
    H --> I["lmt_qc_sampler_&lt;qc_mode&gt;_&lt;date&gt;.sqlite<br/>(QC_ASSUMED_SAMPLES)"]
    H --> I2["Screenshots/*.png"]
    V3["LMT video files (*.mp4, optional)"] --> J
    I --> J["4.lmt_qc_validator.py<br/>manual IN/OUT labeling"]
    I2 --> J
    J --> K["lmt_qc_validator_&lt;date&gt;.sqlite<br/>(QC_ASSUMED_SAMPLES + MANUAL_QC)"]
    J --> L["lmt_qc_validator_&lt;date&gt;.txt<br/>confusion matrix + accuracy metrics"]
```

**Data flow between scripts**

- `0.Preprocessing.py` → `1.lmt_gap_fill.py`: cleaned `DETECTION` table (copy of raw LMT SQLite,
  minus invalid rows).
- `1.lmt_gap_fill.py` → `2.lmt_binary_search.py`: `GAP_FILL_ANALYSIS` table (`FRAMENUMBER`, `IN_NEST`,
  `ASSUMPTION_TYPE`, `GAP_START_FRAME`, `GAP_END_FRAME`); consumed alongside
  the raw LMT video files.
- `2.lmt_binary_search.py` → `3.lmt_qc_sampler.py`: updated `GAP_FILL_ANALYSIS` table (same columns +
  `BINARY_SEARCH`, `FILL_SOURCE`); consumed alongside the same LMT video files.
- `3.lmt_qc_sampler.py` → `4.lmt_qc_validator.py`: `QC_ASSUMED_SAMPLES` table + a `Screenshots/` folder of
  PNGs; video files are optional input to script 4 (only needed for the
  three-panel boundary view).

**Intermediate files/databases produced**
- `{name}_processed.sqlite` (script 0)
- `lmt_gap_fill_<timestamp>.sqlite` (script 1)
- `lmt_binary_search_<date>.sqlite` + `LMT_Summary_<date>.txt` + transient
  `_binsearch_tmp/` cache (script 2)
- `lmt_qc_sampler_<qc_mode>_<date>.sqlite` + `Screenshots/*.png` (script 3, one
  set per selected pool)

**Final outputs**
- `lmt_qc_validator_<date>.sqlite` — QC sample table with human labels
  (`MANUAL_QC`).
- `lmt_qc_validator_<date>.txt` — final accuracy report (confusion matrix,
  accuracy, error rate, sensitivity, specificity) plus lists of false-positive
  and false-negative screenshots for review.
