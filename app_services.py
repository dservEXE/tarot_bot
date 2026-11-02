"""Consent and optional AI enrichment. Personal selection remains in horoscope.py."""
import asyncio
import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone

import aiohttp
import aiosqlite

import db
from config import get_settings
from horoscope import generate_daily_reading, profile_signature

CONSENT_VERSION = '2026-09-08'
AI_FIELDS = ('energy', 'challenge', 'opportunity', 'love', 'work', 'money', 'advice', 'ritual', 'mantra')
_generation_lock = asyncio.Semaphore(3)
_cache_locks: dict[str, asyncio.Lock] = {}


async def init_app() -> None:
    async with aiosqlite.connect(db.DB_NAME) as conn:
        await conn.executescript('''
        CREATE TABLE IF NOT EXISTS user_consents (
            user_id INTEGER PRIMARY KEY, version TEXT NOT NULL, accepted_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS reading_cache (
            cache_key TEXT PRIMARY KEY, content TEXT NOT NULL, created_at TEXT NOT NULL
        );
        ''')
        cutoff = (datetime.now(timezone.utc) - timedelta(days=35)).isoformat()
        await conn.execute('DELETE FROM reading_cache WHERE created_at < ?', (cutoff,))
        await conn.commit()


async def has_consent(user_id: int) -> bool:
    async with aiosqlite.connect(db.DB_NAME) as conn:
        row = await (await conn.execute('SELECT version FROM user_consents WHERE user_id=?', (user_id,))).fetchone()
        return bool(row and row[0] == CONSENT_VERSION)


async def accept_consent(user_id: int) -> None:
    async with aiosqlite.connect(db.DB_NAME) as conn:
        await conn.execute('INSERT OR REPLACE INTO user_consents VALUES (?,?,?)',
                           (user_id, CONSENT_VERSION, datetime.now(timezone.utc).isoformat()))
        await conn.commit()


def extract_ai_fields(response: dict) -> dict[str, str]:
    if response.get('status') != 'completed':
        raise ValueError('Incomplete AI response')
    output = ''.join(part.get('text', '') for item in response.get('output', [])
                     if item.get('type') == 'message' for part in item.get('content', [])
                     if part.get('type') == 'output_text')
    value = json.loads(output)
    if set(value) != set(AI_FIELDS):
        raise ValueError('Unexpected AI schema')
    if any(not isinstance(text, str) or not 10 <= len(text) <= 180 for text in value.values()):
        raise ValueError('Unexpected AI field length')
    return value


async def get_daily_reading(user, reading_date, *, enrich: bool = True) -> dict:
    base = generate_daily_reading(user, reading_date)
    base['generation_source'] = 'algorithm'
    settings = get_settings()
    if not enrich or not settings.ai_enabled:
        return base
    key = hashlib.sha256(f'{profile_signature(user)}|{reading_date}|{settings.openai_model}|reading-v1'.encode()).hexdigest()
    # Bound lock memory; active locks remain referenced by their callers.
    if len(_cache_locks) > 1024:
        for old in [k for k, lock in _cache_locks.items() if not lock.locked()][:512]:
            _cache_locks.pop(old, None)
    lock = _cache_locks.setdefault(key, asyncio.Lock())
    async with lock:
        async with aiosqlite.connect(db.DB_NAME) as conn:
            row = await (await conn.execute('SELECT content FROM reading_cache WHERE cache_key=?', (key,))).fetchone()
        if row:
            base.update(json.loads(row[0]))
            base['generation_source'] = 'ai'
            return base
        # Only derived symbolism is sent. No name, Telegram ID, birthday or birth place.
        context = {field: base[field] for field in ('card', 'theme', 'day_arcana_label', 'personal_arcana_label', 'zodiac_element')}
        context['selected_interpretation'] = {field: base[field] for field in AI_FIELDS}
        schema = {'type': 'object', 'properties': {field: {'type': 'string'} for field in AI_FIELDS},
                  'required': list(AI_FIELDS), 'additionalProperties': False}
        payload = {
            'model': settings.openai_model, 'store': False, 'max_output_tokens': 1800,
            'instructions': (
                'Ты редактор персонального символического расклада на русском языке. '
                'Сохраняй выбранную карту, тему и исходный смысл каждой области. '
                'Раскрой выбранные значения живо, спокойно, конкретно и без повторов. '
                'Каждое поле: одно-два коротких предложения, 10–180 символов. '
                'Обращайся на ты. Никакого HTML/Markdown. Не выдумывай положения планет, '
                'аспекты, транзиты и сведения о человеке. Это рефлексия, а не предсказание фактов. '
                'Без диагнозов, обещаний денег, запугивания, неизбежных событий и вредных ритуалов.'
            ),
            'input': json.dumps(context, ensure_ascii=False),
            'text': {'format': {'type': 'json_schema', 'name': 'daily_reading', 'strict': True, 'schema': schema}},
        }
        try:
            async with _generation_lock:
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=35)) as session:
                    async with session.post('https://api.openai.com/v1/responses', json=payload,
                                            headers={'Authorization': f'Bearer {settings.openai_api_key}'}) as response:
                        if response.status != 200:
                            raise ValueError(f'AI HTTP {response.status}')
                        fields = extract_ai_fields(await response.json())
            async with aiosqlite.connect(db.DB_NAME) as conn:
                await conn.execute('INSERT OR IGNORE INTO reading_cache VALUES (?,?,?)',
                                   (key, json.dumps(fields, ensure_ascii=False), datetime.now(timezone.utc).isoformat()))
                await conn.commit()
            base.update(fields)
            base['generation_source'] = 'ai'
        except (aiohttp.ClientError, TimeoutError, ValueError, KeyError, TypeError):
            logging.warning('AI enrichment unavailable; original reading retained')
        return base
