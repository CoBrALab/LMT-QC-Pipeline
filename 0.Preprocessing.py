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
"""

import os
import shutil
import sqlite3
import time
import tkinter as tk
from tkinter import filedialog, messagebox

# GUI

def browse_input():
    filename = filedialog.askopenfilename(title="Select LMT Output SQLite", filetypes=[("SQLite Database", "*.sqlite *.db"), ("All Files", "*.*")])
    if filename:
        input_var.set(filename)

def browse_output():
    folder = filedialog.askdirectory(title="Select Output Folder")
    if folder:
        output_var.set(folder)

def start_processing():
    input_db = input_var.get().strip()
    output_folder = output_var.get().strip()

    if not os.path.isfile(input_db):
        messagebox.showerror("Error", "Please select a valid LMT Output SQLite.")
        return

    if not os.path.isdir(output_folder):
        messagebox.showerror("Error", "Please select a valid Output Folder.")
        return

    root.destroy()
    process_database(input_db, output_folder)

 
# Processing
def process_database(input_db, output_folder):

    basename = os.path.basename(input_db)
    name, ext = os.path.splitext(basename)

    output_db = os.path.join(output_folder, f"{name}_processed{ext}")

    # Guard against silently overwriting a previous run's output.
    if os.path.exists(output_db):
        overwrite = messagebox.askyesno(
            "Output Already Exists",
            f"The output file already exists:\n{output_db}\n\n"
            f"Do you want to overwrite it?"
        )
        if not overwrite:
            print("Aborted: output file already exists and user chose not to overwrite.")
            return

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
            messagebox.showerror(
                "Error",
                f"The selected SQLite does not contain a DETECTION table.\n\n"
                f"File: {output_db}\n\n"
                f"This does not look like a valid LMT Output SQLite."
            )
            conn.close()
            conn = None
            return

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
            return

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

            proceed = messagebox.askyesno(
                "Assumption Check Failed",
                f"{mismatched_rows:,} row(s) have FRONT_X = -1 but do not have "
                f"FRONT_Y, FRONT_Z, BACK_X, BACK_Y, and BACK_Z all equal to -1 "
                f"as well.\n\n"
                f"This script deletes rows based on FRONT_X = -1 only. "
                f"Proceeding will delete these rows too, even though some of "
                f"their other position columns are not -1.\n\n"
                f"Do you want to proceed anyway?"
            )
            if not proceed:
                print("Aborted by user after assumption-check failure.")
                conn.close()
                conn = None
                return
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

    except Exception as e:
        messagebox.showerror("Error", f"Processing failed:\n\n{e}")
        print(f"ERROR: Processing failed: {e}")

    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


# Main GUI
root = tk.Tk()
root.title("Script0 - SQLite Cleanup")

input_var = tk.StringVar()
output_var = tk.StringVar()

frame = tk.Frame(root, padx=15, pady=15)
frame.pack()

# Input SQLite
tk.Label(frame, text="LMT Output SQLite:").grid(row=0, column=0, sticky="w")
tk.Entry(frame, width=60, textvariable=input_var).grid(row=1, column=0, padx=(0, 10))
tk.Button(frame, text="Browse...", command=browse_input).grid(row=1, column=1)

# Output folder
tk.Label(frame, text="Output Folder:").grid(row=2, column=0, sticky="w", pady=(15, 0))
tk.Entry(frame, width=60, textvariable=output_var).grid(row=3, column=0, padx=(0, 10))
tk.Button(frame, text="Browse...", command=browse_output).grid(row=3, column=1)

# Start
tk.Button(frame, text="Start Processing", command=start_processing, width=25).grid(row=4, column=0, columnspan=2, pady=20)

root.mainloop()