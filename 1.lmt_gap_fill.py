import os
import sqlite3
import pandas as pd
from datetime import datetime
from tkinter import *
from tkinter import filedialog, messagebox

def seconds_to_hms(seconds): # hms means hh:mm:ss format
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"

# Main analysis
def run_analysis(input_db, output_folder, animal_id, nest_xmin, nest_xmax, nest_ymin, nest_ymax, buffer_xmin, buffer_xmax, buffer_ymin, buffer_ymax):
    conn = sqlite3.connect(input_db)
    df = pd.read_sql_query(
        f"""
        SELECT
            FRAMENUMBER,
            MASS_X,
            MASS_Y
        FROM DETECTION
        WHERE ANIMALID = {animal_id}
        ORDER BY FRAMENUMBER
        """,
        conn
    )
    conn.close()

    if len(df) == 0:
        raise Exception(f"No DETECTION rows found for Animal ID {animal_id}")

    def in_roi(x, y, roi):
        return (roi["xmin"] < x < roi["xmax"] and roi["ymin"] < y < roi["ymax"])

    NEST        = {"xmin": nest_xmin,   "xmax": nest_xmax,   "ymin": nest_ymin,   "ymax": nest_ymax}
    NEST_BUFFER = {"xmin": buffer_xmin, "xmax": buffer_xmax, "ymin": buffer_ymin, "ymax": buffer_ymax}

    rows = []
    assumed_rows  = 0
    detected_rows = 0
    detected_in_nest_frames = 0

    for i in range(len(df) - 1):
        current  = df.iloc[i]
        next_row = df.iloc[i + 1]

        f1 = int(current["FRAMENUMBER"])
        f2 = int(next_row["FRAMENUMBER"])

        x1, y1 = current["MASS_X"],  current["MASS_Y"]
        x2, y2 = next_row["MASS_X"], next_row["MASS_Y"]

        in_nest_start = in_roi(x1, y1, NEST)
        in_buffer_end = in_roi(x2, y2, NEST_BUFFER)

        rows.append({
            "FRAMENUMBER":     f1,
            "IN_NEST":         int(in_nest_start),
            "ASSUMPTION_TYPE": "DETECTED",
            "GAP_START_FRAME": None,
            "GAP_END_FRAME":   None,
        })

        detected_rows += 1
        if in_nest_start:
            detected_in_nest_frames += 1

        gap = f2 - f1
        if gap > 1:
            in_nest_value  = 1 if (in_nest_start and in_buffer_end) else -1

            for frame in range(f1 + 1, f2):
                rows.append({
                    "FRAMENUMBER":     frame,
                    "IN_NEST":         in_nest_value,
                    "ASSUMPTION_TYPE": "ASSUMED",
                    "GAP_START_FRAME": f1, # the value that we mention for GAP_START_FRAME is the last detected frame BEFORE gap begins
                    "GAP_END_FRAME":   f2, # the value that we mention for GAP_END_FRAME is the first detected frame AFTER gap ends
                })
                assumed_rows += 1

    last = df.iloc[-1]
    last_in_nest = in_roi(last["MASS_X"], last["MASS_Y"], NEST)
    rows.append({
        "FRAMENUMBER":     int(last["FRAMENUMBER"]),
        "IN_NEST":         int(last_in_nest),
        "ASSUMPTION_TYPE": "DETECTED",
        "GAP_START_FRAME": None,
        "GAP_END_FRAME":   None,
    })
    detected_rows += 1
    if last_in_nest:
        detected_in_nest_frames += 1

    output_df = pd.DataFrame(rows)

    current_date_time = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    sqlite_name = f"lmt_gap_fill_{current_date_time}.sqlite"

    output_sqlite = os.path.join(output_folder, sqlite_name)

    conn = sqlite3.connect(output_sqlite)
    output_df.to_sql("GAP_FILL_ANALYSIS", conn, if_exists="replace", index=False)
    conn.close()

    detected_not_in_nest_frames = detected_rows - detected_in_nest_frames

    messagebox.showinfo(
        "Analysis Complete",
        f"Gap Fill Analysis Complete\n\n"
        f"Detected Frames:     {detected_rows:,}\n"
        f"  - IN NEST:         {detected_in_nest_frames:,}\n"
        f"  - NOT IN NEST:     {detected_not_in_nest_frames:,}\n\n"
        f"Assumed Frames:      {assumed_rows:,}\n"
        f"Total Frames:        {len(output_df):,}\n\n"
        f"SQLite Output:\n{output_sqlite}\n\n"
    )


