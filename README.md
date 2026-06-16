### Script: `1.lmt_qc_sampler.py`

**Purpose:**
- Reads raw LMT tracking detections and classifies each frame as IN NEST, OUT OF NEST, or unknown (-1) by comparing animal position against user-defined ROI coordinates.
- Fills gaps between detections using a buffer-zone heuristic, outputs a complete per-frame classification table.

**Inputs:**
- LMT SQLite database (table: `DETECTION`, columns: `FRAMENUMBER`, `MASS_X`, `MASS_Y`, `ANIMALID`)
- Animal ID (integer)
- Nest ROI: xmin, xmax, ymin, ymax (float)
- Buffer ROI: xmin, xmax, ymin, ymax (float) — must be larger than nest ROI
- Output folder path

**Outputs:**
- `lmt_gap_fill_<date>.sqlite` — table `GAP_FILL_ANALYSIS` with columns: `FRAMENUMBER`, `IN_NEST` (1/0/−1), `ASSUMPTION_TYPE` ("DETECTED"/"ASSUMED"), `GAP_START_FRAME`, `GAP_END_FRAME`

**Core Logic:**
- Iterates consecutive detection pairs; classifies detected frames by point-in-rectangle test against the nest ROI
- Gap frames assigned `IN_NEST = 1` if the frame before the gap is inside the nest and the frame after is inside the buffer; otherwise `IN_NEST = −1`

**Do NOT modify:**
- ROI test uses strict inequality (`<`, not `<=`) — changing to `<=` alters boundary behaviour
- `IN_NEST = −1` sentinel must remain −1; downstream scripts (`2.lmt_binary_search.py`, `4.lmt_qc_validator.py`) filter on this exact value
- Table name `GAP_FILL_ANALYSIS` — hardcoded in 2.lmt_binary_search.py's read query

**Open-source notes:**
- ROI coordinates are pixel-space values specific to each experimental arena; defaults in the GUI (200–350, 50–175) are placeholders and will be wrong for any other setup
- Buffer ROI must fully contain the nest ROI or gap-fill logic produces incorrect results (this is not validated in code)
- No input validation on ROI coordinate ordering (xmin < xmax, ymin < ymax)

---

### Script: `2.lmt_binary_search.py`

**Purpose:**
- Resolves `IN_NEST = −1` (unknown) assumed frames with a gap duration greater than 30s from `1.3.lmt_qc_sampler.py` output using a human-in-the-loop binary search over video frames.
- Outputs a complete frame classification table preserving all rows from the 1.3.lmt_qc_sampler.py input, with a `BINARY_SEARCH` column added.

**Inputs:**
- `lmt_gap_fill_<date>.sqlite` (table: `GAP_FILL_ANALYSIS`)
- LMT video files (.mp4) — filenames must encode the start frame as `...t<FRAMENUMBER>.mp4`
- Output folder path

**Outputs:**
- `lmt_binary_search_<date>.sqlite` — table `GAP_FILL_ANALYSIS`; all rows from 1.3.lmt_qc_sampler.py input preserved, `IN_NEST` updated for resolved frames, `BINARY_SEARCH` column (0/1) added
- `LMT_Summary_<date>.txt` — pipeline summary report covering detection stats, gap stats, binary search outcomes, and per-gap timing

**Core Logic:**
- Gaps shorter than `MIN_GAP_DURATION_FOR_BINARY_SEARCH_IN_SECONDS` (default 30s) are skipped; frames remain `IN_NEST = −1`
- Remaining gaps are presented as binary search tasks: the reviewer sees the boundary detected frames plus the current midpoint; answers IN NEST or OUT OF NEST
- IN answer fills the left half to 1 and recurses on the right; OUT answer recurses on the left half only; segments shorter than `FILL_ENTIRE_SEGMENT_IF_DURATION_LESS_THAN_IN_MINUTES` (default 1 min) are filled entirely without recursing
- Frames not explicitly answered default to `IN_NEST = 0` via `decisions.get(fn, 0)` fallback in `_finish()`

**Do NOT modify:**
- `DB_FPS = 30` and `FRAME_CONVERSION = 2` — these encode the relationship between the LMT database frame rate (30 fps) and the video frame rate (15 fps); wrong values silently extract the wrong frames
- Video filename parsing in `get_start_frame()` — assumes the pattern `...t<int>.<ext>`; any other naming convention will break video-to-frame mapping
- Table name `GAP_FILL_ANALYSIS` in both the read query and the output write
- `BINARY_SEARCH = 1` is set only for frames in searchable gaps (above threshold); this distinction is used by 4.lmt_qc_validator.py

**Open-source notes:**
- The default-zero fallback for unanswered frames (`decisions.get(fn, 0)`) means quitting mid-session silently classifies all unreviewed frames as OUT OF NEST — this is intentional but must be understood by users
- The summary report reconciles the starting `IN_NEST = −1` population into three addends: IN (explicit), OUT (all, including defaulted), and skipped — the OUT total includes both explicitly decided and defaulted frames; see the sub-breakdown in the report for the split
- Undo/redo state is held entirely in memory; closing the window mid-session without completing all gaps loses unprocessed tasks

---

### Script: `3.lmt_qc_sampler.py`

**Purpose:**
- Randomly samples frames from the `2.lmt_binary_search.py` output and extracts corresponding video screenshots for manual QC.
- Supports two independent sampling pools: detected frames and assumed frames.

