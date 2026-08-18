"""
Script0 - Remove Invalid Detection Rows

Purpose: Creates a copy of an LMT Output SQLite database and removes rows from the DETECTION table where FRONT_X = -1
(and when FRONT_X = -1, it must also mean that
    FRONT_Y = -1
    FRONT_Z = -1
    BACK_X  = -1
    BACK_Y  = -1
    BACK_Z  = -1)
   
The original SQLite is NOT modified.

Before deleting, the script verifies that the "FRONT_X = -1 implies all other
position columns are -1" assumption actually holds for this file, and warns
the user if it does not. It also refuses to silently overwrite an existing
output file, and verifies the DETECTION table exists before doing any work.

This is a pure command-line script (no GUI): every input that used to be
collected via a Tkinter dialog is now a CLI argument/flag, and every
confirmation that used to be a messagebox prompt is now a flag that must be
passed explicitly to opt in (the default, unattended behavior is the same
"no" a user would give at an interactive prompt: don't overwrite, don't
proceed past a failed assumption check).
"""

import argparse
import os
import shutil
import sqlite3
import sys
import time


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Create a cleaned copy of an LMT Output SQLite by removing "
                    "DETECTION rows where FRONT_X = -1."
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
    parser.add_argument(
        "--force", action="store_true",
        help="Proceed with deletion even if some FRONT_X = -1 rows do not "
             "also have FRONT_Y/FRONT_Z/BACK_X/BACK_Y/BACK_Z = -1. Without "
             "this flag, the script aborts if that assumption doesn't hold.",
    )
    return parser.parse_args(argv)


# Processing
def process_database(input_db, output_folder, overwrite=False, force=False):
    """
    Returns 0 on success (including the no-op "nothing to delete" case),
    1 on any failure or user-facing abort condition.
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

        print("Counting rows to delete...")

        cur.execute("""
            SELECT COUNT(*)
            FROM DETECTION
            WHERE
                FRONT_X = -1
        """)

        rows_to_delete = cur.fetchone()[0]

        print(f"Rows matching filter: {rows_to_delete:,}")

        if rows_to_delete == 0:
            print("No rows need deleting.")
            conn.close()
            conn = None
            return 0

        # Validate the documented assumption: whenever FRONT_X = -1, the
        # other position columns should also be -1. Warn the user (with an
        # exact mismatch count) if that assumption does not hold for this
        # file, rather than silently deleting rows that may not actually be
        # fully invalid.
        print("\nValidating FRONT_X = -1 assumption against other columns...")

        cur.execute("""
            SELECT COUNT(*)
            FROM DETECTION
            WHERE
                FRONT_X = -1
                AND NOT (
                    FRONT_Y = -1 AND
                    FRONT_Z = -1 AND
                    BACK_X  = -1 AND
                    BACK_Y  = -1 AND
                    BACK_Z  = -1
                )
        """)

        mismatched_rows = cur.fetchone()[0]

        if mismatched_rows > 0:
            print(f"WARNING: {mismatched_rows:,} row(s) have FRONT_X = -1 but "
                  f"do NOT have all of FRONT_Y/FRONT_Z/BACK_X/BACK_Y/BACK_Z = -1.")

            if not force:
                print(
                    f"ABORTED: {mismatched_rows:,} row(s) have FRONT_X = -1 but do not have "
                    f"FRONT_Y, FRONT_Z, BACK_X, BACK_Y, and BACK_Z all equal to -1 "
                    f"as well.\n\n"
                    f"This script deletes rows based on FRONT_X = -1 only. "
                    f"Proceeding would delete these rows too, even though some of "
                    f"their other position columns are not -1.\n\n"
                    f"Pass --force to proceed anyway.",
                    file=sys.stderr,
                )
                conn.close()
                conn = None
                return 1
            print("Proceeding anyway (--force given).")
        else:
            print("Assumption verified: all FRONT_X = -1 rows also have "
                  "FRONT_Y/FRONT_Z/BACK_X/BACK_Y/BACK_Z = -1.")

        print("\nDeleting rows...")

        delete_start = time.time()

        cur.execute("""
            DELETE FROM DETECTION
            WHERE
                FRONT_X = -1
        """)

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
                f"VACUUM failed, but the deleted-row output was already "
                f"saved successfully (uncompacted) at:\n{output_db}\n\n"
                f"Original error: {vacuum_error}"
            ) from vacuum_error

        vacuum_time = time.time() - vacuum_start

        total = time.time() - start

        print("\n" + "=" * 60)
        print("Finished Successfully")
        print("=" * 60)

        print(f"Rows removed : {rows_to_delete:,}")
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

    return process_database(args.input, args.output_folder, overwrite=args.overwrite, force=args.force)


if __name__ == "__main__":
    sys.exit(main())
