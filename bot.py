#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ربات تلگرام تبدیل ویس به متن و پاسخ‌گوی انسانی، نسخه ۷."""

from __future__ import annotations

import asyncio
from collections import OrderedDict, defaultdict, deque
from contextlib import suppress
from functools import wraps
import hashlib
import io
import logging
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
import wave
from typing import Any, Dict, List, Optional, Set, Tuple, Union

# فعال‌سازی موتور فوق‌سریع uvloop در لینوکس در صورت نصب بودن
try:
    import uvloop
    uvloop.install()
except Exception:
    pass

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.constants import ChatType
from telegram.error import BadRequest
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram.request import HTTPXRequest

from google import genai
from google.genai import types

from humanizer_skill import (
    HUMAN_TELEGRAM_REPLY_SYSTEM_PROMPT,
    SUMMARY_SYSTEM_PROMPT,
    TRANSCRIPTION_CLEANUP_PROMPT,
    TRANSLATION_SYSTEM_PROMPT,
    strip_ai_punctuation_and_artifacts,
)


BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
DB_PATH = os.getenv("DB_PATH", "voice_bot.db").strip() or "voice_bot.db"

RAW_ALLOWED_IDS = os.getenv("ALLOWED_USER_IDS", "").strip()
ALLOWED_USER_IDS: Set[int] = {
    int(uid.strip())
    for uid in RAW_ALLOWED_IDS.split(",")
    if uid.strip().isdigit()
}

PRIVATE_CHAT_ONLY = os.getenv("PRIVATE_CHAT_ONLY", "true").strip().lower() in ("1", "true", "yes")


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    try:
        return max(minimum, int(os.getenv(name, str(default)).strip()))
    except (TypeError, ValueError):
        return default


RATE_LIMIT_MAX_REQUESTS = _env_int("RATE_LIMIT_MAX_REQUESTS", 15)
RATE_LIMIT_WINDOW_SECONDS = _env_int("RATE_LIMIT_WINDOW_SECONDS", 60)
AI_REQUEST_TIMEOUT_SECONDS = _env_int("AI_REQUEST_TIMEOUT_SECONDS", 90)
AI_MAX_CONCURRENCY = _env_int("AI_MAX_CONCURRENCY", 8)
RECORD_CACHE_TTL_SECONDS = _env_int("RECORD_CACHE_TTL_SECONDS", 900)
PRECOMPUTE_ENABLED = os.getenv("PRECOMPUTE_ENABLED", "true").strip().lower() in ("1", "true", "yes")
LOG_TIMINGS = os.getenv("LOG_TIMINGS", "false").strip().lower() in ("1", "true", "yes")

MAX_TELEGRAM_FILE_SIZE_BYTES = 20 * 1024 * 1024
TELEGRAM_MAX_MSG_LENGTH = 4000

# مدل‌های نسل جدید هوش مصنوعی گوگل با بالاترین کیفیت و کارایی (Gemini 3.x Series)
TRANSCRIBE_MODEL = os.getenv("TRANSCRIBE_MODEL", "gemini-3.5-transcribe").strip()
CHAT_MODEL = os.getenv("CHAT_MODEL", "gemini-3.8-flash").strip()
TTS_MODEL = os.getenv("TTS_MODEL", "gemini-3.8-flash-tts").strip()
TRANSLATE_MODEL = os.getenv("TRANSLATE_MODEL", "gemini-3.8-flash").strip()

FAST_MODELS = (
    "gemini-3.8-flash",
    "gemini-3.5-transcribe",
    "gemini-3.1-pro-preview",
)

# آستانه حجم فایل برای فشرده‌سازی سریع با ffmpeg در حافظه (۳۵۰ کیلوبایت)
FFMPEG_COMPRESS_THRESHOLD_BYTES = 350 * 1024


# ==================== فیلتر امنیتی لاگ‌ها (جلوگیری از لو رفتن توکن در لاگ پابلیک گیت‌هاب) ====================
_RE_BOT_TOKEN_MASK = re.compile(r"bot\d{8,12}:[A-Za-z0-9_-]{30,}")
_RE_KEY_PARAM_MASK = re.compile(r"key=[A-Za-z0-9_\-\.]+")


class SecretMaskingFilter(logging.Filter):
    """مخفی‌سازی کامل BOT_TOKEN و GEMINI_API_KEY در تمام لاگ‌های کنسول گیت‌هاب اکشنز"""

    def __init__(self, secrets_to_mask: list[str]) -> None:
        super().__init__()
        self.secrets = [s for s in secrets_to_mask if s and len(s) > 6]

    def _mask(self, text: str) -> str:
        masked = text
        for secret in self.secrets:
            if secret in masked:
                masked = masked.replace(secret, "***REDACTED_SECRET***")
        masked = _RE_BOT_TOKEN_MASK.sub("bot***REDACTED***", masked)
        masked = _RE_KEY_PARAM_MASK.sub("key=***REDACTED***", masked)
        return masked

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = self._mask(record.getMessage())
        record.args = ()
        return True


class SecretMaskingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return secret_filter._mask(super().format(record))


logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)

