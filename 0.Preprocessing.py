"""
Script0 - Deduplicate Detection Rows

Purpose: Creates a copy of an LMT Output SQLite database and removes
duplicate/conflicting rows from the DETECTION table, based on FRAMENUMBER
+ ANIMALID identity rather than on any coordinate value.

The original SQLite is NOT modified.

Previously this script deleted every row where FRONT_X = -1, on the
assumption that a missing FRONT_*/BACK_* coordinate meant a row was
unusable. As part of git issue #26, QC testing across several real SQLite databases showed that
assumption does not hold: rows with FRONT_X/Y/Z = -1 and BACK_X/Y/Z = -1
can still carry accurate, usable MASS_X/MASS_Y coordinates (the pipeline's
actual position signal). This script no longer deletes or even inspects
those columns.

What it does instead is deduplicate DETECTION on (FRAMENUMBER, ANIMALID),
the identity every downstream script assumes is unique for a single
animal's timeline:

  Case A - completely identical rows (identical across every column
  except a surrogate/auto-increment primary key, if the table has one):
  the first occurrence (by original row order) is kept, and every later
  identical row is deleted.

  Case B - rows that share the same (FRAMENUMBER, ANIMALID) but disagree
  on some other column (e.g. two different MASS_X/MASS_Y readings for
  the same frame and animal): this is ambiguous, conflicting data with no
  principled way to pick a "correct" row, so every row in that group is
  deleted, including the first one.

This exists because 1.lmt_gap_fill.py's gap-detection logic assumes
FRAMENUMBER strictly increases for a given animal; a duplicate or
out-of-order FRAMENUMBER silently corrupts its gap sizing and downstream
in-nest time estimates (see Issue #5). Running this deduplication first
guarantees that invariant holds by the time 1.lmt_gap_fill.py runs.

This is a pure command-line script (no GUI): every input that used to be
collected via a Tkinter dialog is now a CLI argument/flag, and every
confirmation that used to be a messagebox prompt is now a flag that must
be passed explicitly to opt in (the default, unattended behavior is the
same "no" a user would give at an interactive prompt: don't overwrite).
"""

import argparse
import os
import shutil
import sqlite3
import sys
import time

import pandas as pd


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Create a deduplicated copy of an LMT Output SQLite's "
                    "DETECTION table (dedup keyed on FRAMENUMBER + ANIMALID)."
    )
    parser.add_argument(
        "-i", "--input", required=True,
        help="Path to the LMT Output SQLite to clean.",
    )
    parser.add_argument(
        "-o", "--output-folder", required=True,
        help="Directory to write the cleaned copy into.",
    )
    parser.add_argument(
        "--overwrite", action="store_true",
        help="Overwrite the output file if it already exists. Without this "
             "flag, the script aborts rather than overwriting a previous run.",
    )
    return parser.parse_args(argv)


