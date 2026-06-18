### Script: `1.lmt_gap_fill.py`

**Core Logic:**
- Reads raw LMT tracking detections and classifies each frame as IN NEST, OUT OF NEST, or unknown (-1) by comparing animal position against user-defined ROI coordinates.
- Gap frames assigned `IN_NEST = 1` if the frame before the gap is inside the nest and the frame after is inside the buffer; otherwise `IN_NEST = −1`

**Inputs:**
- LMT Output SQLite (table: `DETECTION`, columns: `FRAMENUMBER`, `MASS_X`, `MASS_Y`, `ANIMALID`)
- Animal ID (integer)
- Nest ROI: xmin, xmax, ymin, ymax (float)
- Buffer ROI: xmin, xmax, ymin, ymax (float) (must be larger than nest ROI)
- Output folder path

**Outputs:**
- `lmt_gap_fill_<date>.sqlite`
    - Table `GAP_FILL_ANALYSIS`
        - Columns
          - `FRAMENUMBER`
          - `IN_NEST` (-1 (Unknown) / 0 (Out of nest) / 1 (In nest))
          - `ASSUMPTION_TYPE` ("DETECTED"/"ASSUMED")
          - `GAP_START_FRAME`(frame before gap)
          - `GAP_END_FRAME` (frame after gap)

**Do NOT modify:**
- ROI test uses strict inequality (`<`, not `<=`) — changing to `<=` alters boundary behaviour
- `IN_NEST = −1` must remain −1; downstream scripts (`2.lmt_binary_search.py`, `4.lmt_qc_validator.py`) filter on this exact value
- Table name `GAP_FILL_ANALYSIS` is hardcoded in 2.lmt_binary_search.py's read query

**Open-source notes:**
- ROI coordinates are pixel-space values specific to each experimental arena; defaults in the GUI (200–350, 50–175) are placeholders and will be wrong for any other setup
- Buffer ROI must fully contain the nest ROI or gap-fill logic produces incorrect results (this is not validated in code)
- No input validation on ROI coordinate ordering (xmin < xmax, ymin < ymax)

---

### Script: `2.lmt_binary_search.py`

**Core Logic:**
- Gaps shorter than `MIN_GAP_DURATION_FOR_BINARY_SEARCH_IN_SECONDS` (default 30s) are skipped; frames remain `IN_NEST = −1`
- Remaining gaps are presented as binary search tasks: the reviewer sees the boundary detected frames plus the current midpoint; answers IN NEST or OUT OF NEST
- IN answer fills the left half to 1 and recurses on the right; OUT answer recurses on the left half only; segments shorter than `FILL_ENTIRE_SEGMENT_IF_DURATION_LESS_THAN_IN_MINUTES` (default 1 min) are      filled entirely without recursing
- Frames not explicitly answered default to `IN_NEST = 0` via `decisions.get(fn, 0)` fallback in `_finish()`
- Outputs a complete frame classification table preserving all rows from the `1.lmt_gap_fill.py` output, with a `BINARY_SEARCH` column added.

**Inputs:**
- `lmt_gap_fill_<date>.sqlite`
- LMT Output video files (.mp4) 
- Output folder path

**Outputs:**
- `lmt_binary_search_<date>.sqlite` 
    - Table `GAP_FILL_ANALYSIS`
        - Columns
          - `FRAMENUMBER`
          - `IN_NEST` (-1 (Unknown) / 0 (Out of nest) / 1 (In nest))
          - `ASSUMPTION_TYPE` ("DETECTED"/"ASSUMED")
          - `GAP_START_FRAME` (frame before gap)
          - `GAP_END_FRAME` (frame after gap)
          - `BINARY_SEARCH`(0 (Binary search was not performed) / 1 (Binary search was performed))
- `LMT_Summary_<date>.txt` 