secret_filter = SecretMaskingFilter([BOT_TOKEN, GEMINI_API_KEY])
for handler in logging.root.handlers:
    handler.addFilter(secret_filter)
    handler.setFormatter(SecretMaskingFormatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s"))

logger = logging.getLogger("VoiceAndPasokhBot")
logger.addFilter(secret_filter)


# ==================== سیستم ضد تبلیغات، ضد اسپم و کنترل دسترسی ====================
_user_request_timestamps: dict[int, deque[float]] = defaultdict(deque)
_RATE_LIMIT_LOCK = threading.RLock()
_ACTIVE_MESSAGE_KEYS: set[tuple[int, int]] = set()
_ACTIVE_MESSAGE_LOCK = threading.RLock()

AD_AND_SPAM_PATTERNS = re.compile(
    r"(?:"
    r"t\.me/(?:\+|joinchat/)[A-Za-z0-9_-]+|"
    r"telegram\.me/(?:\+|joinchat/)[A-Za-z0-9_-]+|"
    r"(?:تبلیغات|سفارش تبلیغ|کسب درآمد دلاری|ایردراپ رایگان|پامپ ارز|سایت شرط\s*بندی|بت فوروارد|وان ایکس بت)"
    r")",
    re.IGNORECASE,
)
_RE_LINKS_COUNT = re.compile(r"(?:https?://|t\.me/|@[A-Za-z0-9_]{5,})")


def is_user_authorized(update: Update) -> bool:
    user = update.effective_user
    chat = update.effective_chat
    if not user or user.is_bot:
        return False
    if PRIVATE_CHAT_ONLY and chat and chat.type != ChatType.PRIVATE:
        return False
    if ALLOWED_USER_IDS and user.id not in ALLOWED_USER_IDS:
        return False
    return True


def prune_rate_limit_state(now: Optional[float] = None) -> None:
    current_time = now if now is not None else time.time()
    window_start = current_time - RATE_LIMIT_WINDOW_SECONDS
    with _RATE_LIMIT_LOCK:
        expired_users = []
        for tracked_user_id, timestamps in _user_request_timestamps.items():
            while timestamps and timestamps[0] <= window_start:
                timestamps.popleft()
            if not timestamps:
                expired_users.append(tracked_user_id)
        for tracked_user_id in expired_users:
            _user_request_timestamps.pop(tracked_user_id, None)


def is_rate_limited(user_id: int) -> bool:
    if ALLOWED_USER_IDS and user_id in ALLOWED_USER_IDS:
        return False
    current_time = time.time()
    window_start = current_time - RATE_LIMIT_WINDOW_SECONDS
    with _RATE_LIMIT_LOCK:
        timestamps = _user_request_timestamps[user_id]
        while timestamps and timestamps[0] <= window_start:
            timestamps.popleft()
        if len(timestamps) >= RATE_LIMIT_MAX_REQUESTS:
            return True
        timestamps.append(current_time)
        if len(_user_request_timestamps) > 1000:
            prune_rate_limit_state(current_time)
        return False


def claim_message_processing(chat_id: int, message_id: int) -> bool:
    key = (chat_id, message_id)
    with _ACTIVE_MESSAGE_LOCK:
        if key in _ACTIVE_MESSAGE_KEYS:
            return False
        _ACTIVE_MESSAGE_KEYS.add(key)
        return True


def release_message_processing(chat_id: int, message_id: int) -> None:
    with _ACTIVE_MESSAGE_LOCK:
        _ACTIVE_MESSAGE_KEYS.discard((chat_id, message_id))


def is_advertising_or_spam(text: str, user_id: int) -> bool:
    if ALLOWED_USER_IDS and user_id in ALLOWED_USER_IDS:
        return False
    if not text:
        return False
    if AD_AND_SPAM_PATTERNS.search(text):
        return True
    if len(_RE_LINKS_COUNT.findall(text)) >= 4:
        return True
    return False


def sanitize_error_message(err: Exception) -> str:
    msg = str(err)
    for s in (BOT_TOKEN, GEMINI_API_KEY):
        if s:
            msg = msg.replace(s, "***")
    msg = _RE_BOT_TOKEN_MASK.sub("bot***", msg)
    msg = _RE_KEY_PARAM_MASK.sub("key=***", msg)
    return msg[:160]


# ==================== کش فوق‌سریع RAM (LRU Cache) + دیتابیس دائمی SQLite ====================
MAX_RAM_CACHE_SIZE = 500
_RAM_RECORDS_BY_ID: OrderedDict[int, dict] = OrderedDict()
_RAM_MSG_TO_RECORD_ID: OrderedDict[tuple[int, int], int] = OrderedDict()
_RAM_RECORD_CACHE_TIMES: dict[int, float] = {}
_PERSISTENT_DB_CONN: Optional[sqlite3.Connection] = None
_DB_LOCK = threading.RLock()
_DB_PATH_IN_USE: Optional[str] = None
_AI_SEMAPHORE: Optional[asyncio.Semaphore] = None
_AI_SEMAPHORE_LOOP: Optional[asyncio.AbstractEventLoop] = None
# نگهداری تسک‌های پیش‌تولید در پس‌زمینه تا اگر کاربر زودتر کلیک کرد، به همان تسک در حال اجرا وصل شود
_INFLIGHT_REPLY_TASKS: dict[int, asyncio.Task] = {}
_INFLIGHT_SUMMARY_TASKS: dict[int, asyncio.Task] = {}
_INFLIGHT_TRANSLATION_TASKS: dict[str, asyncio.Task] = {}


def _get_db_connection() -> sqlite3.Connection:
    global _PERSISTENT_DB_CONN, _DB_PATH_IN_USE
    with _DB_LOCK:
        if _PERSISTENT_DB_CONN is None or _DB_PATH_IN_USE != DB_PATH:
            if _PERSISTENT_DB_CONN is not None:
                with suppress(Exception):
                    _PERSISTENT_DB_CONN.close()
            conn = sqlite3.connect(DB_PATH, timeout=30.0, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA synchronous=NORMAL;")
            conn.execute("PRAGMA temp_store=MEMORY;")
            conn.execute("PRAGMA cache_size=-16000;")
            conn.execute("PRAGMA busy_timeout=30000;")
            _PERSISTENT_DB_CONN = conn
            _DB_PATH_IN_USE = DB_PATH
    return _PERSISTENT_DB_CONN


def close_db() -> None:
    global _PERSISTENT_DB_CONN, _DB_PATH_IN_USE
    with _DB_LOCK:
        if _PERSISTENT_DB_CONN is not None:
            with suppress(Exception):
                _PERSISTENT_DB_CONN.close()
        _PERSISTENT_DB_CONN = None
        _DB_PATH_IN_USE = None


def reset_runtime_state() -> None:
    with _DB_LOCK:
        _RAM_RECORDS_BY_ID.clear()
        _RAM_MSG_TO_RECORD_ID.clear()
        _RAM_RECORD_CACHE_TIMES.clear()
        _USER_SETTINGS_CACHE.clear()
    with _RATE_LIMIT_LOCK:
        _user_request_timestamps.clear()
    with _ACTIVE_MESSAGE_LOCK:
        _ACTIVE_MESSAGE_KEYS.clear()


def _cache_put(record: dict) -> None:
    rec_id = record["record_id"]
    previous = _RAM_RECORDS_BY_ID.get(rec_id)
    if previous and previous.get("bot_msg_id") != record.get("bot_msg_id"):
        _RAM_MSG_TO_RECORD_ID.pop((previous["chat_id"], previous.get("bot_msg_id")), None)
    _RAM_RECORDS_BY_ID[rec_id] = dict(record)
    _RAM_RECORD_CACHE_TIMES[rec_id] = time.monotonic()
    _RAM_RECORDS_BY_ID.move_to_end(rec_id)
    while len(_RAM_RECORDS_BY_ID) > MAX_RAM_CACHE_SIZE:
        removed_id, _ = _RAM_RECORDS_BY_ID.popitem(last=False)
        _RAM_RECORD_CACHE_TIMES.pop(removed_id, None)
        for key, cached_id in list(_RAM_MSG_TO_RECORD_ID.items()):
            if cached_id == removed_id:
                _RAM_MSG_TO_RECORD_ID.pop(key, None)

    chat_id = record["chat_id"]
    if record.get("voice_msg_id"):
        _RAM_MSG_TO_RECORD_ID[(chat_id, record["voice_msg_id"])] = rec_id
    if record.get("bot_msg_id"):
        _RAM_MSG_TO_RECORD_ID[(chat_id, record["bot_msg_id"])] = rec_id
    while len(_RAM_MSG_TO_RECORD_ID) > MAX_RAM_CACHE_SIZE * 2:
        _RAM_MSG_TO_RECORD_ID.popitem(last=False)


def _cache_get(record_id: int) -> Optional[dict]:
    record = _RAM_RECORDS_BY_ID.get(record_id)
    if record is None:
        return None
    if time.monotonic() - _RAM_RECORD_CACHE_TIMES.get(record_id, 0) > RECORD_CACHE_TTL_SECONDS:
        _RAM_RECORDS_BY_ID.pop(record_id, None)
        _RAM_RECORD_CACHE_TIMES.pop(record_id, None)
        return None
    _RAM_RECORDS_BY_ID.move_to_end(record_id)
    return dict(record)


def init_db() -> None:
    conn = _get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS voice_records (
            record_id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            user_id INTEGER,
            voice_msg_id INTEGER NOT NULL,
            bot_msg_id INTEGER,
            transcript TEXT NOT NULL,
            summary TEXT,
            human_reply TEXT,
            translation TEXT,
            created_at REAL NOT NULL
        )
        """
    )
    cursor.execute("PRAGMA table_info(voice_records)")
    existing_cols = {row["name"] for row in cursor.fetchall()}
    if "bot_msg_id" not in existing_cols:
        cursor.execute("ALTER TABLE voice_records ADD COLUMN bot_msg_id INTEGER")
    if "translation" not in existing_cols:
        cursor.execute("ALTER TABLE voice_records ADD COLUMN translation TEXT")
    if "user_id" not in existing_cols:
        cursor.execute("ALTER TABLE voice_records ADD COLUMN user_id INTEGER")

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS user_settings (
            user_id INTEGER PRIMARY KEY,
            chat_model TEXT DEFAULT 'gemini-3.8-flash',
            thinking_level TEXT DEFAULT 'HIGH',
            transcribe_model TEXT DEFAULT 'gemini-3.5-transcribe'
        )
        """
    )

    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_chat_voice ON voice_records(chat_id, voice_msg_id)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_chat_bot_msg ON voice_records(chat_id, bot_msg_id)"
    )
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_created_at ON voice_records(created_at)")
    cursor.execute(
        "CREATE TABLE IF NOT EXISTS translation_cache (record_id INTEGER NOT NULL, "
        "source_hash TEXT NOT NULL, translation TEXT NOT NULL, PRIMARY KEY (record_id, source_hash))"
    )
    cursor.execute(
        "CREATE TABLE IF NOT EXISTS transcription_cache (user_id INTEGER NOT NULL, "
        "file_id TEXT NOT NULL, model TEXT NOT NULL, transcript TEXT NOT NULL, "
        "PRIMARY KEY (user_id, file_id, model))"
    )
    conn.commit()


# ==================== مدیریت تنظیمات اختصاصی کاربر (User Settings Store) ====================
_USER_SETTINGS_CACHE: dict[int, dict] = {}


def get_user_model_settings(user_id: int) -> dict:
    if user_id in _USER_SETTINGS_CACHE:
        return dict(_USER_SETTINGS_CACHE[user_id])
    conn = _get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT chat_model, thinking_level, transcribe_model FROM user_settings WHERE user_id = ?",
        (user_id,),
    )
    row = cursor.fetchone()
    if row:
        settings = dict(row)
    else:
        settings = {
            "chat_model": CHAT_MODEL,
            "thinking_level": "HIGH",
            "transcribe_model": TRANSCRIBE_MODEL,
        }
        cursor.execute(
            """
            INSERT OR IGNORE INTO user_settings (user_id, chat_model, thinking_level, transcribe_model)
            VALUES (?, ?, ?, ?)
            """,
            (user_id, settings["chat_model"], settings["thinking_level"], settings["transcribe_model"]),
        )
        conn.commit()
    _USER_SETTINGS_CACHE[user_id] = settings
    return dict(settings)