# Processing
def process_database(input_db, output_folder, overwrite=False):
    """
    Returns 0 on success (including the no-op "nothing to deduplicate"
    case), 1 on any failure or user-facing abort condition.
    """

    basename = os.path.basename(input_db)
    name, ext = os.path.splitext(basename)

    output_db = os.path.join(output_folder, f"{name}_processed{ext}")

    # Guard against silently overwriting a previous run's output.
    if os.path.exists(output_db):
        if not overwrite:
            print(
                f"ABORTED: output file already exists:\n{output_db}\n"
                f"Pass --overwrite to overwrite it.",
                file=sys.stderr,
            )
            return 1
        print(f"Output file already exists, overwriting (--overwrite given):\n{output_db}")

    conn = None

    try:
        print("=" * 60)
        print("Copying SQLite...")
        print("=" * 60)

        start = time.time()

        shutil.copy2(input_db, output_db)

        print(f"Copy complete.")
        print(f"Saved to:\n{output_db}")

        copy_time = time.time() - start

        print(f"Copy time: {copy_time:.1f} seconds\n")

        print("=" * 60)
        print("Opening copied SQLite...")
        print("=" * 60)

        conn = sqlite3.connect(output_db)

        cur = conn.cursor()

        # Verify the DETECTION table actually exists before querying it.
        cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='DETECTION'"
        )
        if cur.fetchone() is None:
            print(
                f"ERROR: The selected SQLite does not contain a DETECTION table.\n\n"
                f"File: {output_db}\n\n"
                f"This does not look like a valid LMT Output SQLite.",
                file=sys.stderr,
            )
            conn.close()
            conn = None
            return 1

        cur.execute("PRAGMA table_info(DETECTION)")
        table_info  = cur.fetchall()   # (cid, name, type, notnull, dflt_value, pk)
        all_columns = [row[1] for row in table_info]
        # A surrogate/auto-increment primary key (if the table has one) is
        # an identity artifact, not data -- two rows with identical data
        # but different auto-assigned IDs are still the "completely
        # identical" duplicates Case A is about, so it's excluded from the
        # identity comparison below rather than making every row look
        # artificially unique.
        pk_columns  = {row[1] for row in table_info if row[5] > 0}

        for required in ("FRAMENUMBER", "ANIMALID"):
            if required not in all_columns:
                print(
                    f"ERROR: DETECTION is missing the required '{required}' "
                    f"column.\n\nFile: {output_db}\n\n"
                    f"This does not look like a valid LMT Output SQLite.",
                    file=sys.stderr,
                )
                conn.close()
                conn = None
                return 1

        dedup_columns = [c for c in all_columns if c not in pk_columns]

        print("Loading DETECTION rows...")

        col_list = ", ".join(f'"{c}"' for c in all_columns)
        df = pd.read_sql_query(
            f'SELECT rowid AS "_rowid", {col_list} FROM DETECTION ORDER BY rowid',
            conn,
        )

        total_rows = len(df)
        print(f"Total rows: {total_rows:,}")

        if total_rows == 0:
            print("DETECTION table is empty. Nothing to deduplicate.")
            conn.close()
            conn = None
            return 0

        print("\nChecking for missing FRAMENUMBER/ANIMALID...")

        # Rows missing FRAMENUMBER and/or ANIMALID have no identity to
        # deduplicate on. Grouping them together anyway (e.g. via
        # groupby(..., dropna=False)) would treat "missing" as if it were
        # itself a valid, shared identity value, silently lumping together
        # rows that are otherwise completely unrelated -- multiple genuinely
        # different detections that merely all happen to be missing the
        # same field would then look like one big "conflicting" group and
        # get mass-deleted under Case B, which is not what "same
        # FRAMENUMBER + ANIMALID" is supposed to mean. These rows are left
        # untouched instead: this script only ever deletes rows it can
        # positively identify as exact duplicates or as conflicting with
        # another row for the same identity, and a row with no identity at
        # all can be neither.
        missing_identity_mask  = df["FRAMENUMBER"].isna() | df["ANIMALID"].isna()
        missing_identity_count = int(missing_identity_mask.sum())

        if missing_identity_count > 0:
            print(
                f"NOTE: {missing_identity_count:,} row(s) are missing "
                f"FRAMENUMBER and/or ANIMALID. These cannot be deduplicated "
                f"by identity and are left unchanged."
            )

        dedup_df = df[~missing_identity_mask]

        print("\nScanning for duplicate FRAMENUMBER + ANIMALID groups...")

        case_a_delete_rowids = []
        case_b_delete_rowids = []
        case_a_groups = 0
        case_b_groups = 0
        case_b_examples = []

        for (frame_number, animal_id), group in dedup_df.groupby(
            ["FRAMENUMBER", "ANIMALID"], sort=False
        ):
            if len(group) == 1:
                continue

            # Case A vs Case B hinges on whether every row in this
            # (FRAMENUMBER, ANIMALID) group is identical across every
            # dedup column (drop_duplicates treats NaN == NaN as equal,
            # which is the right behavior for identity comparison here --
            # note FRAMENUMBER/ANIMALID themselves can no longer be NaN at
            # this point, but another dedup column still could be).
            distinct = group[dedup_columns].drop_duplicates()

            if len(distinct) == 1:
                # Case A: completely identical rows. Keep the first
                # occurrence by original row order (lowest rowid, which
                # reflects the source file's original insertion order
                # since shutil.copy2 preserves the file byte-for-byte),
                # delete the rest.
                case_a_groups += 1
                sorted_group = group.sort_values("_rowid")
                case_a_delete_rowids.extend(sorted_group["_rowid"].iloc[1:].tolist())
            else:
                # Case B: same FRAMENUMBER + ANIMALID, but conflicting
                # data in some other column. No principled way to pick a
                # "correct" row, so the whole group is discarded,
                # including what would have been the first occurrence.
                case_b_groups += 1
                case_b_delete_rowids.extend(group["_rowid"].tolist())
                if len(case_b_examples) < 10:
                    case_b_examples.append(
                        (int(frame_number), int(animal_id), len(group))
                    )

        case_a_removed = len(case_a_delete_rowids)
        case_b_removed = len(case_b_delete_rowids)
        total_removed  = case_a_removed + case_b_removed

        print(
            f"Case A (exact duplicates): {case_a_groups:,} group(s), "
            f"{case_a_removed:,} row(s) removed (first occurrence kept in each)."
        )
        print(
            f"Case B (same FRAMENUMBER+ANIMALID, conflicting data): "
            f"{case_b_groups:,} group(s), {case_b_removed:,} row(s) removed "
            f"(entire group discarded, no row kept)."
        )
        if case_b_examples:
            print("Case B example group(s) (FRAMENUMBER, ANIMALID, rows in group):")
            for frame_number, animal_id, group_size in case_b_examples:
                print(f"  FRAMENUMBER={frame_number}, ANIMALID={animal_id}, {group_size} conflicting rows")
            if case_b_groups > len(case_b_examples):
                print(f"  ... and {case_b_groups - len(case_b_examples):,} more group(s)")

        if total_removed == 0:
            print("\nNo duplicate or conflicting rows found.")
            conn.close()
            conn = None
            return 0

        print(f"\nDeleting {total_removed:,} row(s)...")

        delete_start = time.time()

        all_delete_rowids = case_a_delete_rowids + case_b_delete_rowids

        # A temp table + subquery delete, rather than a Python DELETE ...
        # WHERE rowid IN (...) with all rowids inlined, avoids SQLite's
        # per-statement bound-parameter limit on very large duplicate sets.
        cur.execute("CREATE TEMP TABLE _dedup_delete_rowids (rowid_val INTEGER PRIMARY KEY)")
        cur.executemany(
            "INSERT INTO _dedup_delete_rowids (rowid_val) VALUES (?)",
            [(int(r),) for r in all_delete_rowids],
        )
        cur.execute("DELETE FROM DETECTION WHERE rowid IN (SELECT rowid_val FROM _dedup_delete_rowids)")
        cur.execute("DROP TABLE _dedup_delete_rowids")

        conn.commit()

        delete_time = time.time() - delete_start

        print("Deletion complete.")

        print("\nRunning VACUUM...")
        print("(This may take several minutes for large databases.)")

        vacuum_start = time.time()

        # Use VACUUM INTO rather than an in-place VACUUM. In-place VACUUM
        # builds its rebuild scratch file in SQLite's default temp location
        # (typically the system temp directory, e.g. /var/tmp), which can be
        # on a much smaller partition than output_folder and can fail with
        # "database or disk is full" on large databases even when the output
        # location has plenty of space. VACUUM INTO writes the compacted
        # copy directly alongside output_db, then we atomically swap it in.
        # The DELETE above has already committed, so output_db remains a
        # valid (if uncompacted) result if this step fails.
        vacuum_tmp = output_db + ".vacuum"
        if os.path.exists(vacuum_tmp):
            os.remove(vacuum_tmp)

        try:
            cur.execute("VACUUM INTO ?", (vacuum_tmp,))
            conn.close()
            conn = None
            os.replace(vacuum_tmp, output_db)
        except Exception as vacuum_error:
            if os.path.exists(vacuum_tmp):
                os.remove(vacuum_tmp)
            raise Exception(
                f"VACUUM failed, but the deduplicated output was already "
                f"saved successfully (uncompacted) at:\n{output_db}\n\n"
                f"Original error: {vacuum_error}"
            ) from vacuum_error

        vacuum_time = time.time() - vacuum_start

        total = time.time() - start

        print("\n" + "=" * 60)
        print("Finished Successfully")
        print("=" * 60)

        print(f"Rows removed total                        : {total_removed:,}")
        print(f"  Case A (exact duplicates)                : {case_a_removed:,}")
        print(f"  Case B (conflicting FRAMENUMBER+ANIMALID): {case_b_removed:,}")
        print(f"Rows remaining                             : {total_rows - total_removed:,}")
        if missing_identity_count > 0:
            print(f"  (of which, missing FRAMENUMBER/ANIMALID, left unchanged: {missing_identity_count:,})")
        print(f"Output SQLite: {output_db}")

        print(f"\nTiming")
        print(f"Copy    : {copy_time:.1f} sec")
        print(f"Delete  : {delete_time:.1f} sec")
        print(f"VACUUM  : {vacuum_time:.1f} sec")
        print(f"Total   : {total:.1f} sec")

        print("=" * 60)

        return 0

    except Exception as e:
        print(f"ERROR: Processing failed: {e}", file=sys.stderr)
        return 1

    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def main(argv=None):
    args = parse_args(argv)

    if not os.path.isfile(args.input):
        print(f"ERROR: Please provide a valid LMT Output SQLite. Not found: {args.input}", file=sys.stderr)
        return 1

    if not os.path.isdir(args.output_folder):
        print(f"ERROR: Please provide a valid output folder. Not found: {args.output_folder}", file=sys.stderr)
        return 1

    return process_database(args.input, args.output_folder, overwrite=args.overwrite)


if __name__ == "__main__":
    sys.exit(main())
