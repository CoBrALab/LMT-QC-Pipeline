import os
import sqlite3
from tkinter import *
from tkinter import filedialog, messagebox

# Database Processing
def split_database_by_frame(input_db, output_db, start_frame, end_frame):

    conn_in = sqlite3.connect(input_db)
    conn_out = sqlite3.connect(output_db)

    c_in = conn_in.cursor()
    c_out = conn_out.cursor()

    # Get all non-system tables
    c_in.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%';")

    tables = [row[0] for row in c_in.fetchall()]

    total_rows_copied = 0

    for table in tables:

        print(f"Processing table: {table}")

        # Recreate table structure
        c_in.execute(f"SELECT sql FROM sqlite_master WHERE type='table' AND name='{table}';")

        create_stmt = c_in.fetchone()[0]
        c_out.execute(create_stmt)

        # Get columns
        c_in.execute(f"PRAGMA table_info({table});")
        columns = [col[1] for col in c_in.fetchall()]

        # Apply frame filter if FRAMENUMBER exists
        if "FRAMENUMBER" in columns:
            rows = c_in.execute(f"SELECT * FROM {table} WHERE FRAMENUMBER BETWEEN ? AND ?",(start_frame, end_frame))
        else:
            rows = c_in.execute(f"SELECT * FROM {table}")

        placeholders = ",".join(["?"] * len(columns))

        insert_query = (f"INSERT INTO {table} VALUES ({placeholders})")

        table_count = 0

        for row in rows:
            c_out.execute(insert_query, row)
            table_count += 1

        conn_out.commit()

        total_rows_copied += table_count

        print(f"  Copied {table_count:,} rows")

    conn_in.close()
    conn_out.close()

    return total_rows_copied

# Global Variables
input_db_path = ""
output_folder = ""

# GUI Functions
def select_input_db():
    global input_db_path
    input_db_path = filedialog.askopenfilename(title="Select Input SQLite Database", filetypes=[("SQLite Database", "*.sqlite *.db")])
    if input_db_path:
        input_label.config(text=input_db_path)

def select_output_folder():
    global output_folder
    output_folder = filedialog.askdirectory(title="Select Output Folder")
    if output_folder:
        output_label.config(text=output_folder)

def run_split():
    if not input_db_path:
        messagebox.showerror("Error", "Please select an input SQLite database.")
        return
    
    if not output_folder:
        messagebox.showerror("Error", "Please select an output folder.")
        return
    
    try:
        start_frame = int(start_frame_entry.get())
        end_frame = int(end_frame_entry.get())
    except ValueError:
        messagebox.showerror("Error", "Start Frame and End Frame must be integers.")
        return
    
    if start_frame > end_frame:
        messagebox.showerror("Error", "Start Frame must be less than or equal to End Frame.")
        return

    output_db = os.path.join(output_folder, f"lmt_frame_subset_{start_frame}_{end_frame}.sqlite")

    try:
        rows_copied = split_database_by_frame(input_db_path, output_db, start_frame, end_frame)
        messagebox.showinfo("Success", f"Subset database created successfully.\n\nOutput File:\n{output_db}\n\nRows Copied: {rows_copied:,}")
    except Exception as e:
        messagebox.showerror("Error", str(e))

# GUI
root = Tk()
root.title("LMT SQLite Frame Range Extractor")
root.geometry("800x400")

Button(root, text="Select Input SQLite", command=select_input_db, width=30).pack(pady=10)
input_label = Label(root, text="No database selected", wraplength=750)
input_label.pack()

Button(root, text="Select Output Folder", command=select_output_folder, width=30).pack(pady=10)
output_label = Label(root, text="No output folder selected", wraplength=750)
output_label.pack()

Label(root, text="Start Frame", font=("Arial", 12)).pack(pady=(20, 5))
start_frame_entry = Entry(root, width=20)
start_frame_entry.pack()

Label(root, text="End Frame", font=("Arial", 12)).pack(pady=(15, 5))
end_frame_entry = Entry(root, width=20)
end_frame_entry.pack()

Button(root, text="CREATE SUBSET DATABASE", command=run_split, bg="green", fg="white", width=30, height=2).pack(pady=30)

root.mainloop()