def update_user_setting(user_id: int, key: str, value: str) -> dict:
    if key not in ("chat_model", "thinking_level", "transcribe_model"):
        return get_user_model_settings(user_id)
    settings = get_user_model_settings(user_id)
    settings[key] = value
    conn = _get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        f"""
        INSERT INTO user_settings (user_id, {key})
        VALUES (?, ?)
        ON CONFLICT(user_id) DO UPDATE SET {key} = excluded.{key}
        """,
        (user_id, value),
    )
    conn.commit()
    _USER_SETTINGS_CACHE[user_id] = settings
    return dict(settings)


def save_record(
    chat_id: int,
    msg_id: int,
    transcript: str,
    human_reply: Optional[str] = None,
    bot_msg_id: Optional[int] = None,
    summary: Optional[str] = None,
    translation: Optional[str] = None,
    user_id: Optional[int] = None,
) -> int:
    now = time.time()
    conn = _get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO voice_records (chat_id, user_id, voice_msg_id, bot_msg_id, transcript, summary, human_reply, translation, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (chat_id, user_id, msg_id, bot_msg_id, transcript, summary, human_reply, translation, now),
    )
    record_id = int(cursor.lastrowid)
    conn.commit()

    record = {
        "record_id": record_id,
        "chat_id": chat_id,
        "user_id": user_id,
        "voice_msg_id": msg_id,
        "bot_msg_id": bot_msg_id,
        "transcript": transcript,
        "summary": summary,
        "human_reply": human_reply,
        "translation": translation,
        "created_at": now,
    }
    _cache_put(record)
    return record_id


def get_record(record_id: int) -> Optional[dict]:
    cached = _cache_get(record_id)
    if cached is not None:
        return cached

    conn = _get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM voice_records WHERE record_id = ?", (record_id,))
    row = cursor.fetchone()
    if row:
        rec = dict(row)
        _cache_put(rec)
        return dict(rec)
    return None


