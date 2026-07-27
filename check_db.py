import sqlite3

conn = sqlite3.connect("data/suppliers.db")
conn.row_factory = sqlite3.Row

rows = conn.execute(
    "SELECT id, supplier_id, reported_term, relationship, confidence, assessed_at "
    "FROM supplier_capabilities WHERE supplier_id = 4 AND canonical_term = 'iso 9001'"
).fetchall()

print(f"{len(rows)} row(s) found:")
for row in rows:
    print(dict(row))