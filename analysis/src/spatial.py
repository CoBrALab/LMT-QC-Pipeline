"""
analysis/src/spatial.py

Position-based metrics: inter-animal proximity (Metric 7) and locomotor
activity / distance traveled (Metric 8). Both read raw MASS_X/MASS_Y from
the processed DETECTION table via analysis.src.io.load_positions(), NOT
from GAP_FILL_ANALYSIS -- position never survives into that table (see
analysis/src/io.py's module docstring).
"""

import numpy as np
import pandas as pd

from .io import DB_FPS


def pairwise_distance(positions: pd.DataFrame, id_a: int, id_b: int) -> pd.Series:
    """
    What it does
    ------------
    Metric 7. Frame-by-frame Euclidean distance between two animals'
    centroids.

    Why it exists
    -------------
    A general social-proximity proxy (huddling, following, avoidance)
    that is not restricted to nest-adjacent behavior the way the
    occupancy metrics are -- it can be computed anywhere in the arena.

    Inputs
    ------
    positions : output of analysis.src.io.load_positions() for (at
        least) id_a and id_b.
    id_a, id_b : ANIMALID values present as X_<id>/Y_<id> columns in
        positions.

    Outputs
    -------
    pandas.Series indexed by FRAMENUMBER, NaN wherever either animal
    lacks a detection at that frame (no gap-fill mechanism applies here --
    a missing position simply means no distance can be computed at that
    frame; there is no principled way to "assume" a position the way the
    nest-ROI logic assumes in-nest status from boundary frames).

    Logic
    -----
    Direct Euclidean distance: sqrt((xa-xb)^2 + (ya-yb)^2), vectorized
    over the full series.

    Assumptions
    -----------
    MASS_X/MASS_Y are in a consistent spatial unit for both animals
    (true within one session/arena). No calibration to real-world units
    (cm, etc.) is established anywhere in this repository -- confirm what
    unit your LMT export actually uses before interpreting absolute
    distance values or choosing a contact_threshold for
    proximity_summary() below.

    Failure modes
    -------------
    LMT identity swaps (two RFID-tagged animals' detections briefly
    mis-assigned to each other, most likely exactly during close contact,
    when the tracker has to disambiguate overlapping blobs) can produce a
    spuriously small OR spuriously large distance depending on swap
    direction. This is a documented LMT failure mode, not something this
    function can detect on its own -- see Validation.

    Validation
    ----------
    Manually spot-check a handful of low-distance frames against the
    actual video to confirm they correspond to genuine close contact and
    not an identity-swap artifact.

    Integration
    -----------
    Feeds proximity_summary() below.
    """
    xa, ya = positions[f"X_{id_a}"], positions[f"Y_{id_a}"]
    xb, yb = positions[f"X_{id_b}"], positions[f"Y_{id_b}"]
    return np.sqrt((xa - xb) ** 2 + (ya - yb) ** 2)


def proximity_summary(distance: pd.Series, contact_threshold: float) -> dict:
    """
    What it does
    ------------
    Summarizes a distance series into mean/median distance and time spent
    at or below a contact threshold.

    Why it exists
    -------------
    Raw per-frame distance is not itself a reportable summary statistic;
    this converts it into session-level numbers comparable across
    animals/conditions.

    Inputs
    ------
    distance : output of pairwise_distance().
    contact_threshold : maximum center-to-center distance (same spatial
        unit as MASS_X/MASS_Y) counted as "in contact." This must be set
        from a biologically meaningful basis (e.g. approximately one
        adult mouse body length) and validated against real video -- it
        is a required argument specifically so it can never be silently
        defaulted to an unvalidated guess.

    Outputs
    -------
    dict: n_valid_frames, mean_distance, median_distance,
    seconds_in_contact, fraction_valid_time_in_contact.
    {"n_valid_frames": 0} if the input series has no valid (non-NaN)
    frames at all.

    Logic
    -----
    NaN frames (either animal undetected) are dropped before computing
    any statistic -- see Assumptions for why this may bias the result.

    Assumptions
    -----------
    Centroid distance ignores body size/orientation: two animals nose-to-
    nose vs. one on top of the other (e.g. a nursing posture) can produce
    similar centroid distances despite being behaviorally very different
    configurations. This metric cannot distinguish them -- that
    distinction requires the side-view camera / manual scoring (later
    tasks).

    Failure modes
    -------------
    An inappropriately small contact_threshold (smaller than typical
    inter-centroid distance even during real contact) will silently
    undercount all real contact as non-contact.

    Validation
    ----------
    Compare seconds_in_contact against a manual video estimate for a
    short representative segment before trusting it at session scale.

    Integration
    -----------
    Report alongside fraction_valid_time_in_contact, which functions as
    this metric's own analog of occupancy's "unresolved fraction" --
    always show how much of the session this statistic is actually based
    on, not just the statistic itself. Also flag, in any write-up: if
    detection dropout is state-dependent (harder to resolve two animals
    individually when they ARE close together -- a plausible tracking-
    error mechanism), the valid/visible frames are a biased sample that
    systematically under-represents true close contact, so
    fraction_valid_time_in_contact should not be treated as an unbiased
    contact estimate.
    """
    valid = distance.dropna()
    if len(valid) == 0:
        return {"n_valid_frames": 0}
    return {
        "n_valid_frames": int(len(valid)),
        "mean_distance": float(valid.mean()),
        "median_distance": float(valid.median()),
        "seconds_in_contact": float((valid <= contact_threshold).sum() / DB_FPS),
        "fraction_valid_time_in_contact": float((valid <= contact_threshold).mean()),
    }