def get_record_by_msg_id(chat_id: int, msg_id: int) -> Optional[dict]:
    cached_id = _RAM_MSG_TO_RECORD_ID.get((chat_id, msg_id))
    if cached_id:
        cached = _cache_get(cached_id)
        if cached is not None and msg_id in (cached.get("voice_msg_id"), cached.get("bot_msg_id")):
            return cached
        _RAM_MSG_TO_RECORD_ID.pop((chat_id, msg_id), None)

    conn = _get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT * FROM voice_records
        WHERE chat_id = ? AND (voice_msg_id = ? OR bot_msg_id = ?)
        ORDER BY record_id DESC LIMIT 1
        """,
        (chat_id, msg_id, msg_id),
    )
    row = cursor.fetchone()
    if row:
        rec = dict(row)
        _cache_put(rec)
        return dict(rec)
    return None


def update_record_field(record_id: int, field: str, value: str | int) -> None:
    if field not in ("summary", "human_reply", "transcript", "bot_msg_id", "translation"):
        return
    conn = _get_db_connection()
    cursor = conn.cursor()
    if field == "human_reply":
        cursor.execute(
            "UPDATE voice_records SET human_reply = ?, translation = NULL WHERE record_id = ?",
            (value, record_id),
        )
    else:
        cursor.execute(
            f"UPDATE voice_records SET {field} = ? WHERE record_id = ?",
            (value, record_id),
        )
    conn.commit()
    cached = _RAM_RECORDS_BY_ID.get(record_id)
    if cached is not None:
        updated = dict(cached)
        updated[field] = value
        if field == "human_reply":
            updated["translation"] = None
        _cache_put(updated)


def can_access_record(record: Optional[dict], chat_id: int, user_id: int) -> bool:
    if not record or record.get("chat_id") != chat_id:
        return False
    owner = record.get("user_id")
    return owner == user_id or (owner is None and chat_id == user_id)


def _locked_db_function(function):
    @wraps(function)
    def locked(*args, **kwargs):
        with _DB_LOCK:
            return function(*args, **kwargs)
    return locked


def translation_cache_key(record_id: int, source_text: str, settings: dict) -> str:
    payload = "\0".join((str(record_id), source_text, settings.get("chat_model", ""),
                          settings.get("thinking_level", "")))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@_locked_db_function
def get_cached_translation(record_id: int, source_hash: str) -> Optional[str]:
    row = _get_db_connection().execute(
        "SELECT translation FROM translation_cache WHERE record_id = ? AND source_hash = ?",
        (record_id, source_hash),
    ).fetchone()
    return row[0] if row else None


@_locked_db_function
def save_cached_translation(record_id: int, source_hash: str, translation: str) -> None:
    conn = _get_db_connection()
    conn.execute("INSERT OR REPLACE INTO translation_cache (record_id, source_hash, translation) VALUES (?, ?, ?)",
                 (record_id, source_hash, translation))
    conn.commit()


@_locked_db_function
def get_cached_transcript(user_id: int, file_id: str, model: str) -> Optional[str]:
    row = _get_db_connection().execute(
        "SELECT transcript FROM transcription_cache WHERE user_id = ? AND file_id = ? AND model = ?",
        (user_id, file_id, model),
    ).fetchone()
    return row[0] if row else None


@_locked_db_function
def save_cached_transcript(user_id: int, file_id: str, model: str, transcript: str) -> None:
    conn = _get_db_connection()
    conn.execute("INSERT OR REPLACE INTO transcription_cache (user_id, file_id, model, transcript) VALUES (?, ?, ?, ?)",
                 (user_id, file_id, model, transcript))
    conn.commit()


init_db = _locked_db_function(init_db)
get_user_model_settings = _locked_db_function(get_user_model_settings)
update_user_setting = _locked_db_function(update_user_setting)
save_record = _locked_db_function(save_record)
get_record = _locked_db_function(get_record)
get_record_by_msg_id = _locked_db_function(get_record_by_msg_id)
update_record_field = _locked_db_function(update_record_field)


# ==================== کیبوردهای شیشه‌ای (Inline Keyboards) ====================
def build_voice_action_keyboard(record_id: int, has_reply: bool = False) -> InlineKeyboardMarkup:
    buttons = [
        [
            InlineKeyboardButton("💬 جواب انسانی", callback_data=f"reply:{record_id}"),
            InlineKeyboardButton("📝 خلاصه", callback_data=f"sum:{record_id}"),
        ],
        [
            InlineKeyboardButton("🌐 ترجمه", callback_data=f"trans:{record_id}"),
            InlineKeyboardButton("🔊 ارسال ویس (TTS)", callback_data=f"tts:{record_id}"),
        ],
    ]
    sub_row = []
    if has_reply:
        sub_row.append(InlineKeyboardButton("🔄 مدل دیگر", callback_data=f"regen:{record_id}"))
    sub_row.append(InlineKeyboardButton("⚙️ تنظیمات مدل", callback_data="open_settings"))
    buttons.append(sub_row)
    return InlineKeyboardMarkup(buttons)


def build_reply_keyboard(record_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🔄 مدل دیگر", callback_data=f"regen:{record_id}"),
                InlineKeyboardButton("📝 خلاصه پیام", callback_data=f"sum:{record_id}"),
            ],
            [
                InlineKeyboardButton("🌐 ترجمه", callback_data=f"trans:{record_id}"),
                InlineKeyboardButton("🔊 ارسال ویس (TTS)", callback_data=f"tts:{record_id}"),
            ],
            [
                InlineKeyboardButton("⚙️ تنظیمات مدل", callback_data="open_settings"),
            ],
        ]
    )


def build_settings_keyboard(user_id: int) -> InlineKeyboardMarkup:
    s = get_user_model_settings(user_id)
    c_model = s.get("chat_model", "gemini-3.8-flash")
    t_level = s.get("thinking_level", "HIGH")
    tr_model = s.get("transcribe_model", "gemini-3.5-transcribe")

    is_flash_high = (c_model == "gemini-3.8-flash" and t_level == "HIGH")
    is_flash_turbo = (c_model == "gemini-3.8-flash" and t_level == "OFF")
    is_pro = (c_model == "gemini-3.1-pro-preview")
    is_trans_35 = (tr_model == "gemini-3.5-transcribe")

    buttons = [
        [
            InlineKeyboardButton(
                f"{'✅ ' if is_flash_high else ''}⚡ Gemini 3.8 Flash (تفکر عمیق High)",
                callback_data="set_cfg:chat:flash_high",
            )
        ],
        [
            InlineKeyboardButton(
                f"{'✅ ' if is_flash_turbo else ''}🚀 Gemini 3.8 Flash (توربو / بدون تفکر)",
                callback_data="set_cfg:chat:flash_turbo",
            )
        ],
        [
            InlineKeyboardButton(
                f"{'✅ ' if is_pro else ''}🧠 Gemini 3.1 Pro Preview (نهایت استدلال)",
                callback_data="set_cfg:chat:pro",
            )
        ],
        [
            InlineKeyboardButton(
                f"🎙️ مدل ویس: {'3.5 Transcribe' if is_trans_35 else '3.8 Flash'} (تغییر)",
                callback_data="set_cfg:transcribe:toggle",
            )
        ],
        [
            InlineKeyboardButton("❌ بستن تنظیمات", callback_data="close_settings")
        ],
    ]
    return InlineKeyboardMarkup(buttons)


# ==================== توابع کمکی ارسال و ویرایش ایمن پیام در تلگرام ====================
def split_text_chunks(full_text: str, header: str = "", footer: str = "") -> list[str]:
    if len(header) + len(full_text) + len(footer) <= TELEGRAM_MAX_MSG_LENGTH:
        return [f"{header}{full_text}{footer}"]
    chunk_size = TELEGRAM_MAX_MSG_LENGTH - len(header) - len(footer) - 64
    if chunk_size <= 0:
        raise ValueError("سربرگ یا پاورقی پیام بیش از حد طولانی است")
    chunks = [full_text[index:index + chunk_size] for index in range(0, len(full_text), chunk_size)] or [""]
    total = len(chunks)
    return [
        f"{header}(بخش {index} از {total})\n\n{chunk}{footer if index == total else ''}"
        for index, chunk in enumerate(chunks, start=1)
    ]


async def safe_edit_or_send_long_text(
    status_msg: Message,
    origin_msg: Message,
    full_text: str,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
    header_prefix: str = "",
    footer_suffix: str = "",
) -> None:
    chunks = split_text_chunks(full_text, header_prefix, footer_suffix)
    if len(chunks) == 1:
        try:
            await status_msg.edit_text(chunks[0], reply_markup=reply_markup)
        except BadRequest as br:
            if "Message is not modified" not in str(br):
                raise
        return

    await status_msg.edit_text(chunks[0])

    for idx, chunk in enumerate(chunks[1:], start=2):
        is_last = idx == len(chunks)
        sent = await origin_msg.reply_text(
            chunk,
            reply_to_message_id=origin_msg.message_id,
            reply_markup=reply_markup if is_last else None,
        )
        if is_last and reply_markup:
            rec = get_record_by_msg_id(origin_msg.chat_id, origin_msg.message_id)
            if rec:
                update_record_field(rec["record_id"], "bot_msg_id", sent.message_id)


async def send_long_text(origin_msg: Message, full_text: str, reply_to_id: int,
                         reply_markup: Optional[InlineKeyboardMarkup] = None,
                         header_prefix: str = "") -> Message:
    chunks = split_text_chunks(full_text, header_prefix)
    sent = None
    for index, chunk in enumerate(chunks):
        sent = await origin_msg.reply_text(
            chunk, reply_to_message_id=reply_to_id,
            reply_markup=reply_markup if index == len(chunks) - 1 else None,
            allow_sending_without_reply=True,
        )
    return sent


def displayed_record_text(message: Message, record: dict) -> str:
    markup = getattr(message, "reply_markup", None)
    rows = getattr(markup, "inline_keyboard", ()) if markup else ()
    is_reply = any(
        str(getattr(button, "callback_data", "")).startswith("regen:")
        for row in rows for button in row
    )
    if is_reply and record.get("human_reply"):
        return record["human_reply"]
    return record["transcript"]


# ==================== فشرده‌سازی فوق‌سریع صوت در حافظه (برای ویدیومسیج و فایل‌های حجیم) ====================
def _compress_audio_in_memory_sync(raw_bytes: bytes) -> tuple[bytes, str]:
    """تبدیل سریع ویدیومسیج یا فایل صوتی سنگین به صدای مونو 16kHz Opus کم‌حجم در RAM برای آپلود آنی"""
    try:
        proc = subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                "pipe:0",
                "-vn",
                "-ac",
                "1",
                "-ar",
                "16000",
                "-c:a",
                "libopus",
                "-b:a",
                "24k",
                "-f",
                "ogg",
                "pipe:1",
            ],
            input=raw_bytes,
            capture_output=True,
            timeout=8,
            check=True,
        )
        if proc.stdout and len(proc.stdout) < len(raw_bytes):
            return proc.stdout, "audio/ogg"
    except Exception:
        pass
    return raw_bytes, ""


async def optimize_media_for_fast_upload(raw_bytes: bytes, mime_type: str, is_video_note: bool) -> tuple[bytes, str]:
    """اگر فایل ویدیومسیج است یا بیش از ۳۵۰ کیلوبایت حجم دارد، در کسری از ثانیه صدای خالص کم‌حجم استخراج می‌شود"""
    if not is_video_note:
        return raw_bytes, mime_type

    compressed, new_mime = await asyncio.to_thread(_compress_audio_in_memory_sync, raw_bytes)
    if new_mime:
        return compressed, new_mime
    return raw_bytes, mime_type


# ==================== موتور هوش مصنوعی پیشرفته (Gemini 3.x Series) ====================
_genai_client: Optional[genai.Client] = None


def get_genai_client() -> genai.Client:
    global _genai_client
    if _genai_client is None:
        if not GEMINI_API_KEY:
            raise RuntimeError("کلید GEMINI_API_KEY تنظیم نشده است.")
        _genai_client = genai.Client(api_key=GEMINI_API_KEY)
    return _genai_client


def _candidate_models(primary_model: str) -> list[str]:
    return [primary_model, primary_model] if primary_model else []


def _build_model_config(
    model_name: str,
    system_instruction: Optional[str] = None,
    temperature: float = 0.5,
    thinking_level: Optional[str] = "HIGH",
    response_modalities: Optional[list[str]] = None,
) -> types.GenerateContentConfig:
    kwargs: dict = {"temperature": temperature}
    if system_instruction:
        kwargs["system_instruction"] = system_instruction
    if response_modalities:
        kwargs["response_modalities"] = response_modalities

    if hasattr(types, "ThinkingConfig"):
        try:
            if "2.5" in model_name and thinking_level == "OFF":
                kwargs["thinking_config"] = types.ThinkingConfig(thinking_budget=0)
            elif "2.5" in model_name:
                pass
            elif thinking_level == "OFF":
                kwargs["thinking_config"] = types.ThinkingConfig(thinking_budget=0)
            elif thinking_level in ("HIGH", "MEDIUM", "LOW"):
                kwargs["thinking_config"] = types.ThinkingConfig(thinking_level=thinking_level)
        except Exception as e:
            logger.debug(f"ThinkingConfig setup note: {e}")

    return types.GenerateContentConfig(**kwargs)


async def _call_single_model(
    model_name: str,
    contents: list | str,
    system_instruction: Optional[str] = None,
    temperature: float = 0.5,
    thinking_level: Optional[str] = "HIGH",
    response_modalities: Optional[list[str]] = None,
) -> Any:
    client = get_genai_client()
    config = _build_model_config(
        model_name=model_name,
        system_instruction=system_instruction,
        temperature=temperature,
        thinking_level=thinking_level,
        response_modalities=response_modalities,
    )
    global _AI_SEMAPHORE, _AI_SEMAPHORE_LOOP
    loop = asyncio.get_running_loop()
    if _AI_SEMAPHORE is None or _AI_SEMAPHORE_LOOP is not loop:
        _AI_SEMAPHORE = asyncio.Semaphore(AI_MAX_CONCURRENCY)
        _AI_SEMAPHORE_LOOP = loop
    async with _AI_SEMAPHORE:
        response = await asyncio.wait_for(
            client.aio.models.generate_content(model=model_name, contents=contents, config=config),
            timeout=AI_REQUEST_TIMEOUT_SECONDS,
        )
    if response_modalities and "AUDIO" in response_modalities:
        if response and response.candidates and response.candidates[0].content:
            for part in response.candidates[0].content.parts:
                if part.inline_data and part.inline_data.data:
                    return part.inline_data.data
        raise RuntimeError(f"No audio returned from {model_name}")

    if response and response.text and response.text.strip():
        return response.text.strip()
    raise RuntimeError(f"Empty response from {model_name}")


async def _hedged_generate(
    primary_model: str,
    contents: list | str,
    system_instruction: Optional[str] = None,
    temperature: float = 0.5,
    hedge_delay_sec: float = 3.2,
    thinking_level: Optional[str] = "HIGH",
) -> str:
    """Race requests using the configured model without changing model or thinking level."""
    candidates = _candidate_models(primary_model)
    if not candidates:
        raise ValueError("مدل هوش مصنوعی مشخص نشده است.")
    tasks: set[asyncio.Task] = set()
    last_error: Optional[Exception] = None

    def start_request(model_name: str) -> asyncio.Task:
        return asyncio.create_task(_call_single_model(
            model_name, contents, system_instruction, temperature, thinking_level=thinking_level
        ))

    try:
        primary_task = start_request(candidates[0])
        tasks.add(primary_task)
        done, _ = await asyncio.wait(tasks, timeout=max(0.0, hedge_delay_sec))
        if done:
            try:
                return primary_task.result()
            except Exception as error:
                last_error = error
                logger.warning("Primary request failed: %s", sanitize_error_message(error))
                tasks.discard(primary_task)
        if len(candidates) > 1:
            tasks.add(start_request(candidates[1]))
        while tasks:
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            tasks = set(pending)
            successful_result = None
            for finished in done:
                try:
                    result = finished.result()
                    if successful_result is None:
                        successful_result = result
                except Exception as error:
                    last_error = error
                    logger.warning("Model request failed: %s", sanitize_error_message(error))
            if successful_result is not None:
                return successful_result
        raise RuntimeError("خطا در دریافت پاسخ از سرور هوش مصنوعی.") from last_error
    finally:
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


def _convert_wav_to_ogg_opus_sync(wav_bytes: bytes) -> bytes:
    """تبدیل صوت WAV به فرمت Opus استاندارد ویس تلگرام با ffmpeg"""
    try:
        proc = subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                "pipe:0",
                "-c:a",
                "libopus",
                "-b:a",
                "32k",
                "-f",
                "ogg",
                "pipe:1",
            ],
            input=wav_bytes,
            capture_output=True,
            timeout=10,
            check=True,
        )
        if proc.stdout and len(proc.stdout) > 0:
            return proc.stdout
    except Exception as e:
        logger.warning("ffmpeg conversion to ogg failed: %s", sanitize_error_message(e))
    raise RuntimeError("تبدیل صدا به فرمت تلگرام انجام نشد؛ نصب ffmpeg را بررسی کنید.")


async def generate_speech_audio(text: str) -> bytes:
    """تولید ویس صوتی فوق‌العاده طبیعی با مدل اختصاصی Gemini 3.8 Flash TTS"""
    clean_text = text.replace("*", "").replace("#", "").strip()
    if not clean_text:
        raise ValueError("متن برای تولید صدا خالی است.")
    if len(clean_text) > 800:
        raise ValueError("متن برای تولید صدا بیش از ۸۰۰ نویسه است؛ لطفاً متن کوتاه‌تری انتخاب کنید.")
    raw_wav_bytes = await _call_single_model(
        model_name=TTS_MODEL,
        contents=clean_text,
        response_modalities=["AUDIO"],
    )
    if not raw_wav_bytes.startswith((b"RIFF", b"OggS", b"fLaC", b"ID3")):
        wav_buffer = io.BytesIO()
        with wave.open(wav_buffer, "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(24000)
            wav_file.writeframes(raw_wav_bytes)
        raw_wav_bytes = wav_buffer.getvalue()
    ogg_bytes = await asyncio.to_thread(_convert_wav_to_ogg_opus_sync, raw_wav_bytes)
    return ogg_bytes


async def transcribe_audio_bytes(
    audio_bytes: bytes,
    mime_type: str = "audio/ogg",
    preferred_model: Optional[str] = None,
) -> str:
    audio_part = types.Part.from_bytes(data=audio_bytes, mime_type=mime_type)
    model_to_use = preferred_model or TRANSCRIBE_MODEL
    return await _hedged_generate(
        primary_model=model_to_use,
        contents=[audio_part, "متن این ویس را دقیق بنویس:"],
        system_instruction=TRANSCRIPTION_CLEANUP_PROMPT,
        temperature=0.2,
        hedge_delay_sec=3.8,
        thinking_level="OFF",
    )


async def generate_summary_text(transcript: str, user_id: Optional[int] = None) -> str:
    settings = get_user_model_settings(user_id) if user_id else {}
    model_to_use = settings.get("chat_model") or CHAT_MODEL
    thinking_level = settings.get("thinking_level") or "HIGH"

    return await _hedged_generate(
        primary_model=model_to_use,
        contents=f"متن ورودی:\n{transcript}",
        system_instruction=SUMMARY_SYSTEM_PROMPT,
        temperature=0.4,
        hedge_delay_sec=2.8,
        thinking_level=thinking_level,
    )


async def generate_human_telegram_reply(
    transcript: str,
    variation_hint: str = "",
    user_id: Optional[int] = None,
) -> str:
    settings = get_user_model_settings(user_id) if user_id else {}
    model_to_use = settings.get("chat_model") or CHAT_MODEL
    thinking_level = settings.get("thinking_level") or "HIGH"

    user_content = f"متن پیام یا ویس مخاطب:\n{transcript}"
    if variation_hint:
        user_content += f"\n\nدستورالعمل تکمیلی:\n{variation_hint}"

    raw_reply = await _hedged_generate(
        primary_model=model_to_use,
        contents=user_content,
        system_instruction=HUMAN_TELEGRAM_REPLY_SYSTEM_PROMPT,
        temperature=0.65,
        hedge_delay_sec=2.8,
        thinking_level=thinking_level,
    )
    return strip_ai_punctuation_and_artifacts(raw_reply)


async def translate_text_content(text: str, user_id: Optional[int] = None) -> str:
    """ترجمه روان و اصیل با هوش مصنوعی (فارسی به انگلیسی و برعکس)"""
    settings = get_user_model_settings(user_id) if user_id else {}
    model_to_use = settings.get("chat_model") or CHAT_MODEL
    thinking_level = settings.get("thinking_level") or "HIGH"

    return await _hedged_generate(
        primary_model=model_to_use,
        contents=f"متن زیر را ترجمه کن:\n{text}",
        system_instruction=TRANSLATION_SYSTEM_PROMPT,
        temperature=0.3,
        hedge_delay_sec=2.8,
        thinking_level=thinking_level,
    )


# ==================== پیش‌تولید هوشمند در پس‌زمینه (Speculative Background Pre-Generation) ====================
async def _precompute_reply_for_record(record_id: int, transcript: str, user_id: Optional[int] = None) -> str:
    reply = await generate_human_telegram_reply(transcript, user_id=user_id)
    await asyncio.to_thread(save_precomputed_field, record_id, "human_reply", reply)
    return reply


async def _precompute_summary_for_record(record_id: int, transcript: str, user_id: Optional[int] = None) -> str:
    summary = await generate_summary_text(transcript, user_id=user_id)
    await asyncio.to_thread(save_precomputed_field, record_id, "summary", summary)
    return summary


@_locked_db_function
def save_precomputed_field(record_id: int, field: str, value: str) -> None:
    if field not in ("human_reply", "summary"):
        raise ValueError("Invalid precomputed field")
    conn = _get_db_connection()
    cursor = conn.execute(
        f"UPDATE voice_records SET {field} = ? WHERE record_id = ? AND ({field} IS NULL OR {field} = '')",
        (value, record_id),
    )
    conn.commit()
    if cursor.rowcount:
        cached = _RAM_RECORDS_BY_ID.get(record_id)
        if cached is not None:
            updated = dict(cached)
            updated[field] = value
            _cache_put(updated)


def _get_or_start_task(tasks: dict[int, asyncio.Task], record_id: int, factory) -> asyncio.Task:
    task = tasks.get(record_id)
    if task is not None and not task.done():
        return task
    task = asyncio.create_task(factory())
    tasks[record_id] = task

    def finished(completed: asyncio.Task) -> None:
        if tasks.get(record_id) is completed:
            tasks.pop(record_id, None)
        if not completed.cancelled():
            error = completed.exception()
            if error:
                logger.warning("Background request failed: %s", sanitize_error_message(error))

    task.add_done_callback(finished)
    return task


def trigger_background_precomputation(record_id: int, transcript: str, user_id: Optional[int] = None) -> None:
    """تولید خودکار جواب انسانی و خلاصه در پس‌زمینه به محض آماده شدن متن ویس تا دکمه‌ها در ۰.۰۵ ثانیه کار کنند"""
    if not PRECOMPUTE_ENABLED:
        return
    _get_or_start_task(_INFLIGHT_REPLY_TASKS, record_id,
                       lambda: _precompute_reply_for_record(record_id, transcript, user_id=user_id))
    _get_or_start_task(_INFLIGHT_SUMMARY_TASKS, record_id,
                       lambda: _precompute_summary_for_record(record_id, transcript, user_id=user_id))


# ==================== هندلرهای تلگرام ====================
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_user_authorized(update):
        return

    welcome_text = (
        "سلام 👋 به ربات تبدیل ویس و پاسخ‌گوی انسانی خوش آمدید\n\n"
        "🎙️ تبدیل ویس و ویدیومسیج به متن\n"
        "💬 تولید پاسخ انسانی و خلاصه پیام\n"
        "🌐 ترجمه و 🔊 تبدیل متن به ویس\n\n"
        "برای دیدن مدل و تنظیمات فعلی دستور /settings را بفرستید."
    )
    if update.message:
        await update.message.reply_text(welcome_text)


async def id_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not update.message:
        return
    await update.message.reply_text(
        f"🆔 آیدی عددی تلگرام شما:\n{user.id}\n\n"
        f"برای قفل کردن بات روی اکانت خودتان، این عدد را در GitHub Secrets با نام ALLOWED_USER_IDS ذخیره کنید."
    )


async def _download_media_bytes(bot, file_id: str) -> bytes:
    tg_file = await asyncio.wait_for(bot.get_file(file_id), timeout=30)
    audio_bytearray = await asyncio.wait_for(tg_file.download_as_bytearray(), timeout=90)
    if not audio_bytearray:
        raise RuntimeError("فایل صوتی دانلودشده خالی است.")
    if len(audio_bytearray) > MAX_TELEGRAM_FILE_SIZE_BYTES:
        raise RuntimeError("حجم فایل صوتی بیش از حد مجاز است.")
    return bytes(audio_bytearray)


async def _await_status(task: asyncio.Task, *dependent_tasks: asyncio.Task) -> Message:
    try:
        return await task
    except BaseException:
        for dependent in dependent_tasks:
            dependent.cancel()
        if dependent_tasks:
            await asyncio.gather(*dependent_tasks, return_exceptions=True)
        raise


def _prevent_duplicate_messages(handler):
    @wraps(handler)
    async def guarded(update, context):
        message = update.effective_message
        if not message or not claim_message_processing(message.chat_id, message.message_id):
            return
        started = time.perf_counter()
        try:
            await handler(update, context)
        finally:
            release_message_processing(message.chat_id, message.message_id)
            if LOG_TIMINGS:
                logger.info("handler=%s duration_ms=%.0f", handler.__name__, (time.perf_counter() - started) * 1000)
    return guarded


@_prevent_duplicate_messages
async def process_single_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_user_authorized(update):
        return

    message = update.effective_message
    user = update.effective_user
    if not message or not user:
        return

    if is_rate_limited(user.id):
        await message.reply_text("⏳ تعداد درخواست‌های شما در این دقیقه بیش از حد مجاز است. لطفاً کمی صبر کنید.")
        return

    media_obj = message.voice or message.audio or message.video_note
    if not media_obj:
        return

    file_size = getattr(media_obj, "file_size", None)
    if file_size and file_size > MAX_TELEGRAM_FILE_SIZE_BYTES:
        await message.reply_text(
            "⚠️ حجم این فایل صوتی/ویدیویی بیش از ۲۰ مگابایت است و تلگرام اجازه دانلود مستقیم آن توسط بات را نمی‌دهد."
        )
        return

    is_video_note = message.video_note is not None
    mime_type = "video/mp4" if is_video_note else (getattr(media_obj, "mime_type", None) or "audio/ogg")

    # اجرای موازی ارسال پیام وضعیت تلگرام و دانلود فایل صوتی در یک لحظه (حذف تأخیر سریالی)
    status_msg_task = asyncio.create_task(
        message.reply_text(
            "⚡ در حال تبدیل فوق‌سریع ویس...",
            reply_to_message_id=message.message_id,
        )
    )
    user_cfg = await asyncio.to_thread(get_user_model_settings, user.id)
    transcribe_model = user_cfg.get("transcribe_model") or TRANSCRIBE_MODEL
    unique_file_id = getattr(media_obj, "file_unique_id", None)
    cache_task = asyncio.create_task(asyncio.to_thread(
        get_cached_transcript, user.id, unique_file_id, transcribe_model
    )) if unique_file_id else None
    try:
        transcript = await cache_task if cache_task else None
        if not transcript:
            download_task = asyncio.create_task(_download_media_bytes(context.bot, media_obj.file_id))
            status_msg = await _await_status(status_msg_task, download_task)
            raw_audio_bytes = await download_task
            opt_bytes, opt_mime = await optimize_media_for_fast_upload(
                raw_audio_bytes, mime_type=mime_type, is_video_note=is_video_note
            )
            transcript = await transcribe_audio_bytes(
                opt_bytes, mime_type=opt_mime, preferred_model=transcribe_model
            )
            if unique_file_id:
                await asyncio.to_thread(save_cached_transcript, user.id, unique_file_id, transcribe_model, transcript)
        else:
            status_msg = await status_msg_task

        # ذخیره سریع و دریافت record_id
        record_id = await asyncio.to_thread(save_record,
            chat_id=message.chat_id,
            msg_id=message.message_id,
            bot_msg_id=status_msg.message_id,
            transcript=transcript,
            user_id=user.id,
        )

        # استارت پیش‌تولید جواب انسانی و خلاصه در پس‌زمینه برای پاسخ آنی به کلیک دکمه‌ها
        trigger_background_precomputation(record_id, transcript, user_id=user.id)

        await safe_edit_or_send_long_text(
            status_msg=status_msg,
            origin_msg=message,
            full_text=transcript,
            reply_markup=build_voice_action_keyboard(record_id),
            header_prefix="📝 متن ویس:\n\n",
            footer_suffix="\n\n━━━━━━━━━━━━━━\nمی‌خوای خلاصه‌شو بهت بدم یا جوابِ انسانی و تلگرامیش رو برات بنویسم؟ 👇",
        )

    except Exception as e:
        logger.error(f"Error processing voice message {message.message_id}: {sanitize_error_message(e)}")
        if not status_msg_task.done():
            status_msg_task.cancel()
        result = await asyncio.gather(status_msg_task, return_exceptions=True)
        if result and not isinstance(result[0], BaseException):
            await result[0].edit_text(
                f"❌ متأسفانه در پردازش این ویس خطایی رخ داد:\n{sanitize_error_message(e)}"
            )


_RE_CLEAN_VOICE_HEADER = re.compile(
    r"^📝\s*\*{0,2}متن ویس(?:\s*\(بخش\s*[0-9۰-۹]+(?:\s*از\s*[0-9۰-۹]+)?\))?:?\*{0,2}\s*"
)
_RE_CLEAN_SUM_HEADER = re.compile(r"^📌\s*\*{0,2}خلاصه:?\*{0,2}\s*")


def _extract_clean_base_text(raw_text: str) -> str:
    cleaned = raw_text
    if "━━━━━━━━━━━━━━" in cleaned:
        cleaned = cleaned.split("━━━━━━━━━━━━━━")[0]
    cleaned = _RE_CLEAN_VOICE_HEADER.sub("", cleaned.strip())
    cleaned = _RE_CLEAN_SUM_HEADER.sub("", cleaned.strip())
    return cleaned.strip()


@_prevent_duplicate_messages
async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_user_authorized(update):
        return

    message = update.effective_message
    user = update.effective_user
    if not message or not user:
        return

    incoming_text = (message.text or message.caption or "").strip()
    if not incoming_text:
        return

    if is_advertising_or_spam(incoming_text, user.id):
        logger.warning(f"Blocked spam/advertising message from user_id={user.id}")
        return

    if is_rate_limited(user.id):
        await message.reply_text("⏳ تعداد درخواست‌های شما در این دقیقه بیش از حد مجاز است. لطفاً کمی صبر کنید.")
        return

    is_forwarded = getattr(message, "forward_origin", None) is not None or getattr(message, "forward_date", None) is not None

    # حالت ۱: ریپلای دستور سفارشی روی پیام یا ویس قبلی
    if message.reply_to_message and not is_forwarded:
        replied_msg = message.reply_to_message
        db_rec = await asyncio.to_thread(get_record_by_msg_id, message.chat_id, replied_msg.message_id)
        if db_rec and not can_access_record(db_rec, message.chat_id, user.id):
            await message.reply_text("⛔ دسترسی به این پیام مجاز نیست.")
            return
        if db_rec and db_rec.get("transcript"):
            base_text = db_rec["transcript"]
        else:
            raw_replied = replied_msg.text or replied_msg.caption or ""
            base_text = _extract_clean_base_text(raw_replied)

        if base_text:
            custom_hint = (
                f"کاربر می‌خواهد در جوابِ این پیام/ویس، این نکات یا تصمیم‌ها را بگوید: «{incoming_text}». "
                f"دقیقاً همین موارد را با لحن طبیعی، خونسرد، بدون تملق و بدون هیچ علامت نگارشی و نیم‌فاصله کتابی تبدیل به پیام تلگرامی کن."
            )
            # اجرای موازی ارسال پیام انتظار و تولید پاسخ هوش مصنوعی در یک لحظه!
            wait_msg_task = asyncio.create_task(
                message.reply_text(
                    "⚡ در حال نوشتن جواب تلگرامی...",
                    reply_to_message_id=message.message_id,
                )
            )
            reply_task = asyncio.create_task(
                generate_human_telegram_reply(base_text, variation_hint=custom_hint, user_id=user.id)
            )
            wait_msg = await _await_status(wait_msg_task, reply_task)
            try:
                human_reply = await reply_task
                rec_id = await asyncio.to_thread(save_record,
                    chat_id=message.chat_id,
                    msg_id=message.message_id,
                    bot_msg_id=wait_msg.message_id,
                    transcript=f"پیام مخاطب:\n{base_text}\n\nنکات پاسخ:\n{incoming_text}",
                    human_reply=human_reply,
                    user_id=user.id,
                )
                await safe_edit_or_send_long_text(
                    status_msg=wait_msg,
                    origin_msg=message,
                    full_text=human_reply,
                    reply_markup=build_reply_keyboard(rec_id),
                )
                return
            except Exception as e:
                await wait_msg.edit_text(f"❌ خطا: {sanitize_error_message(e)}")
                return

    # حالت ۲: پیام متنی فورواردشده یا مستقیم -> اجرای موازی پیام وضعیت و تولید پاسخ
    wait_msg_task = asyncio.create_task(
        message.reply_text(
            "⚡ در حال نوشتن جواب انسانی...",
            reply_to_message_id=message.message_id,
        )
    )
    reply_task = asyncio.create_task(generate_human_telegram_reply(incoming_text, user_id=user.id))
    wait_msg = await _await_status(wait_msg_task, reply_task)

    try:
        human_reply = await reply_task
        record_id = await asyncio.to_thread(save_record,
            chat_id=message.chat_id,
            msg_id=message.message_id,
            bot_msg_id=wait_msg.message_id,
            transcript=incoming_text,
            human_reply=human_reply,
            user_id=user.id,
        )
        await safe_edit_or_send_long_text(
            status_msg=wait_msg,
            origin_msg=message,
            full_text=human_reply,
            reply_markup=build_reply_keyboard(record_id),
        )
    except Exception as e:
        logger.error(f"Error handling text message {message.message_id}: {sanitize_error_message(e)}")
        await wait_msg.edit_text(f"❌ خطا در تولید پاسخ: {sanitize_error_message(e)}")


async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_user_authorized(update):
        return
    user = update.effective_user
    if not user or not update.message:
        return

    s = get_user_model_settings(user.id)
    text = (
        "⚙️ **تنظیمات موتورهای پیشرفته هوش مصنوعی Gemini**\n\n"
        f"🔹 **مدل فعال چت/پاسخ:** `{s.get('chat_model')}`\n"
        f"🔹 **سطح تفکر (Thinking Level):** `{s.get('thinking_level')}`\n"
        f"🔹 **مدل تبدیل ویس (Speech-to-Text):** `{s.get('transcribe_model')}`\n"
        f"🔹 **مدل تولید ویس (TTS):** `{TTS_MODEL}`\n\n"
        "برای تغییر مدل یا سرعت تفکر، روی دکمه‌های زیر کلیک کنید:"
    )
    await update.message.reply_text(
        text,
        reply_markup=build_settings_keyboard(user.id),
        parse_mode="Markdown",
    )


async def button_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_user_authorized(update):
        if update.callback_query:
            await update.callback_query.answer("⛔ شما مجوز استفاده از این بات را ندارید.", show_alert=True)
        return

    query = update.callback_query
    user = update.effective_user
    if not query or not query.data or not query.message or not user:
        return

    # ۱. مدیریت پنجره تنظیمات هوش مصنوعی
    if query.data == "open_settings":
        s = get_user_model_settings(user.id)
        text = (
            "⚙️ **تنظیمات موتورهای هوش مصنوعی**\n\n"
            f"🔹 **مدل فعال چت/پاسخ:** `{s.get('chat_model')}`\n"
            f"🔹 **سطح تفکر (Thinking Level):** `{s.get('thinking_level')}`\n"
            f"🔹 **مدل تبدیل ویس:** `{s.get('transcribe_model')}`\n"
            f"🔹 **مدل صدا (TTS):** `{TTS_MODEL}`\n\n"
            "روی هر گزینه برای تغییر وضعیت بزنید:"
        )
        await query.message.reply_text(
            text,
            reply_markup=build_settings_keyboard(user.id),
            parse_mode="Markdown",
        )
        await query.answer()
        return

    if query.data == "close_settings":
        try:
            await query.message.delete()
        except Exception:
            await query.message.edit_text("تنظیمات بسته شد.")
        await query.answer("بسته شد.")
        return

    if query.data.startswith("set_cfg:"):
        try:
            _, target, mode = query.data.split(":", 2)
        except ValueError:
            await query.answer("⚠️ تنظیمات نامعتبر است.", show_alert=True)
            return
        if (target, mode) not in {("chat", "flash_high"), ("chat", "flash_turbo"),
                                  ("chat", "pro"), ("transcribe", "toggle")}:
            await query.answer("⚠️ تنظیمات نامعتبر است.", show_alert=True)
            return
        if target == "chat":
            if mode == "flash_high":
                update_user_setting(user.id, "chat_model", "gemini-3.8-flash")
                update_user_setting(user.id, "thinking_level", "HIGH")
                await query.answer("✅ Gemini 3.8 Flash (تفکر عمیق High) انتخاب شد!", show_alert=False)
            elif mode == "flash_turbo":
                update_user_setting(user.id, "chat_model", "gemini-3.8-flash")
                update_user_setting(user.id, "thinking_level", "OFF")
                await query.answer("🚀 Gemini 3.8 Flash (توربو بدون تفکر) انتخاب شد!", show_alert=False)
            elif mode == "pro":
                update_user_setting(user.id, "chat_model", "gemini-3.1-pro-preview")
                update_user_setting(user.id, "thinking_level", "HIGH")
                await query.answer("🧠 Gemini 3.1 Pro Preview (نهایت استدلال) انتخاب شد!", show_alert=False)
        elif target == "transcribe" and mode == "toggle":
            curr = get_user_model_settings(user.id).get("transcribe_model")
            new_m = "gemini-3.8-flash" if curr == "gemini-3.5-transcribe" else "gemini-3.5-transcribe"
            update_user_setting(user.id, "transcribe_model", new_m)
            await query.answer(f"🎙️ مدل ویس تغییر کرد به:\n{new_m}", show_alert=False)

        s = get_user_model_settings(user.id)
        updated_text = (
            "⚙️ **تنظیمات موتورهای هوش مصنوعی**\n\n"
            f"🔹 **مدل فعال چت/پاسخ:** `{s.get('chat_model')}`\n"
            f"🔹 **سطح تفکر (Thinking Level):** `{s.get('thinking_level')}`\n"
            f"🔹 **مدل تبدیل ویس:** `{s.get('transcribe_model')}`\n"
            f"🔹 **مدل صدا (TTS):** `{TTS_MODEL}`\n\n"
            "روی هر گزینه برای تغییر وضعیت بزنید:"
        )
        try:
            await query.message.edit_text(
                updated_text,
                reply_markup=build_settings_keyboard(user.id),
                parse_mode="Markdown",
            )
        except Exception:
            pass
        return

    if is_rate_limited(user.id):
        await query.answer("⏳ لطفاً چند ثانیه صبر کنید و دوباره امتحان کنید.", show_alert=True)
        return

    try:
        action, record_id_str = query.data.split(":", 1)
        record_id = int(record_id_str)
    except ValueError:
        await query.answer("⚠️ دکمه نامعتبر است.", show_alert=True)
        return

    record = await asyncio.to_thread(get_record, record_id)
    if not can_access_record(record, query.message.chat_id, user.id):
        await query.answer("⚠️ اطلاعات این پیام یافت نشد.", show_alert=True)
        return

    transcript = record["transcript"]
    voice_msg_id = record["voice_msg_id"]

    if action == "sum":
        if record.get("summary"):
            await query.answer("⚡ خلاصه آماده است!")
            await send_long_text(query.message, record["summary"], voice_msg_id, header_prefix="📌 خلاصه:\n\n")
            return

        await query.answer("⚡ در حال خلاصه‌سازی...")
        wait_msg_task = asyncio.create_task(
            query.message.reply_text(
                "⚡ در حال خلاصه‌سازی سریع...",
                reply_to_message_id=voice_msg_id,
            )
        )
        summary_task = _get_or_start_task(_INFLIGHT_SUMMARY_TASKS, record_id,
            lambda: _precompute_summary_for_record(record_id, transcript, user_id=user.id))
        wait_msg = await wait_msg_task
        try:
            summary = await asyncio.shield(summary_task)
            await safe_edit_or_send_long_text(
                status_msg=wait_msg,
                origin_msg=query.message,
                full_text=summary,
                header_prefix="📌 خلاصه:\n\n",
            )
        except Exception as e:
            await wait_msg.edit_text(f"❌ خطا در خلاصه‌سازی: {sanitize_error_message(e)}")

    elif action == "reply":
        if record.get("human_reply"):
            await query.answer("⚡ جواب آماده است!")
            sent = await send_long_text(query.message, record["human_reply"], voice_msg_id,
                                        reply_markup=build_reply_keyboard(record_id))
            await asyncio.to_thread(update_record_field, record_id, "bot_msg_id", sent.message_id)
            return

        await query.answer("⚡ در حال آماده‌سازی جواب...")
        wait_msg_task = asyncio.create_task(
            query.message.reply_text(
                "⚡ در حال نوشتن جواب انسانی...",
                reply_to_message_id=voice_msg_id,
            )
        )
        reply_task = _get_or_start_task(_INFLIGHT_REPLY_TASKS, record_id,
            lambda: _precompute_reply_for_record(record_id, transcript, user_id=user.id))
        wait_msg = await wait_msg_task
        try:
            human_reply = await asyncio.shield(reply_task)
            await asyncio.to_thread(update_record_field, record_id, "bot_msg_id", wait_msg.message_id)
            await safe_edit_or_send_long_text(
                status_msg=wait_msg,
                origin_msg=query.message,
                full_text=human_reply,
                reply_markup=build_reply_keyboard(record_id),
            )
        except Exception as e:
            await wait_msg.edit_text(f"❌ خطا در تولید جواب انسانی: {sanitize_error_message(e)}")

    elif action == "trans":
        base_for_trans = displayed_record_text(query.message, record)
        settings = await asyncio.to_thread(get_user_model_settings, user.id)
        source_hash = translation_cache_key(record_id, base_for_trans, settings)
        cached_translation = await asyncio.to_thread(get_cached_translation, record_id, source_hash)
        if cached_translation:
            await query.answer("⚡ ترجمه آماده است!")
            await send_long_text(query.message, cached_translation, voice_msg_id, header_prefix="🌐 ترجمه:\n\n")
            return

        await query.answer("🌐 در حال ترجمه هوشمند...")
        wait_msg_task = asyncio.create_task(
            query.message.reply_text(
                "🌐 در حال ترجمه هوشمند با Gemini...",
                reply_to_message_id=voice_msg_id,
            )
        )
        async def translate_and_save():
            translated = await translate_text_content(base_for_trans, user_id=user.id)
            await asyncio.to_thread(save_cached_translation, record_id, source_hash, translated)
            return translated
        trans_task = _get_or_start_task(_INFLIGHT_TRANSLATION_TASKS, source_hash, translate_and_save)
        wait_msg = await wait_msg_task
        try:
            translated = await asyncio.shield(trans_task)
            await safe_edit_or_send_long_text(
                status_msg=wait_msg,
                origin_msg=query.message,
                full_text=translated,
                header_prefix="🌐 ترجمه:\n\n",
            )
        except Exception as e:
            await wait_msg.edit_text(f"❌ خطا در ترجمه: {sanitize_error_message(e)}")

    elif action == "tts":
        text_to_speak = displayed_record_text(query.message, record)
        await query.answer("🔊 در حال تولید ویس صوتی...")
        wait_msg_task = asyncio.create_task(
            query.message.reply_text(
                "🔊 در حال تولید فایل صوتی...",
                reply_to_message_id=voice_msg_id,
            )
        )
        tts_task = asyncio.create_task(generate_speech_audio(text_to_speak))
        wait_msg = await wait_msg_task
        try:
            ogg_bytes = await tts_task
            await query.message.reply_voice(
                voice=io.BytesIO(ogg_bytes),
                reply_to_message_id=voice_msg_id,
                caption="🔊 ویس تولیدشده با هوش مصنوعی",
            )
            await wait_msg.delete()
        except Exception as e:
            logger.error(f"TTS error: {sanitize_error_message(e)}")
            await wait_msg.edit_text(f"❌ خطا در تولید ویس صوتی: {sanitize_error_message(e)}")

    elif action == "regen":
        await query.answer("🔄 در حال نوشتن یک مدل جدید...")
        hint = (
            "یک مدل جواب متفاوت‌تر، راحت‌تر و خونسردتر نسبت به حالت قبلی بنویس "
            "که باز هم صددرصد طبیعی، بدون هیچ علامت نگارشی، بدون نیم‌فاصله کتابی و بدون تعارف باشه."
        )
        wait_msg_task = asyncio.create_task(
            query.message.reply_text(
                "⚡ در حال نوشتن مدل جدید...",
                reply_to_message_id=voice_msg_id,
            )
        )
        reply_task = asyncio.create_task(
            generate_human_telegram_reply(transcript, variation_hint=hint, user_id=user.id)
        )
        wait_msg = await wait_msg_task
        try:
            human_reply = await reply_task
            await asyncio.to_thread(update_record_field, record_id, "human_reply", human_reply)
            await asyncio.to_thread(update_record_field, record_id, "bot_msg_id", wait_msg.message_id)
            await safe_edit_or_send_long_text(
                status_msg=wait_msg,
                origin_msg=query.message,
                full_text=human_reply,
                reply_markup=build_reply_keyboard(record_id),
            )
        except Exception as e:
            await wait_msg.edit_text(f"❌ خطا در تولید جواب انسانی: {sanitize_error_message(e)}")
    else:
        await query.answer("⚠️ دکمه نامعتبر است.", show_alert=True)


async def shutdown_resources(application) -> None:
    global _genai_client
    tasks = set(_INFLIGHT_REPLY_TASKS.values()) | set(_INFLIGHT_SUMMARY_TASKS.values()) | set(_INFLIGHT_TRANSLATION_TASKS.values())
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    if _genai_client is not None:
        with suppress(Exception):
            await _genai_client.aio.aclose()
        with suppress(Exception):
            _genai_client.close()
        _genai_client = None
    await asyncio.to_thread(close_db)


def main() -> None:
    if not BOT_TOKEN or not GEMINI_API_KEY:
        logger.error("❌ کلیدهای ارتباطی یافت نشدند.")
        sys.exit(1)

    if RAW_ALLOWED_IDS and any(not item.strip().isdigit() for item in RAW_ALLOWED_IDS.split(",")):
        logger.error("ALLOWED_USER_IDS باید فقط شامل شناسه‌های عددی جداشده با ویرگول باشد.")
        sys.exit(1)

    init_db()
    # گرم کردن کلاینت هوش مصنوعی در بدو شروع تا اولین پیام هیچ تأخیر اولیه‌ای نداشته باشد
    try:
        get_genai_client()
    except Exception:
        pass

    logger.info("Starting @voiceandpasokhbot (Gemini 3.x Power Edition)...")

    # تنظیم استخر کانکشن بزرگ و HTTP/2 برای حذف تأخیرهای شبکه در ارتباط با سرور تلگرام
    try:
        t_request = HTTPXRequest(
            connection_pool_size=64,
            read_timeout=25.0,
            write_timeout=25.0,
            connect_timeout=10.0,
            pool_timeout=10.0,
            http_version="2",
        )
        app = (
            ApplicationBuilder()
            .token(BOT_TOKEN)
            .request(t_request)
            .get_updates_request(
                HTTPXRequest(
                    connection_pool_size=16,
                    read_timeout=35.0,
                    connect_timeout=10.0,
                    pool_timeout=10.0,
                    http_version="2",
                )
            )
            .concurrent_updates(32)
            .post_shutdown(shutdown_resources)
            .build()
        )
    except Exception:
        app = (
            ApplicationBuilder()
            .token(BOT_TOKEN)
            .concurrent_updates(32)
            .post_shutdown(shutdown_resources)
            .build()
        )

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", start_command))
    app.add_handler(CommandHandler("id", id_command))
    app.add_handler(CommandHandler("settings", settings_command))
    app.add_handler(CommandHandler("model", settings_command))
    app.add_handler(CommandHandler("models", settings_command))
    app.add_handler(CommandHandler("mode", settings_command))

    app.add_handler(
        MessageHandler(
            filters.VOICE | filters.AUDIO | filters.VIDEO_NOTE,
            process_single_voice,
        )
    )

    app.add_handler(CallbackQueryHandler(button_callback_handler))

    app.add_handler(
        MessageHandler(
            (filters.TEXT | filters.CAPTION) & (~filters.COMMAND) & (~filters.VOICE) & (~filters.AUDIO) & (~filters.VIDEO_NOTE),
            handle_text_message,
        )
    )

    app.run_polling(
        poll_interval=0.1,
        timeout=25,
        drop_pending_updates=False,
        allowed_updates=Update.ALL_TYPES,
    )


if __name__ == "__main__":
    main()
