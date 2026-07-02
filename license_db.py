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
from datetime import datetime, timedelta, timezone

import psycopg2
import psycopg2.errors
import psycopg2.extras

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # на хостинге переменные задаются через панель — dotenv не обязателен

DATABASE_URL = os.getenv("DATABASE_URL")

# Блокировка для безопасных записей из async-окружения
_lock = threading.Lock()


class ChannelAlreadyLicensedError(Exception):
    """
    На канал уже выдана активная (не отозванная) лицензия — второй
    активной лицензии на тот же канал одновременно быть не может.

    Атрибут `existing` содержит словарь с данными уже действующей
    лицензии (тот же формат, что возвращают функции поиска), чтобы
    вызывающий код мог показать, кому и когда она была выдана.
    """

    def __init__(self, existing: dict):
        self.existing = existing
        super().__init__(
            f"Channel {existing.get('channel_id')} already has an active "
            f"license ({existing.get('license_id')})"
        )


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
        # Частичный уникальный индекс: у одного канала не может быть
        # больше одной АКТИВНОЙ (revoked = 0) лицензии одновременно.
        # Отозванные (revoked = 1) записи в индекс не входят, поэтому
        # историю по каналу хранить можно сколько угодно — ограничение
        # действует только на текущую, ещё не отозванную лицензию.
        # Это защищает от дублей даже если команда -license по какой-то
        # причине (двойное нажатие, второй экземпляр бота и т.п.)
        # обработается больше одного раза — Postgres просто отклонит
        # вторую попытку на уровне базы.
        cur.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_licenses_channel_active
                ON licenses (channel_id)
                WHERE revoked = 0;
        """)
        # Таблица для защиты от повторной обработки одного и того же
        # сообщения Discord (см. claim_message ниже) — на случай двойной
        # доставки события от Discord или двух одновременно запущенных
        # процессов бота с одним токеном.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS processed_messages (
                message_id    BIGINT PRIMARY KEY,
                processed_at  TEXT   NOT NULL
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

    Поднимает ChannelAlreadyLicensedError, если на указанный канал уже
    выдана активная лицензия (проверяется на уровне базы данных через
    уникальный индекс — см. init_db — поэтому защита работает даже при
    гонке между двумя одновременными вызовами).
    """
    try:
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
    except psycopg2.errors.UniqueViolation:
        existing = get_active_license_by_channel(channel_id)
        if existing is not None:
            raise ChannelAlreadyLicensedError(existing) from None
        raise


# ────────────────────────────────────────────────────────────
#  ЗАЩИТА ОТ ДВОЙНОЙ ОБРАБОТКИ ОДНОГО СООБЩЕНИЯ
# ────────────────────────────────────────────────────────────

def claim_message(message_id: int) -> bool:
    """
    Атомарно "занимает" ID сообщения Discord для обработки команды.

    Возвращает True — если это сообщение обрабатывается впервые, команду
    можно выполнять.
    Возвращает False — если это сообщение уже было занято раньше (например,
    Discord доставил событие повторно, либо параллельно работают два
    процесса бота с одним токеном) — в этом случае команду нужно
    проигнорировать, чтобы не выполнить её второй раз.

    Работает на уровне PostgreSQL (INSERT ... ON CONFLICT DO NOTHING),
    поэтому гарантия "выполнится только один раз" действует даже между
    разными процессами, не только внутри одного.
    """
    with _lock, _connect() as conn:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO processed_messages (message_id, processed_at)
               VALUES (%s, %s)
               ON CONFLICT (message_id) DO NOTHING
               RETURNING message_id""",
            (message_id, _now_iso()),
        )
        return cur.fetchone() is not None


def cleanup_processed_messages(older_than_hours: int = 24) -> int:
    """
    Удаляет из processed_messages записи старше указанного количества
    часов — таблица нужна только чтобы поймать повторную доставку
    сообщения в течение короткого времени, хранить её вечно незачем.
    Возвращает количество удалённых строк.
    """
    with _lock, _connect() as conn:
        cur = conn.cursor()
        cur.execute(
            """DELETE FROM processed_messages
               WHERE processed_at < %s""",
            ((datetime.now(timezone.utc) - timedelta(hours=older_than_hours)).isoformat(),),
        )
        return cur.rowcount


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


def get_active_license_by_channel(channel_id: int) -> dict | None:
    """
    Возвращает активную (не отозванную) лицензию, привязанную к каналу,
    либо None, если канал сейчас свободен.
    """
    with _connect() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM licenses WHERE channel_id = %s AND revoked = 0 LIMIT 1",
            (channel_id,),
        )
        row = cur.fetchone()
        return dict(row) if row else None


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
#  ОЧИСТКА ИСТОРИИ ЛИЦЕНЗИЙ
# ────────────────────────────────────────────────────────────

def clear_history(discord_id: int) -> int:
    """
    Полностью удаляет ВСЮ историю лицензий пользователя (активные и
    отозванные записи). Действие необратимо — в отличие от revoke_licenses,
    которая только помечает лицензии отозванными, эта функция стирает
    строки из таблицы licenses насовсем.

    Возвращает количество удалённых записей (0, если истории не было).

    Обратите внимание: сама по себе эта функция НЕ снимает роли и права
    на каналах в Discord — если у пользователя есть активные лицензии,
    их нужно сначала отозвать через revoke_licenses (или -cancel в боте),
    иначе роль/доступ у него останутся, а следов в базе не будет.
    """
    with _lock, _connect() as conn:
        cur = conn.cursor()
        cur.execute(
            "DELETE FROM licenses WHERE discord_id = %s",
            (discord_id,),
        )
        return cur.rowcount


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