def locomotor_distance(positions: pd.DataFrame, animal_id: int,
                        max_frame_gap: int = 1) -> dict:
    """
    What it does
    ------------
    Metric 8. Total path length traveled by one animal.

    Why it exists
    -------------
    A general activity-level covariate: useful mainly to confirm a
    treatment effect on nest time is not simply a treatment effect on
    overall mobility (e.g. sedation, illness). Not a maternal-care metric
    on its own -- ranked lowest of the metrics in this module for that reason.

    Inputs
    ------
    positions : output of load_positions() for (at least) animal_id.
    max_frame_gap : consecutive-frame displacement is only summed where
        the two frames are truly adjacent (FRAMENUMBER difference <= this
        value, default 1). A displacement computed across a longer
        detection gap would include however far the animal actually
        moved during the WHOLE gap collapsed into one straight-line
        segment, wildly underestimating true path length (a mouse rarely
        moves in a straight line) -- this is a deliberate
        exclude-don't-guess choice, consistent with this pipeline's own
        established handling of nest-ROI gaps.

    Outputs
    -------
    dict: total_distance, n_valid_segments, n_excluded_gap_segments,
    fraction_path_observed (n_valid_segments / total adjacent-frame
    pairs -- an explicit honesty check on how much of the true path this
    estimate is actually built from).

    Logic
    -----
    Consecutive-frame Euclidean displacement, summed only over segments
    where the frame-number gap is within max_frame_gap.

    Assumptions
    -----------
    Straight-line displacement between two truly adjacent frames (1/15s
    apart at video frame rate, 1/30s at DB frame rate) is a reasonable
    approximation of actual path length over that short an interval, even
    though it is not exact for any interval > 0.

    Failure modes
    -------------
    fraction_path_observed will typically be substantially LOWER than the
    frame-resolved fraction underlying the occupancy metrics, because
    nothing in this pipeline reconstructs a path across a detection gap
    the way gap-filling reconstructs nest STATE across a gap -- there is
    no principled way to. A low fraction_path_observed means the reported
    distance is a substantial underestimate of true path length, not a
    small one.

    Validation
    ----------
    Always report fraction_path_observed alongside total_distance; do not
    interpret total_distance in isolation.

    Integration
    -----------
    Independent per-animal statistic; not combined with any other
    function in this package.
    """
    x = positions[f"X_{animal_id}"]
    y = positions[f"Y_{animal_id}"]
    frames = positions.index.to_numpy()

    valid = x.notna() & y.notna()
    x, y, frames = x[valid], y[valid], frames[valid]

    if len(frames) < 2:
        return {
            "total_distance": 0.0,
            "n_valid_segments": 0,
            "n_excluded_gap_segments": 0,
            "fraction_path_observed": np.nan,
        }

    frame_diff = np.diff(frames)
    dx = np.diff(x.to_numpy())
    dy = np.diff(y.to_numpy())
    seg_dist = np.sqrt(dx ** 2 + dy ** 2)

    is_adjacent = frame_diff <= max_frame_gap
    total_distance = float(seg_dist[is_adjacent].sum())

    return {
        "total_distance": total_distance,
        "n_valid_segments": int(is_adjacent.sum()),
        "n_excluded_gap_segments": int((~is_adjacent).sum()),
        "fraction_path_observed": float(is_adjacent.sum() / len(is_adjacent)),
    }