**Do NOT modify:**
- `DB_FPS = 30` and `FRAME_CONVERSION = 2`: these encode the relationship between the LMT database frame rate (30 fps) and the video frame rate (15 fps), wrong values would extract the wrong frames
- Video filename parsing in `get_start_frame()` assumes the pattern `...t<int>.<ext>`, any other naming convention will break video-to-frame mapping
- Table name `GAP_FILL_ANALYSIS` in both the read query and the output write
- `BINARY_SEARCH = 1` is set only for frames in searchable gaps (above threshold), this distinction is used by 4.lmt_qc_validator.py

**Open-source notes:**
- The default-zero fallback for unanswered frames (`decisions.get(fn, 0)`) means quitting mid-session silently classifies all unreviewed frames as OUT OF NEST, this is intentional but must be understood by users
- The summary report reconciles the starting `IN_NEST = −1` population into three addends: IN (explicit), OUT (all, including defaulted), and skipped — the OUT total includes both explicitly decided and       defaulted frames; see the sub-breakdown in the report for the split
- Undo/redo state is held entirely in memory; closing the window mid-session without completing all gaps loses unprocessed tasks

---

### Script: `3.lmt_qc_sampler.py`

**Core Logic:**
- Randomly samples frames from the `2.lmt_binary_search.py` output and extracts corresponding video screenshots for manual QC.
- Supports two independent sampling pools: detected frames and assumed frames.

**Inputs:**
- `lmt_binary_search_<date>.sqlite` 
- LMT Output video files (.mp4) 
- Output folder path
- Animal ID (integer)
- Sample count (integer, applied independently to each selected QC type)
- QC type selection: one or both of `"LMT Detected QC"` / `"Assumed Rows QC"`

**Outputs:**
- Per selected type:
    - `lmt_qc_sampler_<slug>_<timestamp>.sqlite`
        - Table `QC_ASSUMED_SAMPLES`
            - Columns
                - `sample_id`
                - `animal_id`
                - `video`
                - `frame_global`
                - `IN_NEST`
                - `ASSUMPTION_TYPE`
                - `GAP_START_FRAME`
                - `GAP_END_FRAME`
                - `BINARY_SEARCH`
                - `screenshot`
                - `QC_TYPE`
      - `Screenshots_<slug>_<timestamp>` (folder containing extracted PNG frames)

**Do NOT modify:**
- `FRAME_CONVERSION = 2` must match the value in `2.lmt_binary_search.py`
- Video filename frame-number parsing must match `2.lmt_binary_search.py` convention
- `QC_TYPE` column written to output and `4.lmt_qc_validator.py` reads this column to select the correct filter; changing the string values breaks the handoff
- Table name `QC_ASSUMED_SAMPLES` is hardcoded in `4.lmt_qc_validator.py`'s read query
- `IN_NEST = −1` rows are excluded from the assumed pool 

**Open-source notes:**
- No fixed random seed, i.e, each run produces a different sample; reproducibility requires the user to manage this externally if needed
- If both QC types are selected and one fails (e.g. no detected rows in the database), the other still completes; partial results are reported
  
---

### Script: `4.lmt_qc_validator.py`

**Purpose:**
- Presents sampled frames from `3.lmt_qc_sampler.py` output for manual labelling and computes a confusion matrix comparing algorithm classifications against human labels.

**Inputs:**
- `lmt_qc_sampler_<slug>_<timestamp>.sqlite` 
- `Screenshots_<slug>_<timestamp>`

**Outputs:**
- `lmt_qc_validator_<date>.sqlite`
    - Table `QC_ASSUMED_SAMPLES`
        - Columns
            - `sample_id`
            - `animal_id`
            - `video`
            - `frame_global`
            - `IN_NEST`
            - `ASSUMPTION_TYPE`
            - `GAP_START_FRAME`
            - `GAP_END_FRAME`
            - `BINARY_SEARCH`
            - `screenshot`
            - `QC_TYPE`
            - `MANUAL_QC` (0 (Out of nest) / 1 (In nest) / NaN (Not yet labelled))
- `lmt_qc_validator_<date>.txt` 

**Do NOT modify:**
- `QC_TYPE` string constants must match the values written by `3.lmt_qc_sampler.py` (`"Assumed Rows QC"`, `"LMT Detected QC"`)
- Table name `QC_ASSUMED_SAMPLES` is hardcoded in the read query
