# -*- coding: utf-8 -*-
"""
База данных лицензий Neway RP — SQLite (файл license_data.db).

Таблицы:
  licenses — одна строка на каждую выданную лицензию
  notes    — одна строка-заметка на каждого пользователя

Нумерация ID:
  Тип 1 (Участник РП)  → RPP-0000001, RPP-0000002, …
  Тип 2 (Менеджер РП)  → RPM-0000001, RPM-0000002, …

Счётчик независимый для каждого типа — у RPP и RPM своя сквозная нумерация.
"""

import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path("license_data.db")

# Блокировка для безопасных записей из async-окружения
_lock = threading.Lock()


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def init_db() -> None:
    """
    Создаёт таблицы при первом запуске. Вызывать один раз при старте бота.
    Повторные вызовы безопасны (IF NOT EXISTS).
    """
    with _lock, _connect() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS licenses (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                license_id    TEXT    NOT NULL UNIQUE,
                license_type  INTEGER NOT NULL CHECK(license_type IN (1, 2)),
                discord_id    INTEGER NOT NULL,
                username      TEXT    NOT NULL,
                channel_id    INTEGER NOT NULL,
                issued_by     INTEGER NOT NULL,
                issued_at     TEXT    NOT NULL,
                revoked       INTEGER NOT NULL DEFAULT 0 CHECK(revoked IN (0, 1)),
                revoked_by    INTEGER,
                revoked_at    TEXT
            );

            CREATE TABLE IF NOT EXISTS notes (
                discord_id  INTEGER PRIMARY KEY,
                note_text   TEXT    NOT NULL DEFAULT '',
                updated_by  INTEGER,
                updated_at  TEXT
            );
        """)


# ────────────────────────────────────────────────────────────
#  ВНУТРЕННИЕ УТИЛИТЫ
# ────────────────────────────────────────────────────────────

def _next_license_id(conn: sqlite3.Connection, license_type: int) -> str:
    """
    Генерирует следующий уникальный ID лицензии для данного типа.
    Счёт ведётся по всем записям этого типа (включая отозванные),
    чтобы ID никогда не повторялись.
    """
    prefix = "RPP" if license_type == 1 else "RPM"
    row = conn.execute(
        "SELECT COUNT(*) AS cnt FROM licenses WHERE license_type = ?",
        (license_type,),
    ).fetchone()
    next_num = (row["cnt"] or 0) + 1
    return f"{prefix}-{next_num:07d}"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ────────────────────────────────────────────────────────────
#  ВЫДАЧА ЛИЦЕНЗИИ
# ────────────────────────────────────────────────────────────

def issue_license(
    license_type: int,
    discord_id: int,
    username: str,
    channel_id: int,
    issued_by: int,
) -> str:
    """
    Записывает новую лицензию в БД.
    Возвращает сгенерированный license_id (например, «RPP-0000001»).
    """
    with _lock, _connect() as conn:
        lic_id = _next_license_id(conn, license_type)
        conn.execute(
            """INSERT INTO licenses
               (license_id, license_type, discord_id, username, channel_id, issued_by, issued_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (lic_id, license_type, discord_id, username, channel_id, issued_by, _now_iso()),
        )
        return lic_id


# ────────────────────────────────────────────────────────────
#  ОТЗЫВ ЛИЦЕНЗИЙ
# ────────────────────────────────────────────────────────────

def revoke_licenses(
    discord_id: int,
    license_type: int,
    revoked_by: int,
) -> list[dict]:
    """
    Отзывает ВСЕ активные лицензии указанного типа у пользователя.

    Возвращает список словарей с данными отозванных записей — бот
    использует их, чтобы знать, с каких каналов снять права.
    Если активных лицензий нет — возвращает пустой список.
    """
    now = _now_iso()
    with _lock, _connect() as conn:
        rows = conn.execute(
            """SELECT * FROM licenses
               WHERE discord_id = ? AND license_type = ? AND revoked = 0""",
            (discord_id, license_type),
        ).fetchall()

        if not rows:
            return []

        ids = [r["id"] for r in rows]
        placeholders = ",".join("?" * len(ids))
        conn.execute(
            f"""UPDATE licenses
                SET revoked = 1, revoked_by = ?, revoked_at = ?
                WHERE id IN ({placeholders})""",
            [revoked_by, now, *ids],
        )
        return [dict(r) for r in rows]


# ────────────────────────────────────────────────────────────
#  ПОИСК
# ────────────────────────────────────────────────────────────

def search_by_license_id(license_id: str) -> list[dict]:
    """Точный поиск по ID лицензии (регистр не важен)."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM licenses WHERE license_id = ?",
            (license_id.upper(),),
        ).fetchall()
        return [dict(r) for r in rows]


def search_by_discord_id(discord_id: int) -> list[dict]:
    """Все лицензии пользователя по его Discord ID, сначала новые."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM licenses WHERE discord_id = ? ORDER BY issued_at DESC",
            (discord_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def search_by_username(username: str) -> list[dict]:
    """
    Частичный поиск по юзернейму (регистронезависимо).
    Ищет по имени, которое было актуальным на момент выдачи лицензии.
    """
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM licenses WHERE LOWER(username) LIKE ? ORDER BY issued_at DESC",
            (f"%{username.lower()}%",),
        ).fetchall()
        return [dict(r) for r in rows]


def get_active_licenses(discord_id: int, license_type: int) -> list[dict]:
    """Только активные (не отозванные) лицензии конкретного типа."""
    with _connect() as conn:
        rows = conn.execute(
            """SELECT * FROM licenses
               WHERE discord_id = ? AND license_type = ? AND revoked = 0""",
            (discord_id, license_type),
        ).fetchall()
        return [dict(r) for r in rows]


# ────────────────────────────────────────────────────────────
#  ЗАМЕТКИ
# ────────────────────────────────────────────────────────────

def get_note(discord_id: int) -> str:
    """Возвращает текст заметки. Пустая строка — если заметки нет."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT note_text FROM notes WHERE discord_id = ?",
            (discord_id,),
        ).fetchone()
        return row["note_text"] if row else ""


def set_note(discord_id: int, note_text: str, updated_by: int) -> None:
    """Создаёт или полностью перезаписывает заметку для пользователя."""
    with _lock, _connect() as conn:
        conn.execute(
            """INSERT INTO notes (discord_id, note_text, updated_by, updated_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(discord_id) DO UPDATE SET
                   note_text  = excluded.note_text,
                   updated_by = excluded.updated_by,
                   updated_at = excluded.updated_at""",
            (discord_id, note_text, updated_by, _now_iso()),
        )
