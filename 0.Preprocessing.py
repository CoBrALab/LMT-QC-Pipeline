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
        return

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

    cur.execute("VACUUM")

    conn.commit()

    vacuum_time = time.time() - vacuum_start

    conn.close()

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