"""SQLite storage. Everything per-user lives here and never goes through git."""
from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY, phone TEXT UNIQUE NOT NULL, bark TEXT NOT NULL,
  is_admin INTEGER NOT NULL DEFAULT 0, settings TEXT NOT NULL DEFAULT '{}', created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS invites(
  code TEXT PRIMARY KEY, kind TEXT NOT NULL DEFAULT 'invite', phone TEXT,
  created_at REAL NOT NULL, used_at REAL, used_by INTEGER);
CREATE TABLE IF NOT EXISTS otps(
  phone TEXT PRIMARY KEY, code_hash TEXT NOT NULL, expires_at REAL NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0, pending TEXT);
CREATE TABLE IF NOT EXISTS otp_sends(phone TEXT NOT NULL, at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS sessions(token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL, expires_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS products(
  id INTEGER PRIMARY KEY AUTOINCREMENT,  -- never reuse an id: state and history are keyed by it
  user_id INTEGER NOT NULL, cfg TEXT NOT NULL, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS user_state(user_id INTEGER PRIMARY KEY, state TEXT NOT NULL DEFAULT '{}', latest TEXT NOT NULL DEFAULT '{}');
CREATE TABLE IF NOT EXISTS http_cache(url TEXT PRIMARY KEY, body TEXT, error TEXT, fetched_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def db_path() -> str:
    return os.environ.get("DEALWEB_DB") or str(HERE.parent / "dealweb.db")


def connect(path: str | None = None, *, init: bool = False) -> sqlite3.Connection:
    con = sqlite3.connect(path or db_path(), timeout=30)
    con.row_factory = sqlite3.Row
    if init:
        con.execute("PRAGMA journal_mode=WAL")
        con.executescript(SCHEMA)
    return con


def load_sources() -> list[dict]:
    return json.loads((HERE / "sources.json").read_text("utf-8"))


def user_products(con, user_id: int) -> list[dict]:
    rows = con.execute("SELECT id, cfg FROM products WHERE user_id=? ORDER BY id", (user_id,)).fetchall()
    return [{**json.loads(r["cfg"]), "id": f"p{r['id']}"} for r in rows]


def get_meta(con, key: str, default=None):
    row = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return json.loads(row["value"]) if row else default


def set_meta(con, key: str, value) -> None:
    con.execute("INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)", (key, json.dumps(value, ensure_ascii=False)))


def try_lock(con, name: str, ttl: float = 1500) -> bool:
    """Cross-process lock (web process vs. timer worker). Stale locks expire after ttl seconds."""
    now = time.time()
    with con:
        con.execute("BEGIN IMMEDIATE")
        held = get_meta(con, f"lock:{name}")
        if held and now - held < ttl:
            return False
        set_meta(con, f"lock:{name}", now)
    return True


def unlock(con, name: str) -> None:
    with con:
        con.execute("DELETE FROM meta WHERE key=?", (f"lock:{name}",))
