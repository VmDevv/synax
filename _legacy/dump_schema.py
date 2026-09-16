import sqlite3

def dump_schema():
    conn = sqlite3.connect('audit_rules.db')
    cursor = conn.cursor()
    cursor.execute("SELECT sql FROM sqlite_master WHERE type='table'")
    tables = cursor.fetchall()
    for table in tables:
        if table[0]:
            print(table[0])
            print(";")
    conn.close()

if __name__ == "__main__":
    dump_schema()
