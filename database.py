# -*- coding: utf-8 -*-
"""
Модуль для работы с базой данных лицензий (SQLite).
"""
import sqlite3
import datetime

DB_NAME = "license_data.db"

def init_db():
    """Инициализация базы данных и создание таблиц."""
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    
    # Таблица лицензий
    c.execute('''CREATE TABLE IF NOT EXISTS licenses (
        license_id TEXT PRIMARY KEY,
        user_id INTEGER,
        license_type INTEGER,
        channel_id INTEGER,
        status TEXT DEFAULT 'active',
        issued_at TEXT,
        revoked_at TEXT
    )''')
    
    # Таблица заметок (комментариев) к пользователям
    c.execute('''CREATE TABLE IF NOT EXISTS notes (
        user_id INTEGER PRIMARY KEY,
        note_text TEXT,
        updated_by INTEGER,
        updated_at TEXT
    )''')
    
    conn.commit()
    conn.close()

def generate_license_id(license_type: int) -> str:
    """Генерирует уникальный ID лицензии (RPP-0000001 или RPM-0000001)."""
    prefix = "RPP-" if license_type == 1 else "RPM-"
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    
    # Ищем максимальный номер для данного префикса
    c.execute("SELECT license_id FROM licenses WHERE license_id LIKE ? ORDER BY license_id DESC LIMIT 1", (f"{prefix}%",))
    row = c.fetchone()
    conn.close()
    
    if row:
        last_num = int(row[0].split('-')[1])
        new_num = last_num + 1
    else:
        new_num = 1
        
    return f"{prefix}{str(new_num).zfill(7)}"

def add_license(user_id: int, license_type: int, channel_id: int) -> str:
    """Добавляет новую лицензию в БД и возвращает её ID."""
    license_id = generate_license_id(license_type)
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute('''INSERT INTO licenses (license_id, user_id, license_type, channel_id, status, issued_at)
                 VALUES (?, ?, ?, ?, 'active', ?)''', 
              (license_id, user_id, license_type, channel_id, now))
    conn.commit()
    conn.close()
    return license_id

def get_user_licenses(user_id: int, license_type: int = None, status: str = 'active'):
    """Получает лицензии пользователя. Можно фильтровать по типу и статусу."""
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    
    if license_type:
        c.execute("SELECT * FROM licenses WHERE user_id = ? AND license_type = ? AND status = ?", 
                  (user_id, license_type, status))
    else:
        c.execute("SELECT * FROM licenses WHERE user_id = ? AND status = ?", (user_id, status))
        
    rows = c.fetchall()
    conn.close()
    return rows

def revoke_licenses(user_id: int, license_type: int):
    """Отзывает все активные лицензии указанного типа у пользователя."""
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute('''UPDATE licenses SET status = 'revoked', revoked_at = ? 
                 WHERE user_id = ? AND license_type = ? AND status = 'active' ''',
              (now, user_id, license_type))
    conn.commit()
    conn.close()

def get_note(user_id: int):
    """Получает заметку к пользователю."""
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT note_text, updated_by, updated_at FROM notes WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    conn.close()
    return row

def set_note(user_id: int, note_text: str, updated_by: int):
    """Добавляет или обновляет заметку к пользователю."""
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute('''INSERT INTO notes (user_id, note_text, updated_by, updated_at) 
                 VALUES (?, ?, ?, ?)
                 ON CONFLICT(user_id) DO UPDATE SET 
                 note_text=excluded.note_text, updated_by=excluded.updated_by, updated_at=excluded.updated_at''',
              (user_id, note_text, updated_by, now))
    conn.commit()
    conn.close()

def search_licenses(query: str):
    """Ищет лицензии по ID лицензии, ID пользователя или возвращает все для поиска по нику (обработка в bot.py)."""
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    
    # Пытаемся найти по точному совпадению ID лицензии или ID пользователя
    if query.isdigit():
        c.execute("SELECT * FROM licenses WHERE user_id = ? OR license_id = ?", (int(query), query))
    else:
        c.execute("SELECT * FROM licenses WHERE license_id LIKE ?", (f"%{query}%",))
        
    rows = c.fetchall()
    conn.close()
    return rows

def get_all_active_user_ids():
    """Возвращает список всех уникальных user_id с активными лицензиями (для поиска по нику)."""
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT DISTINCT user_id FROM licenses WHERE status = 'active'")
    rows = [row[0] for row in c.fetchall()]
    conn.close()
    return rows