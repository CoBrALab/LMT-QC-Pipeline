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
- Reads `GAP_FILL_ANALYSIS` from the `1.lmt_gap_fill.py` SQLite; splits rows into `DETECTED` and `ASSUMED` subsets
- Within `ASSUMED` frames, those with `IN_NEST = 1` were already logic-filled by `1.lmt_gap_fill.py` and pass through untouched; only frames with `IN_NEST = -1` enter the binary search workflow
- Each gap of `IN_NEST = -1` frames is classified by its boundary state (the last detected frame before the gap and the first detected frame after): type 00 (OUT→OUT), 01 (OUT→IN), 10 (IN→OUT), or 11 (IN→IN)
- Type 00 and Type 11 gaps are skipped entirely; their frames remain `IN_NEST = -1` in the output. Type 11 gaps should not appear (`1.lmt_gap_fill.py` should have logic-filled them); a non-zero count is flagged in the report as unexpected
- Gaps of type 01 or 10 shorter than `MIN_GAP_DURATION_FOR_BINARY_SEARCH` are also skipped; frames remain `IN_NEST = -1`
- Remaining 01/10 gaps are presented to the reviewer as binary search tasks. Each task shows three panels: the last detected frame before the gap (left), the current midpoint frame (centre), the first detected frame after the gap (right)
- Type 10 logic: IN answer fills the left half to 1 and recurses on the right half; OUT answer recurses on the left half only. 
- Type 01 logic: Is mirrored. IN fills the right half and recurses left; OUT recurses right
- Segments shorter than `FILL_ENTIRE_SEGMENT_IF_DURATION_LESS_THAN_IN_MINUTES` are filled entirely without recursing
- Frames that entered binary search but were never explicitly answered default to `IN_NEST = 0` via decisions.get(fn, 0) in _finish()
- On completion, `ASSUMED` rows are merged with all original `DETECTED` rows and written together to `GAP_FILL_ANALYSIS`. `DETECTED` rows receive `FILL_SOURCE = "DETECTED"` and `BINARY_SEARCH = 0`. `ASSUMED` rows receive `FILL_SOURCE` of `"BINARY_SEARCH"`, `"LOGIC"`, or `"UNKNOWN"` depending on how they were resolved
- A plain-text summary report is written with integrity checks; any frame-count mismatch raises an error and the report is not written 


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
          - `FILL_SOURCE` ("DETECTED"/"LOGIC"/BINARY_SEARCH"/"UNKNOWN")
- `LMT_Summary_<date>.txt` 

**Do NOT modify:**
- `DB_FPS = 30` and `FRAME_CONVERSION = 2`: these encode the relationship between the LMT database frame rate (30 fps) and the video frame rate (15 fps), wrong values would extract the wrong frames
- Video filename parsing in `get_start_frame()` assumes the pattern `...t<int>.<ext>`, any other naming convention will break video-to-frame mapping
- Table name `GAP_FILL_ANALYSIS` in both the read query and the output write
- `BINARY_SEARCH = 1` is set only for frames in searchable gaps (above threshold), this distinction is used by 4.lmt_qc_validator.py
- `FILL_SOURCE` values "DETECTED", "LOGIC", "BINARY_SEARCH", "UNKNOWN": `3.lmt_qc_sampler.py`'s filter_pool() depends on these exact strings
- The merged output must contain both DETECTED and ASSUMED rows; `3.lmt_qc_sampler.py`'s DETECTED pool requires DETECTED rows to be present


**Open-source notes:**
- The default-zero fallback for unanswered frames (`decisions.get(fn, 0)`) means closing mid-session silently classifies all unreviewed frames as OUT OF NEST; this is intentional but the summary report's "binary-search OUT" total includes both explicitly decided and defaulted frames (the report does not split these further)
- Type 11 gaps with `IN_NEST = -1` frames indicate a `1.lmt_gap_fill.py` logic error; `2.lmt_binary_search.py` skips them (does not binary-search them) and reports their count as unexpected in the Processing Breakdown section
- The summary report integrity checks raise `IntegrityError` and abort report writing if any frame-count identity fails; the SQLite is always saved before the report is attempted, so data is not lost on a report failure
- Undo/redo state is held entirely in memory; closing the window mid-session without completing all gaps loses all unprocessed tasks and their implicit OUT=0 defaults will apply


---

### Script: `3.lmt_qc_sampler.py`

**Core Logic:**
- Reads `GAP_FILL_ANALYSIS` from `lmt_binary_search_<date>.sqlite`
- User selects one or more QC pools via checkboxes (`DETECTED`, `BINARY_SEARCH`, `LOGIC`); at least one must be selected
- For each selected pool, sampling and extraction run independently; the user-entered sample count applies per pool
- Pool filtering logic:
    - `DETECTED`: `ASSUMPTION_TYPE == "DETECTED"` (no IN_NEST restriction)
    - `BINARY_SEARCH`: `ASSUMPTION_TYPE == "ASSUMED"` AND `FILL_SOURCE == "BINARY_SEARCH"` AND `IN_NEST in (0, 1)`
    - `LOGIC`: `ASSUMPTION_TYPE == "ASSUMED"` AND `FILL_SOURCE == "LOGIC"` AND `IN_NEST in (0, 1)`
- `IN_NEST = -1` frames (UNKNOWN) are excluded from all pools
- For each pool: `n_samples` rows are drawn randomly without replacement, sorted by `FRAMENUMBER`, and a screenshot is extracted for each
- Each pool writes its own output subfolder `<POOL>_<date>/Screenshots/` and its own SQLite

**Inputs:**
- `lmt_binary_search_<date>.sqlite` 
- LMT Output video files (.mp4) 
- Output folder path
- Animal ID (integer)
- Sample count (integer, applied independently to each selected QC type)
- QC type selection: one or more of `DETECTED`, `BINARY_SEARCH`, `LOGIC`

**Outputs:**
- Per selected type:
    - `lmt_qc_sampler_<qc_mode>_<timestamp>.sqlite`
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
                - `screenshot`
                - `QC_MODE`
      - `<POOL>_<date>/Screenshots/<screenshot_files>.png` (folder containing extracted PNG frames)

**Do NOT modify:**
- `QC_MODE` string values (`DETECTED`, `BINARY_SEARCH`, `LOGIC`): `4.lmt_qc_validator.py` reads these from the first row to determine its filter branch
- `FILL_SOURCE` must be written to `QC_ASSUMED_SAMPLES`; `4.lmt_qc_validator.py`'s primary filter path depends on it
- Pool output folder naming pattern <POOL>_<date>; each pool must have its own folder and SQLite — no mixed-pool files

**Open-source notes:**
- No fixed random seed, i.e, each run produces a different sample; reproducibility requires the user to manage this externally if needed
- If fewer frames are available in a pool than requested, an error is raised before any extraction begins; partial sampling is not performed
- Screenshots are named S<counter>_A<animal_id>_G<global_frame>_<video_basename>.png; counter resets to 1 for each pool run independently
  
---

### Script: `4.lmt_qc_validator.py`

**Purpose:**
- Loads QC_ASSUMED_SAMPLES from `lmt_qc_sampler_<qc_mode>_<timestamp>.sqlite`; reads QC_MODE from the first row to determine which filter and display context to apply
- Filters eligible rows by pool:
    - `DETECTED`: `ASSUMPTION_TYPE == "DETECTED"`
    - `BINARY_SEARCH`: `ASSUMPTION_TYPE == "ASSUMED"` AND `FILL_SOURCE == "BINARY_SEARCH"` AND `IN_NEST in (0, 1)`
    - `LOGIC`: `ASSUMPTION_TYPE == "ASSUMED"` AND `FILL_SOURCE == "LOGIC"` AND `IN_NEST in (0, 1)`
- Presents each eligible frame's screenshot one at a time; user labels it IN NEST (A) or OUT OF NEST (D); labels are saved to MANUAL_QC column after each answer
- Database is saved after every label (no data loss on unexpected close)
- On reaching the last sample, computes a two-class confusion matrix: algorithm IN_NEST is the prediction; human MANUAL_QC is the ground truth.
- Writes a validation report (.txt) containing the confusion matrix, four performance metrics, and the screenshot filenames of all FP and FN cases
- Navigation: Previous (←) and Next (→) allow free movement; labelling with A or D does not auto-advance

**Inputs:**
- `lmt_qc_sampler_<qc_mode>_<timestamp>.sqlite` 
- `Screenshots_<qc_mode>_<timestamp>`

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
            - `screenshot`
            - `QC_MODE`
            - `MANUAL_QC` (0 (Out of nest) / 1 (In nest) / NaN (Not yet labelled))
- `lmt_qc_validator_<date>.txt` 

