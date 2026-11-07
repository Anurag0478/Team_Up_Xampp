"""MySQL / MariaDB connection helpers (XAMPP).

Settings come from environment variables or from a local `.env` file:
  DB_HOST (127.0.0.1)  DB_PORT (3306)  DB_USER (root)  DB_PASSWORD (empty)  DB_NAME (teamup)
These are XAMPP's defaults, so with a stock XAMPP install nothing needs to be configured.
"""
import os
import pymysql
from pymysql.cursors import DictCursor


def load_env():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(path): return
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_env()

HOST = os.environ.get("DB_HOST", "127.0.0.1")
PORT = int(os.environ.get("DB_PORT", 3306))
USER = os.environ.get("DB_USER", "root")
PASSWORD = os.environ.get("DB_PASSWORD", "")
NAME = os.environ.get("DB_NAME", "teamup")


class Conn:
    """Thin wrapper so code can call conn.execute(sql, args) and get a cursor back.
    Autocommit is ON; transactions are opened explicitly with START TRANSACTION."""

    def __init__(self, database=NAME):
        self.raw = pymysql.connect(host=HOST, port=PORT, user=USER, password=PASSWORD, database=database,
                                   charset="utf8mb4", cursorclass=DictCursor, autocommit=True)

    def execute(self, sql, args=None):
        cur = self.raw.cursor()
        cur.execute(sql, args)
        return cur

    def executemany(self, sql, seq):
        cur = self.raw.cursor()
        cur.executemany(sql, list(seq))
        return cur

    def close(self):
        try: self.raw.close()
        except Exception: pass


def connect(database=NAME):
    return Conn(database)


def run_script(conn, text):
    """Run an .sql file that may use `DELIMITER $$` (needed for triggers).
    CREATE DATABASE / USE lines are skipped: the database is chosen by DB_NAME."""
    delim, buf = ";", []
    for line in text.splitlines():
        s = line.strip()
        if s.upper().startswith("DELIMITER "):
            delim = s.split(None, 1)[1]; continue
        if not s or s.startswith("--"): continue
        buf.append(line)
        if s.endswith(delim):
            stmt = "\n".join(buf).rstrip()[: -len(delim)].strip(); buf = []
            if stmt and not stmt.upper().startswith(("CREATE DATABASE", "USE ")):
                conn.execute(stmt)