# GUI
input_db_path      = ""
output_folder_path = ""

def select_database():
    global input_db_path
    input_db_path = filedialog.askopenfilename(filetypes=[("SQLite Database", "*.sqlite *.db")])
    label_db.config(text=input_db_path)

def select_output_folder():
    global output_folder_path
    output_folder_path = filedialog.askdirectory()
    label_output.config(text=output_folder_path)

def start():
    try:
        if not input_db_path:
            raise Exception("Please select an LMT SQLite database")
        if not output_folder_path:
            raise Exception("Please select an output folder")
        run_analysis(
            input_db=input_db_path,
            output_folder=output_folder_path,
            animal_id=int(entry_animal.get()),
            nest_xmin=float(entry_nest_xmin.get()),
            nest_xmax=float(entry_nest_xmax.get()),
            nest_ymin=float(entry_nest_ymin.get()),
            nest_ymax=float(entry_nest_ymax.get()),
            buffer_xmin=float(entry_buffer_xmin.get()),
            buffer_xmax=float(entry_buffer_xmax.get()),
            buffer_ymin=float(entry_buffer_ymin.get()),
            buffer_ymax=float(entry_buffer_ymax.get()),
        )
    except Exception as e:
        messagebox.showerror("Error", str(e))

root = Tk()
root.title("LMT Gap Fill Assumption Generator")
root.geometry("800x850")

Label(root, text="LMT Gap Fill Assumption Generator", font=("Arial", 16, "bold")).pack(pady=10)

Button(root, text="Select LMT SQLite Database", command=select_database).pack()
label_db = Label(root, text="No database selected", wraplength=700)
label_db.pack(pady=5)

Label(root, text="Animal ID").pack()
entry_animal = Entry(root); entry_animal.insert(0, "1"); entry_animal.pack()

Label(root, text="Nest X Minimum").pack()
entry_nest_xmin = Entry(root); entry_nest_xmin.insert(0, "100"); entry_nest_xmin.pack()

Label(root, text="Nest X Maximum").pack()
entry_nest_xmax = Entry(root); entry_nest_xmax.insert(0, "250"); entry_nest_xmax.pack()

Label(root, text="Nest Y Minimum").pack()
entry_nest_ymin = Entry(root); entry_nest_ymin.insert(0, "50"); entry_nest_ymin.pack()

Label(root, text="Nest Y Maximum").pack()
entry_nest_ymax = Entry(root); entry_nest_ymax.insert(0, "200"); entry_nest_ymax.pack()

Label(root, text="Buffer X Minimum").pack()
entry_buffer_xmin = Entry(root); entry_buffer_xmin.insert(0, "80"); entry_buffer_xmin.pack()

Label(root, text="Buffer X Maximum").pack()
entry_buffer_xmax = Entry(root); entry_buffer_xmax.insert(0, "270"); entry_buffer_xmax.pack()

Label(root, text="Buffer Y Minimum").pack()
entry_buffer_ymin = Entry(root); entry_buffer_ymin.insert(0, "30"); entry_buffer_ymin.pack()

Label(root, text="Buffer Y Maximum").pack()
entry_buffer_ymax = Entry(root); entry_buffer_ymax.insert(0, "220"); entry_buffer_ymax.pack()

Button(root, text="Select Output Folder", command=select_output_folder).pack(pady=10)
label_output = Label(root, text="No output folder selected", wraplength=700)
label_output.pack()

Button(root, text="RUN ANALYSIS", command=start, bg="green", fg="white", width=25, height=2).pack(pady=20)

root.mainloop()
