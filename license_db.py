# -*- coding: utf-8 -*-
"""
База данных лицензий Neway RP — PostgreSQL.

Раньше здесь был локальный файл SQLite (license_data.db), но он лежал
внутри папки проекта и стирался при каждом обновлении бота на хостинге.
Теперь база — отдельный управляемый PostgreSQL-сервер, который живёт
независимо от кода бота и переживает любые редеплои/обновления.

Подключение настраивается ОДНОЙ переменной окружения:

    DATABASE_URL=postgresql://user:password@host:5432/dbname

Задайте её там же, где сейчас задан BOT_TOKEN (в панели хостинга, в
разделе переменных окружения, или в файле .env при локальном запуске).

Таблицы:
  licenses — одна строка на каждую выданную лицензию
  notes    — одна строка-заметка на каждого пользователя

Нумерация ID:
  Тип 1 (Участник РП)  → RPP-0000001, RPP-0000002, …
  Тип 2 (Менеджер РП)  → RPM-0000001, RPM-0000002, …

Счётчик независимый для каждого типа — у RPP и RPM своя сквозная нумерация.
"""

import contextlib
import os
import threading
from datetime import datetime, timezone

import psycopg2
import psycopg2.extras

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # на хостинге переменные задаются через панель — dotenv не обязателен

DATABASE_URL = os.getenv("DATABASE_URL")

# Блокировка для безопасных записей из async-окружения
_lock = threading.Lock()


@contextlib.contextmanager
def _connect():
    """
    Открывает соединение с PostgreSQL, отдаёт строки как словари
    (psycopg2.extras.RealDictRow ведёт себя как dict — row["field"] и
    dict(row) работают точно так же, как раньше со sqlite3.Row).
    Коммитит при успехе, откатывает при исключении, всегда закрывает
    соединение — это важно для PostgreSQL, у бесплатных тарифов обычно
    жёсткий лимит одновременных подключений.
    """
    if not DATABASE_URL:
        raise RuntimeError(
            "Не найдена переменная окружения DATABASE_URL! "
            "Укажите строку подключения к PostgreSQL в настройках хостинга "
            "(там же, где задаётся BOT_TOKEN)."
        )
    conn = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    """
    Создаёт таблицы при первом запуске. Вызывать один раз при старте бота.
    Повторные вызовы безопасны (IF NOT EXISTS).
    """
    with _lock, _connect() as conn:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS licenses (
                id            SERIAL  PRIMARY KEY,
                license_id    TEXT    NOT NULL UNIQUE,
                license_type  INTEGER NOT NULL CHECK(license_type IN (1, 2)),
                discord_id    BIGINT  NOT NULL,
                username      TEXT    NOT NULL,
                channel_id    BIGINT  NOT NULL,
                issued_by     BIGINT  NOT NULL,
                issued_at     TEXT    NOT NULL,
                revoked       INTEGER NOT NULL DEFAULT 0 CHECK(revoked IN (0, 1)),
                revoked_by    BIGINT,
                revoked_at    TEXT
            );
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS notes (
                discord_id  BIGINT PRIMARY KEY,
                note_text   TEXT   NOT NULL DEFAULT '',
                updated_by  BIGINT,
                updated_at  TEXT
            );
        """)


# ────────────────────────────────────────────────────────────
#  ВНУТРЕННИЕ УТИЛИТЫ
# ────────────────────────────────────────────────────────────

def _next_license_id(conn, license_type: int) -> str:
    """
    Генерирует следующий уникальный ID лицензии для данного типа.
    Счёт ведётся по всем записям этого типа (включая отозванные),
    чтобы ID никогда не повторялись.
    """
    prefix = "RPP" if license_type == 1 else "RPM"
    cur = conn.cursor()
    cur.execute(
        "SELECT COUNT(*) AS cnt FROM licenses WHERE license_type = %s",
        (license_type,),
    )
    row = cur.fetchone()
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
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO licenses
               (license_id, license_type, discord_id, username, channel_id, issued_by, issued_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s)""",
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
        cur = conn.cursor()
        cur.execute(
            """SELECT * FROM licenses
               WHERE discord_id = %s AND license_type = %s AND revoked = 0""",
            (discord_id, license_type),
        )
        rows = cur.fetchall()

        if not rows:
            return []

        ids = [r["id"] for r in rows]
        cur.execute(
            """UPDATE licenses
                SET revoked = 1, revoked_by = %s, revoked_at = %s
                WHERE id = ANY(%s)""",
            (revoked_by, now, ids),
        )
        return [dict(r) for r in rows]


# ────────────────────────────────────────────────────────────
#  ПОИСК
# ────────────────────────────────────────────────────────────

def search_by_license_id(license_id: str) -> list[dict]:
    """Точный поиск по ID лицензии (регистр не важен)."""
    with _connect() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM licenses WHERE license_id = %s",
            (license_id.upper(),),
        )
        return [dict(r) for r in cur.fetchall()]


def search_by_discord_id(discord_id: int) -> list[dict]:
    """Все лицензии пользователя по его Discord ID, сначала новые."""
    with _connect() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM licenses WHERE discord_id = %s ORDER BY issued_at DESC",
            (discord_id,),
        )
        return [dict(r) for r in cur.fetchall()]


def search_by_username(username: str) -> list[dict]:
    """
    Частичный поиск по юзернейму (регистронезависимо).
    Ищет по имени, которое было актуальным на момент выдачи лицензии.
    """
    with _connect() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM licenses WHERE LOWER(username) LIKE %s ORDER BY issued_at DESC",
            (f"%{username.lower()}%",),
        )
        return [dict(r) for r in cur.fetchall()]


def get_active_licenses(discord_id: int, license_type: int) -> list[dict]:
    """Только активные (не отозванные) лицензии конкретного типа."""
    with _connect() as conn:
        cur = conn.cursor()
        cur.execute(
            """SELECT * FROM licenses
               WHERE discord_id = %s AND license_type = %s AND revoked = 0""",
            (discord_id, license_type),
        )
        return [dict(r) for r in cur.fetchall()]


# ────────────────────────────────────────────────────────────
#  ЗАМЕТКИ
# ────────────────────────────────────────────────────────────

def get_note(discord_id: int) -> str:
    """Возвращает текст заметки. Пустая строка — если заметки нет."""
    with _connect() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT note_text FROM notes WHERE discord_id = %s",
            (discord_id,),
        )
        row = cur.fetchone()
        return row["note_text"] if row else ""


def set_note(discord_id: int, note_text: str, updated_by: int) -> None:
    """Создаёт или полностью перезаписывает заметку для пользователя."""
    with _lock, _connect() as conn:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO notes (discord_id, note_text, updated_by, updated_at)
               VALUES (%s, %s, %s, %s)
               ON CONFLICT(discord_id) DO UPDATE SET
                   note_text  = excluded.note_text,
                   updated_by = excluded.updated_by,
                   updated_at = excluded.updated_at""",
            (discord_id, note_text, updated_by, _now_iso()),
        )
