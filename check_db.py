import sqlite3

conn = sqlite3.connect('katina.db')
conn.row_factory = sqlite3.Row

print("--- Sessions ---")
for row in conn.execute("SELECT * FROM sessions"):
    print(dict(row))

print("--- Orders ---")
for row in conn.execute("SELECT * FROM orders"):
    print(dict(row))