**Inputs:**
- `lmt_binary_search_<date>.sqlite` (table: `GAP_FILL_ANALYSIS`)
- LMT video files (.mp4) — same naming convention as required by 2.lmt_binary_search.py
- Output folder path
- Animal ID (integer)
- Sample count (integer, applied independently to each selected QC type)
- QC type selection: one or both of `"LMT Detected QC"` / `"Assumed Rows QC"`

**Outputs:**
- Per selected type: `lmt_qc_sampler_<slug>_<timestamp>.sqlite` — table `QC_ASSUMED_SAMPLES`
- Per selected type: `Screenshots_<slug>_<timestamp>/` folder containing extracted PNG frames
- Columns in output table: `sample_id`, `animal_id`, `video`, `frame_global`, `IN_NEST`, `ASSUMPTION_TYPE`, `GAP_START_FRAME`, `GAP_END_FRAME`, `BINARY_SEARCH`, `screenshot`, `QC_TYPE`

**Core Logic:**
- Loads full `GAP_FILL_ANALYSIS` table and filters to the appropriate pool per QC type (detected: `ASSUMPTION_TYPE = "DETECTED"`; assumed: `ASSUMPTION_TYPE = "ASSUMED"` and `IN_NEST in (0, 1)`)
- Draws a random sample without a fixed seed; sample is sorted by frame number before screenshot extraction
- Each video frame is located by matching the global frame number against video start/end ranges derived from filenames; local frame index computed as `(global_frame − video_start) / FRAME_CONVERSION`
- Both types share a single timestamp when run together; each type gets its own screenshot folder and SQLite

**Do NOT modify:**
- `FRAME_CONVERSION = 2` — must match the value in 2.lmt_binary_search.py
- Video filename frame-number parsing — must match 2.lmt_binary_search.py convention
- `QC_TYPE` column written to output — `4.lmt_qc_validator.py` reads this to select the correct filter; changing the string values breaks the handoff
- Table name `QC_ASSUMED_SAMPLES` — hardcoded in 4.lmt_qc_validator.py's read query
- `IN_NEST = −1` rows are excluded from the assumed pool — these are unresolved frames with no valid algorithm classification to validate against

**Open-source notes:**
- No fixed random seed, i.e, each run produces a different sample; reproducibility requires the user to manage this externally if needed
- If both QC types are selected and one fails (e.g. no detected rows in the database), the other still completes; partial results are reported
- `BINARY_SEARCH` column defaults to 0 if absent, this provides backward compatibility with pre-update `2.lmt_binary_search.py` outputs, but such outputs also lack detected rows, so the detected QC pool will be empty

---

### Script: `4.lmt_qc_validator.py`

**Purpose:**
- Presents sampled frames from `3.lmt_qc_sampler.py` output for manual labelling and computes a confusion matrix comparing algorithm classifications against human labels.

**Inputs:**
- `lmt_qc_sampler_<slug>_<timestamp>.sqlite` (table: `QC_ASSUMED_SAMPLES`)
- Screenshot folder produced by the corresponding `3.lmt_qc_sampler.py` run

**Outputs:**
- `lmt_qc_validator_<date>.sqlite` — same table with `MANUAL_QC` column populated (1 = IN NEST, 0 = OUT OF NEST)
- `lmt_qc_validator_<date>.txt` — validation report with confusion matrix (TP, FP, TN, FN) and accuracy, error rate, sensitivity, specificity

**Core Logic:**
- Detects QC type from the `QC_TYPE` column and applies the matching row filter: detected runs filter to `ASSUMPTION_TYPE = "DETECTED"`; assumed runs filter to `ASSUMPTION_TYPE = "ASSUMED"` and `IN_NEST in (0, 1)`; legacy files without `QC_TYPE` fall back to assumed-only with a warning
- Displays one screenshot at a time with algorithm prediction and current manual label; keyboard shortcuts A (IN) / D (OUT) / ← (previous) / → (next)
- Saves to SQLite on every label action — progress is preserved if the session is interrupted
- Confusion matrix uses `IN_NEST = 1` as the positive class and `IN_NEST = 0` as the negative class; computed only over labelled rows

**Do NOT modify:**
- `QC_TYPE` string constants — must match the values written by 3.lmt_qc_sampler.py (`"Assumed Rows QC"`, `"LMT Detected QC"`)
- `MANUAL_QC` sentinel values (1, 0, None/NaN) — None/NaN is the "not yet labelled" state used to distinguish unlabelled from labelled-as-OUT
- `IN_NEST = −1` exclusion — these rows have no valid algorithm classification and must never appear in the confusion matrix
- Table name `QC_ASSUMED_SAMPLES` — hardcoded in the read query

**Open-source notes:**
- The screenshot path is reconstructed as `os.path.join(screenshot_folder, row["screenshot"])`; the screenshot folder must be the exact folder produced by the corresponding 3.lmt_qc_sampler.py run — mismatching SQLite and folder (e.g. from different runs) will cause missing-file errors
- Saving on every label action means the output SQLite is written frequently; running from a network drive or slow storage may cause noticeable lag
- Legacy `3.lmt_qc_sampler.py` outputs (no `QC_TYPE` column) are supported with a fallback warning but will only work correctly if the file contains assumed rows; feeding a legacy detected-only file produces an empty session
