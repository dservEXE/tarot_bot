import os
import uuid
from pathlib import Path
from datetime import datetime, timezone

import aiosqlite

_configured_db = Path(os.environ.get("DB_PATH") or os.environ.get("DB_NAME") or "users.db").expanduser()
DB_NAME = str(_configured_db if _configured_db.is_absolute() else Path(__file__).resolve().parent / _configured_db)


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def normalize_username(username: str | None) -> str | None:
    if not username:
        return None
    username = username.strip().lstrip("@").lower()
    return username or None


async def _get_columns(db: aiosqlite.Connection, table: str) -> set[str]:
    cursor = await db.execute(f"PRAGMA table_info({table})")
    rows = await cursor.fetchall()
    return {row[1] for row in rows}


async def _ensure_column(db: aiosqlite.Connection, table: str, column: str, ddl: str) -> None:
    columns = await _get_columns(db, table)
    if column not in columns:
        await db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


async def init_db() -> None:
    Path(DB_NAME).parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA busy_timeout=10000")
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                telegram_id INTEGER PRIMARY KEY,
                username TEXT,
                name TEXT NOT NULL,
                birth_date TEXT,
                birth_time TEXT,
                birth_place TEXT,
                timezone TEXT NOT NULL DEFAULT 'Europe/Moscow',
                zodiac TEXT,
                personal_arcana INTEGER,
                tokens INTEGER NOT NULL DEFAULT 0,
                referrer_id INTEGER,
                notify_time TEXT,
                notify_enabled INTEGER NOT NULL DEFAULT 1,
                subscription_until TEXT,
                paid_full_date TEXT,
                paid_match_date TEXT,
                matrix_access INTEGER NOT NULL DEFAULT 0,
                matrix_paid_at TEXT,
                last_daily_sent_date TEXT,
                created_at TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT ''
            )
            """
        )

        # migrations for old DBs
        await _ensure_column(db, "users", "username", "TEXT")
        await _ensure_column(db, "users", "birth_time", "TEXT")
        await _ensure_column(db, "users", "birth_place", "TEXT")
        await _ensure_column(db, "users", "timezone", "TEXT NOT NULL DEFAULT 'Europe/Moscow'")
        await _ensure_column(db, "users", "zodiac", "TEXT")
        await _ensure_column(db, "users", "personal_arcana", "INTEGER")
        await _ensure_column(db, "users", "tokens", "INTEGER NOT NULL DEFAULT 0")
        await _ensure_column(db, "users", "referrer_id", "INTEGER")
        await _ensure_column(db, "users", "notify_time", "TEXT")
        await _ensure_column(db, "users", "notify_enabled", "INTEGER NOT NULL DEFAULT 1")
        await _ensure_column(db, "users", "subscription_until", "TEXT")
        await _ensure_column(db, "users", "paid_full_date", "TEXT")
        await _ensure_column(db, "users", "paid_match_date", "TEXT")
        await _ensure_column(db, "users", "matrix_access", "INTEGER NOT NULL DEFAULT 0")
        await _ensure_column(db, "users", "matrix_paid_at", "TEXT")
        await _ensure_column(db, "users", "last_daily_sent_date", "TEXT")
        await _ensure_column(db, "users", "created_at", "TEXT NOT NULL DEFAULT ''")
        await _ensure_column(db, "users", "updated_at", "TEXT NOT NULL DEFAULT ''")

        await db.execute("""CREATE TABLE IF NOT EXISTS token_transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            request_id TEXT NOT NULL UNIQUE,
            user_id INTEGER NOT NULL,
            delta INTEGER NOT NULL,
            balance_after INTEGER NOT NULL,
            kind TEXT NOT NULL,
            reason TEXT NOT NULL,
            actor_id INTEGER,
            created_at TEXT NOT NULL
        )""")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_ledger_user ON token_transactions(user_id, id DESC)")

        await db.commit()


async def get_user(telegram_id: int):
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM users WHERE telegram_id = ?",
            (telegram_id,),
        ) as cursor:
            return await cursor.fetchone()


async def get_user_by_username(username: str):
    username = normalize_username(username)
    if not username:
        return None

    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM users WHERE lower(username) = ?",
            (username,),
        ) as cursor:
            return await cursor.fetchone()


async def delete_user_data(telegram_id: int) -> dict:
    """Delete all user-linked records in one transaction.

    Payment and token journal rows are removed at the user's explicit deletion
    request; this operation is reserved for the authenticated admin panel.
    Reading cache has no user id, so it is cleared as a conservative privacy
    measure rather than attempting to infer ownership from a hash.
    """
    uid = int(telegram_id)
    if uid <= 0:
        raise ValueError("Некорректный Telegram ID")
    async with aiosqlite.connect(DB_NAME, timeout=10) as connection:
        await connection.execute("PRAGMA foreign_keys=ON")
        await connection.execute("PRAGMA busy_timeout=10000")
        await connection.execute("BEGIN IMMEDIATE")
        try:
            exists = await (await connection.execute(
                "SELECT 1 FROM users WHERE telegram_id=?", (uid,))).fetchone()
            if exists is None:
                await connection.rollback()
                return {"deleted": False, "user_id": uid, "counts": {}}
            tables = {row[0] for row in await (await connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'" )).fetchall()}
            counts = {}
            if "payment_orders" in tables:
                counts["payment_orders"] = (await (await connection.execute(
                    "SELECT COUNT(*) FROM payment_orders WHERE user_id=?", (uid,))).fetchone())[0]
            if "refund_attempts" in tables and "payment_orders" in tables:
                counts["refund_attempts"] = (await (await connection.execute(
                    "SELECT COUNT(*) FROM refund_attempts WHERE order_id IN "
                    "(SELECT order_id FROM payment_orders WHERE user_id=?)", (uid,))).fetchone())[0]
            for table, column in (("token_transactions", "user_id"), ("product_entitlements", "user_id"),
                                  ("user_consents", "user_id")):
                if table in tables:
                    counts[table] = (await (await connection.execute(
                        f"SELECT COUNT(*) FROM {table} WHERE {column}=?", (uid,))).fetchone())[0]
            if "reading_cache" in tables:
                counts["reading_cache"] = (await (await connection.execute(
                    "SELECT COUNT(*) FROM reading_cache")).fetchone())[0]

            await connection.execute("UPDATE users SET referrer_id=NULL WHERE referrer_id=?", (uid,))
            if "refund_attempts" in tables and "payment_orders" in tables:
                await connection.execute(
                    "DELETE FROM refund_attempts WHERE order_id IN "
                    "(SELECT order_id FROM payment_orders WHERE user_id=?)", (uid,))
            if "payment_orders" in tables:
                await connection.execute("DELETE FROM payment_orders WHERE user_id=?", (uid,))
            for table, column in (("token_transactions", "user_id"), ("product_entitlements", "user_id"),
                                  ("user_consents", "user_id")):
                if table in tables:
                    await connection.execute(f"DELETE FROM {table} WHERE {column}=?", (uid,))
            if "reading_cache" in tables:
                await connection.execute("DELETE FROM reading_cache")
            await connection.execute("DELETE FROM users WHERE telegram_id=?", (uid,))
            await connection.commit()
            return {"deleted": True, "user_id": uid, "counts": counts}
        except BaseException:
            await connection.rollback()
            raise


async def get_all_users():
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users ORDER BY telegram_id DESC") as cursor:
            rows = await cursor.fetchall()
            return rows


async def upsert_user(
    telegram_id: int,
    name: str,
    username: str | None = None,
    referrer_id: int | None = None,
) -> bool:
    username = normalize_username(username)
    ts = now_iso()

    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("BEGIN IMMEDIATE")
        cursor = await db.execute("SELECT 1 FROM users WHERE telegram_id = ?", (telegram_id,))
        if await cursor.fetchone() is None:
            await db.execute(
                """
                INSERT INTO users (
                    telegram_id,
                    username,
                    name,
                    tokens,
                    referrer_id,
                    notify_enabled,
                    matrix_access,
                    created_at,
                    updated_at
                ) VALUES (?, ?, ?, 0, ?, 1, 0, ?, ?)
                """,
                (telegram_id, username, name, referrer_id, ts, ts),
            )
            await db.commit()
            return True

        await db.execute(
            """
            UPDATE users
            SET name = ?,
                username = COALESCE(?, username),
                updated_at = ?
            WHERE telegram_id = ?
            """,
            (name, username, ts, telegram_id),
        )
        await db.commit()
        return False


async def set_zodiac(telegram_id: int, zodiac: str) -> None:
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            """
            UPDATE users
            SET zodiac = ?, updated_at = ?
            WHERE telegram_id = ?
            """,
            (zodiac, now_iso(), telegram_id),
        )
        await db.commit()


async def set_personal_arcana(telegram_id: int, personal_arcana: int) -> None:
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            """
            UPDATE users
            SET personal_arcana = ?, updated_at = ?
            WHERE telegram_id = ?
            """,
            (personal_arcana, now_iso(), telegram_id),
        )
        await db.commit()


async def set_birth_date(telegram_id: int, birth_date: str) -> None:
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            """
            UPDATE users
            SET birth_date = ?, updated_at = ?
            WHERE telegram_id = ?
            """,
            (birth_date, now_iso(), telegram_id),
        )
        await db.commit()


async def set_birth_time(telegram_id: int, birth_time: str) -> None:
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            """
            UPDATE users
            SET birth_time = ?, updated_at = ?
            WHERE telegram_id = ?
            """,
            (birth_time, now_iso(), telegram_id),
        )
        await db.commit()


async def set_birth_place(telegram_id: int, birth_place: str) -> None:
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            """
            UPDATE users
            SET birth_place = ?, updated_at = ?
            WHERE telegram_id = ?
            """,
            (birth_place.strip(), now_iso(), telegram_id),
        )
        await db.commit()


async def set_timezone(telegram_id: int, timezone_name: str) -> None:
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            """
            UPDATE users
            SET timezone = ?, updated_at = ?
            WHERE telegram_id = ?
            """,
            (timezone_name, now_iso(), telegram_id),
        )
        await db.commit()


async def set_notify_time(telegram_id: int, notify_time: str) -> None:
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            """
            UPDATE users
            SET notify_time = ?, updated_at = ?
            WHERE telegram_id = ?
            """,
            (notify_time, now_iso(), telegram_id),
        )
        await db.commit()


async def set_notify_enabled(telegram_id: int, enabled: bool) -> None:
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            """
            UPDATE users
            SET notify_enabled = ?, updated_at = ?
            WHERE telegram_id = ?
            """,
            (1 if enabled else 0, now_iso(), telegram_id),
        )
        await db.commit()


async def set_subscription_until(telegram_id: int, subscription_until: str | None) -> None:
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            """
            UPDATE users
            SET subscription_until = ?, updated_at = ?
            WHERE telegram_id = ?
            """,
            (subscription_until, now_iso(), telegram_id),
        )
        await db.commit()


async def set_paid_full_date(telegram_id: int, paid_full_date: str | None) -> None:
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            """
            UPDATE users
            SET paid_full_date = ?, updated_at = ?
            WHERE telegram_id = ?
            """,
            (paid_full_date, now_iso(), telegram_id),
        )
        await db.commit()


async def set_paid_match_date(telegram_id: int, paid_match_date: str | None) -> None:
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            """
            UPDATE users
            SET paid_match_date = ?, updated_at = ?
            WHERE telegram_id = ?
            """,
            (paid_match_date, now_iso(), telegram_id),
        )
        await db.commit()


async def set_matrix_access(telegram_id: int, access: bool) -> None:
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            """
            UPDATE users
            SET matrix_access = ?, updated_at = ?
            WHERE telegram_id = ?
            """,
            (1 if access else 0, now_iso(), telegram_id),
        )
        await db.commit()


async def set_matrix_paid_at(telegram_id: int, paid_at: str | None) -> None:
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            """
            UPDATE users
            SET matrix_paid_at = ?, updated_at = ?
            WHERE telegram_id = ?
            """,
            (paid_at, now_iso(), telegram_id),
        )
        await db.commit()


async def set_last_daily_sent_date(telegram_id: int, sent_date: str) -> None:
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            """
            UPDATE users
            SET last_daily_sent_date = ?, updated_at = ?
            WHERE telegram_id = ?
            """,
            (sent_date, now_iso(), telegram_id),
        )
        await db.commit()


async def add_tokens(user_id: int, amount: int) -> None:
    if type(amount) is not int or amount <= 0:
        raise ValueError("Token credit must be a positive integer")
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("BEGIN IMMEDIATE")
        cursor = await db.execute(
            """
            UPDATE users
            SET tokens = COALESCE(tokens, 0) + ?,
                updated_at = ?
            WHERE telegram_id = ?
            """,
            (amount, now_iso(), user_id),
        )
        if cursor.rowcount != 1:
            raise ValueError("Unknown user")
        balance = await (await db.execute("SELECT tokens FROM users WHERE telegram_id = ?", (user_id,))).fetchone()
        await db.execute("INSERT INTO token_transactions(request_id,user_id,delta,balance_after,kind,reason,created_at) VALUES(?,?,?,?,?,?,?)",
                         ("credit:" + uuid.uuid4().hex, user_id, amount, balance[0], "legacy_credit", "Application credit", now_iso()))
        await db.commit()


async def spend_tokens(user_id: int, amount: int) -> bool:
    if type(amount) is not int or amount <= 0:
        raise ValueError("Token debit must be a positive integer")
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("BEGIN IMMEDIATE")
        cursor = await db.execute(
            """
            UPDATE users
            SET tokens = COALESCE(tokens, 0) - ?,
                updated_at = ?
            WHERE telegram_id = ? AND COALESCE(tokens, 0) >= ?
            """,
            (amount, now_iso(), user_id, amount),
        )
        if cursor.rowcount != 1:
            return False
        balance = await (await db.execute("SELECT tokens FROM users WHERE telegram_id = ?", (user_id,))).fetchone()
        await db.execute("INSERT INTO token_transactions(request_id,user_id,delta,balance_after,kind,reason,created_at) VALUES(?,?,?,?,?,?,?)",
                         ("debit:" + uuid.uuid4().hex, user_id, -amount, balance[0], "legacy_debit", "Application debit", now_iso()))
        await db.commit()
        return True


async def get_tokens(user_id: int) -> int:
    user = await get_user(user_id)
    if not user:
        return 0
    return int(user["tokens"] or 0)


async def get_all_users_for_notifications():
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT *
            FROM users
            WHERE COALESCE(notify_enabled, 1) = 1
              AND birth_date IS NOT NULL
              AND birth_time IS NOT NULL
              AND birth_place IS NOT NULL
              AND notify_time IS NOT NULL
            """
        ) as cursor:
            return await cursor.fetchall()


async def reset_profile(telegram_id: int) -> None:
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            """
            UPDATE users
            SET birth_date = NULL,
                birth_time = NULL,
                birth_place = NULL,
                zodiac = NULL,
                personal_arcana = NULL,
                notify_time = NULL,
                paid_full_date = NULL,
                paid_match_date = NULL,
                last_daily_sent_date = NULL,
                updated_at = ?
            WHERE telegram_id = ?
            """,
            (now_iso(), telegram_id),
        )
        await db.commit()
