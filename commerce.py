"""Payment ledger and entitlements. No network calls; trust only verified provider events.

Every balance mutation and its ledger row share one SQLite transaction. An uncertain
external refund stays pending for reconciliation; do not retry it blindly. Confirmed
refunds may create token debt if tokens were already spent. New spending is blocked
until top-ups repay that debt. Existing entitlements remain readable.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import json
from uuid import uuid4

import aiosqlite
import db as user_db


class CommerceError(ValueError):
    pass


class PaymentValidationError(CommerceError):
    pass


@dataclass(frozen=True)
class TokenPackage:
    tokens: int
    stars: int
    amount_kopecks: int
    title: str

    @property
    def standard_amount_kopecks(self) -> int:
        """The regular catalog price at one ruble per token."""
        return self.tokens * 100

    @property
    def savings_kopecks(self) -> int:
        return max(0, self.standard_amount_kopecks - self.amount_kopecks)

    @property
    def discount_percent(self) -> int:
        if not self.savings_kopecks:
            return 0
        return round(self.savings_kopecks * 100 / self.standard_amount_kopecks)


# Commercial prices, not a representation of Telegram's exchange/withdrawal rate.
# Ruble prices use the regular 1 ₽ per token rate, with a visible discount on the
# larger packs. Telegram Stars remain one Star per token because Stars have their
# own provider pricing and are not interchangeable with rubles.
PACKAGES = {
    "10": TokenPackage(10, 10, 10 * 100, "10 токенов"),
    "50": TokenPackage(50, 50, 50 * 100, "50 токенов"),
    "100": TokenPackage(100, 100, 100 * 100, "100 токенов"),
    "200": TokenPackage(200, 200, 200 * 100, "200 токенов"),
    "250": TokenPackage(250, 250, 250 * 100, "250 токенов"),
    "500": TokenPackage(500, 500, 349 * 100, "500 токенов"),
    "1000": TokenPackage(1000, 1000, 699 * 100, "1000 токенов"),
}
PRODUCT_PRICES = {"full_reading": 10, "match": 15, "subscription": 199, "matrix": 349, "matrix_report": 499}


@dataclass(frozen=True)
class PurchaseResult:
    status: str
    balance: int
    price: int
    access_key: str


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso() -> str:
    return _now().isoformat()


def _datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed
    except (ValueError, TypeError):
        return None


@asynccontextmanager
async def _connection(*, write: bool = False):
    async with aiosqlite.connect(user_db.DB_NAME, timeout=5) as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA foreign_keys=ON")
        if write:
            await conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
            if write:
                await conn.commit()
        except BaseException:
            if write:
                await conn.rollback()
            raise


async def _one(conn, sql, args=()):
    return await (await conn.execute(sql, args)).fetchone()


async def _balance(conn, user_id: int) -> int:
    row = await _one(conn, "SELECT tokens FROM users WHERE telegram_id=?", (user_id,))
    if row is None:
        raise CommerceError("Профиль не найден. Откройте /start.")
    return int(row[0] or 0)


async def _ledger(conn, user_id, delta, kind, reason, request_id, actor_id=None):
    balance = await _balance(conn, user_id)
    await conn.execute("INSERT INTO token_transactions(request_id,user_id,delta,balance_after,kind,reason,actor_id,created_at) VALUES(?,?,?,?,?,?,?,?)",
                       (request_id, user_id, delta, balance, kind, reason, actor_id, _iso()))
    return balance


async def init_commerce() -> None:
    """Run after db.init_db; non-destructive and safe to repeat."""
    async with _connection(write=True) as conn:
        await conn.execute("""CREATE TABLE IF NOT EXISTS payment_orders (
            order_id TEXT PRIMARY KEY, payload TEXT NOT NULL UNIQUE,
            user_id INTEGER NOT NULL REFERENCES users(telegram_id),
            package_id TEXT NOT NULL, provider TEXT NOT NULL,
            currency TEXT NOT NULL, amount INTEGER NOT NULL CHECK(amount>0),
            stars INTEGER NOT NULL DEFAULT 0, amount_kopecks INTEGER NOT NULL DEFAULT 0,
            tokens INTEGER NOT NULL CHECK(tokens>0), status TEXT NOT NULL DEFAULT 'pending',
            telegram_charge_id TEXT UNIQUE, provider_payment_id TEXT UNIQUE,
            provider_charge_id TEXT, confirmation_url TEXT, created_at TEXT NOT NULL, expires_at TEXT NOT NULL,
            paid_at TEXT, refunded_at TEXT, refund_attempt_id TEXT
        )""")
        await user_db._ensure_column(conn, "payment_orders", "confirmation_url", "TEXT")
        await user_db._ensure_column(conn, "payment_orders", "request_json", "TEXT")
        await user_db._ensure_column(conn, "payment_orders", "last_reconcile_at", "TEXT")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_payments_user ON payment_orders(user_id,created_at DESC)")
        await conn.execute("""CREATE TABLE IF NOT EXISTS refund_attempts (
            attempt_id TEXT PRIMARY KEY, order_id TEXT NOT NULL REFERENCES payment_orders(order_id),
            actor_id INTEGER, reason TEXT NOT NULL, status TEXT NOT NULL,
            error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        )""")
        await user_db._ensure_column(conn, "refund_attempts", "provider_refund_id", "TEXT")
        await user_db._ensure_column(conn, "refund_attempts", "request_json", "TEXT")
        await user_db._ensure_column(conn, "refund_attempts", "last_reconcile_at", "TEXT")
        await conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_provider_refund_unique ON refund_attempts(provider_refund_id)")
        await conn.execute("""CREATE TABLE IF NOT EXISTS product_entitlements (
            user_id INTEGER NOT NULL REFERENCES users(telegram_id), product TEXT NOT NULL,
            access_key TEXT NOT NULL, created_at TEXT NOT NULL, price INTEGER NOT NULL,
            PRIMARY KEY(user_id,product,access_key)
        )""")
        await conn.execute("CREATE TABLE IF NOT EXISTS commerce_migrations(name TEXT PRIMARY KEY, applied_at TEXT NOT NULL)")
        if not await _one(conn, "SELECT 1 FROM commerce_migrations WHERE name='legacy_v1'"):
            rows = await (await conn.execute("SELECT * FROM users")).fetchall()
            for row in rows:
                uid = row["telegram_id"]
                # Opening entry is the unrecorded portion, also safe after legacy helper use.
                ledger_sum = await _one(conn, "SELECT COALESCE(SUM(delta),0) FROM token_transactions WHERE user_id=?", (uid,))
                opening = int(row["tokens"] or 0) - ledger_sum[0]
                if opening:
                    await _ledger(conn, uid, opening, "opening_balance", "Баланс до платежного журнала", f"opening:{uid}")
                for product, key in (("matrix", "*" if row["matrix_access"] else None),
                                     ("full_reading", row["paid_full_date"]), ("match", row["paid_match_date"])):
                    if key:
                        await conn.execute("INSERT OR IGNORE INTO product_entitlements VALUES(?,?,?,?,0)", (uid, product, key, _iso()))
            await conn.execute("INSERT INTO commerce_migrations VALUES('legacy_v1',?)", (_iso(),))


async def _create_order(user_id: int, package_id: str, provider: str) -> dict:
    package = PACKAGES.get(str(package_id))
    if package is None:
        raise CommerceError("Такого пакета нет. Выберите пакет в меню баланса.")
    oid = uuid4().hex
    currency = "XTR" if provider == "telegram_stars" else "RUB"
    amount = package.stars if currency == "XTR" else package.amount_kopecks
    async with _connection(write=True) as conn:
        await _balance(conn, user_id)
        await conn.execute("""INSERT INTO payment_orders
            (order_id,payload,user_id,package_id,provider,currency,amount,stars,amount_kopecks,tokens,created_at,expires_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (oid, "tarot:" + oid, user_id, str(package_id), provider, currency, amount,
             amount if currency == "XTR" else 0, amount if currency == "RUB" else 0,
             package.tokens, _iso(), (_now() + timedelta(minutes=30)).isoformat()))
        return dict(await _one(conn, "SELECT * FROM payment_orders WHERE order_id=?", (oid,)))


async def create_order(user_id: int, package_id: str) -> dict:
    return await _create_order(user_id, package_id, "telegram_stars")


async def create_external_order(user_id: int, package_id: str) -> dict:
    return await _create_order(user_id, package_id, "yookassa")


def _validate(order, user_id, currency, amount, provider):
    if (order is None or order["user_id"] != user_id or order["currency"] != currency
            or type(amount) is not int or order["amount"] != amount or order["provider"] != provider):
        raise PaymentValidationError("Платёж не совпадает с заказом. Обратитесь в /paysupport.")


async def validate_pre_checkout(user_id: int, payload: str, currency: str, total_amount: int) -> dict:
    async with _connection() as conn:
        order = await _one(conn, "SELECT * FROM payment_orders WHERE payload=?", (payload,))
        _validate(order, user_id, currency, total_amount, "telegram_stars")
        if order["status"] != "pending" or _datetime(order["expires_at"]) <= _now():
            raise PaymentValidationError("Этот счёт уже закрыт или устарел. Создайте новый в разделе «Баланс».")
        return dict(order)


async def _confirm(conn, order, user_id, currency, amount, provider, charge_id, provider_charge_id=""):
    _validate(order, user_id, currency, amount, provider)
    if not isinstance(charge_id, str) or not charge_id.strip():
        raise PaymentValidationError("Отсутствует идентификатор платежа.")
    field = "telegram_charge_id" if provider == "telegram_stars" else "provider_payment_id"
    if order["status"] in ("paid", "refund_pending", "refunded"):
        if order[field] != charge_id:
            raise PaymentValidationError("Заказ уже связан с другим платежом.")
        return {"credited": False, "balance": await _balance(conn, user_id), "tokens": order["tokens"], "order_id": order["order_id"]}
    collision = await _one(conn, f"SELECT order_id FROM payment_orders WHERE {field}=? AND order_id<>?", (charge_id, order["order_id"]))
    if collision or order[field] not in (None, charge_id):
        raise PaymentValidationError("Идентификатор платежа уже использован.")
    # Expiry applies to initiation, never to a verified completed payment arriving late.
    await conn.execute(f"UPDATE payment_orders SET status='paid',{field}=?,provider_charge_id=?,paid_at=? WHERE order_id=?",
                       (charge_id, provider_charge_id, _iso(), order["order_id"]))
    await conn.execute("UPDATE users SET tokens=COALESCE(tokens,0)+?,updated_at=? WHERE telegram_id=?", (order["tokens"], _iso(), user_id))
    balance = await _ledger(conn, user_id, order["tokens"], "payment", f"Пакет {order['package_id']}", "payment:" + order["order_id"])
    return {"credited": True, "balance": balance, "tokens": order["tokens"], "order_id": order["order_id"]}


async def confirm_payment(user_id: int, payload: str, currency: str, total_amount: int,
                          telegram_charge_id: str, provider_charge_id: str = "") -> dict:
    async with _connection(write=True) as conn:
        order = await _one(conn, "SELECT * FROM payment_orders WHERE payload=?", (payload,))
        return await _confirm(conn, order, user_id, currency, total_amount, "telegram_stars", telegram_charge_id, provider_charge_id)


async def attach_external_payment(order_id: str, provider_payment_id: str, confirmation_url: str | None = None) -> dict:
    if not isinstance(provider_payment_id, str) or not provider_payment_id.strip():
        raise PaymentValidationError("Пустой идентификатор платежа.")
    async with _connection(write=True) as conn:
        order = await _one(conn, "SELECT * FROM payment_orders WHERE order_id=?", (order_id,))
        if order is None or order["provider"] != "yookassa" or order["provider_payment_id"] not in (None, provider_payment_id):
            raise PaymentValidationError("Нельзя привязать платёж к заказу.")
        other = await _one(conn, "SELECT 1 FROM payment_orders WHERE provider_payment_id=? AND order_id<>?", (provider_payment_id, order_id))
        if other:
            raise PaymentValidationError("Платёж уже привязан к другому заказу.")
        await conn.execute("UPDATE payment_orders SET provider_payment_id=?,confirmation_url=COALESCE(?,confirmation_url) WHERE order_id=?", (provider_payment_id, confirmation_url, order_id))
        return dict(await _one(conn, "SELECT * FROM payment_orders WHERE order_id=?", (order_id,)))


async def confirm_external_payment(order_id: str, payment_id: str, amount_kopecks: int,
                                   currency: str, metadata_user_id: int, paid: bool,
                                   status: str) -> dict:
    if paid is not True or status != "succeeded":
        raise PaymentValidationError("Провайдер ещё не подтвердил оплату.")
    async with _connection(write=True) as conn:
        order = await _one(conn, "SELECT * FROM payment_orders WHERE order_id=?", (order_id,))
        if order is None or order["provider_payment_id"] != payment_id:
            raise PaymentValidationError("Неизвестный платёж провайдера.")
        return await _confirm(conn, order, metadata_user_id, currency, amount_kopecks, "yookassa", payment_id)


def _product_key(user, product, access_key):
    if product not in PRODUCT_PRICES:
        raise CommerceError("Неизвестная услуга.")
    if product in ("matrix", "matrix_report"):
        if not user["birth_date"] or (access_key is not None and access_key != user["birth_date"]):
            raise CommerceError("Профиль изменился. Откройте матрицу заново.")
        return user["birth_date"]
    if product in ("full_reading", "match"):
        try:
            return date.fromisoformat(str(access_key)).isoformat()
        except ValueError as exc:
            raise CommerceError("Не указана дата доступа.") from exc
    return "subscription"


async def has_entitlement(user_id: int, product: str, access_key: str) -> bool:
    async with _connection() as conn:
        wildcard = "*" if product == "matrix" else access_key
        return bool(await _one(conn, "SELECT 1 FROM product_entitlements WHERE user_id=? AND product=? AND access_key IN (?,?)", (user_id, product, access_key, wildcard)))


async def purchase_product(user_id: int, product: str, access_key: str | None = None) -> PurchaseResult:
    """Charge once and grant durable access atomically. Keys are chosen by bot handlers."""
    async with _connection(write=True) as conn:
        user = await _one(conn, "SELECT * FROM users WHERE telegram_id=?", (user_id,))
        if user is None:
            raise CommerceError("Профиль не найден.")
        key = _product_key(user, product, access_key)
        balance, price = int(user["tokens"] or 0), PRODUCT_PRICES[product]
        sub_until = _datetime(user["subscription_until"])
        sub_active = sub_until is not None and sub_until > _now()
        owned = sub_active if product == "subscription" else await _one(conn, "SELECT 1 FROM product_entitlements WHERE user_id=? AND product=? AND access_key IN (?,?)", (user_id, product, key, "*" if product == "matrix" else key))
        if owned or (sub_active and product in ("full_reading", "match")):
            return PurchaseResult("already_owned", balance, price, key)
        if balance < price:
            return PurchaseResult("insufficient", balance, price, key)
        await conn.execute("UPDATE users SET tokens=tokens-?,updated_at=? WHERE telegram_id=?", (price, _iso(), user_id))
        if product == "subscription":
            key = (_now() + timedelta(days=30)).isoformat()
            await conn.execute("UPDATE users SET subscription_until=? WHERE telegram_id=?", (key, user_id))
        else:
            legacy_column = {"full_reading": "paid_full_date", "match": "paid_match_date"}.get(product)
            if legacy_column:
                await conn.execute(f"UPDATE users SET {legacy_column}=? WHERE telegram_id=?", (key, user_id))
            elif product == "matrix":
                await conn.execute("UPDATE users SET matrix_access=1,matrix_paid_at=? WHERE telegram_id=?", (_iso(), user_id))
        await conn.execute("INSERT INTO product_entitlements VALUES(?,?,?,?,?)", (user_id, product, key, _iso(), price))
        if product == "matrix_report":
            await conn.execute("INSERT OR IGNORE INTO product_entitlements VALUES(?,?,?,?,0)", (user_id, "matrix", key, _iso()))
            await conn.execute("UPDATE users SET matrix_access=1,matrix_paid_at=COALESCE(matrix_paid_at,?) WHERE telegram_id=?", (_iso(), user_id))
        balance = await _ledger(conn, user_id, -price, "purchase", product + ":" + key, f"purchase:{user_id}:{product}:{key}")
        return PurchaseResult("purchased", balance, price, key)


async def adjust_tokens(actor_id: int, user_id: int, delta: int, reason: str, request_id: str) -> dict:
    if type(delta) is not int or not 0 < abs(delta) <= 100000 or not 3 <= len(reason.strip()) <= 500 or not request_id:
        raise CommerceError("Некорректная сумма, причина или идентификатор операции.")
    rid = "admin:" + request_id
    async with _connection(write=True) as conn:
        previous = await _one(conn, "SELECT * FROM token_transactions WHERE request_id=?", (rid,))
        if previous:
            if (previous["user_id"], previous["delta"], previous["actor_id"], previous["reason"]) != (user_id, delta, actor_id, reason.strip()):
                raise CommerceError("Идентификатор операции уже использован с другими параметрами.")
            return {"changed": False, "balance": await _balance(conn, user_id)}
        await _balance(conn, user_id)
        await conn.execute("UPDATE users SET tokens=COALESCE(tokens,0)+?,updated_at=? WHERE telegram_id=?", (delta, _iso(), user_id))
        balance = await _ledger(conn, user_id, delta, "admin_adjustment", reason.strip(), rid, actor_id)
        return {"changed": True, "balance": balance}


async def get_payment(order_id: str) -> dict | None:
    async with _connection() as conn:
        row = await _one(conn, "SELECT * FROM payment_orders WHERE order_id=?", (order_id,))
        return dict(row) if row else None


async def get_payment_by_provider_id(payment_id: str) -> dict | None:
    async with _connection() as conn:
        row = await _one(conn, "SELECT * FROM payment_orders WHERE provider_payment_id=?", (payment_id,))
        return dict(row) if row else None


async def get_payment_by_payload(payload: str) -> dict | None:
    async with _connection() as conn:
        row = await _one(conn, "SELECT * FROM payment_orders WHERE payload=?", (payload,))
        return dict(row) if row else None


async def list_pending_external_payments(limit: int = 100) -> list[dict]:
    """Select and rotate work fairly; old unresolved orders cannot starve new ones."""
    async with _connection(write=True) as conn:
        # Expired invoices can still produce late successful confirmations.
        rows = await (await conn.execute("SELECT * FROM payment_orders WHERE provider='yookassa' AND status='pending' ORDER BY COALESCE(last_reconcile_at,created_at),order_id LIMIT ?", (min(max(limit, 1), 1000),))).fetchall()
        await conn.executemany("UPDATE payment_orders SET last_reconcile_at=? WHERE order_id=?", [(_iso(), row["order_id"]) for row in rows])
        return [dict(row) for row in rows]


def _canonical_request(request_json: dict | str) -> str:
    try:
        body = json.loads(request_json) if isinstance(request_json, str) else request_json
        if not isinstance(body, dict):
            raise ValueError("Expected an object")
        encoded = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        if len(encoded) > 100000:
            raise ValueError("Request too large")
        return encoded
    except (ValueError, TypeError) as exc:
        raise PaymentValidationError("Некорректный запрос провайдера.") from exc


async def save_external_request(order_id: str, request_json: dict | str) -> dict:
    encoded = _canonical_request(request_json)
    async with _connection(write=True) as conn:
        order = await _one(conn, "SELECT * FROM payment_orders WHERE order_id=?", (order_id,))
        if not order or order["provider"] != "yookassa" or order["request_json"] not in (None, encoded):
            raise PaymentValidationError("Запрос платежа уже сохранён с другими параметрами.")
        await conn.execute("UPDATE payment_orders SET request_json=? WHERE order_id=?", (encoded, order_id))
        return dict(await _one(conn, "SELECT * FROM payment_orders WHERE order_id=?", (order_id,)))


async def mark_external_canceled(order_id: str) -> None:
    """Only after the provider authoritatively reports canceled; no token mutation."""
    async with _connection(write=True) as conn:
        await conn.execute("UPDATE payment_orders SET status='canceled' WHERE order_id=? AND provider='yookassa' AND status='pending'", (order_id,))


async def begin_refund(actor_id: int, order_id: str, reason: str) -> dict:
    if not 3 <= len(reason.strip()) <= 500:
        raise CommerceError("Укажите причину возврата (3–500 символов).")
    async with _connection(write=True) as conn:
        order = await _one(conn, "SELECT * FROM payment_orders WHERE order_id=?", (order_id,))
        if not order or order["status"] != "paid":
            raise CommerceError("Возврат уже выполнен, ожидает сверки или платёж не завершён.")
        attempt = uuid4().hex
        await conn.execute("INSERT INTO refund_attempts(attempt_id,order_id,actor_id,reason,status,error,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)", (attempt, order_id, actor_id, reason.strip(), "pending", None, _iso(), _iso()))
        await conn.execute("UPDATE payment_orders SET status='refund_pending',refund_attempt_id=? WHERE order_id=?", (attempt, order_id))
        result = dict(await _one(conn, "SELECT * FROM payment_orders WHERE order_id=?", (order_id,)))
        result["attempt_id"] = attempt
        return result


async def save_refund_request(attempt_id: str, request_json: dict | str) -> dict:
    encoded = _canonical_request(request_json)
    async with _connection(write=True) as conn:
        attempt = await _one(conn, "SELECT * FROM refund_attempts WHERE attempt_id=?", (attempt_id,))
        if not attempt or attempt["request_json"] not in (None, encoded):
            raise PaymentValidationError("Запрос возврата уже сохранён с другими параметрами.")
        await conn.execute("UPDATE refund_attempts SET request_json=? WHERE attempt_id=?", (encoded, attempt_id))
        return dict(await _one(conn, "SELECT * FROM refund_attempts WHERE attempt_id=?", (attempt_id,)))


async def attach_external_refund(attempt_id: str, provider_refund_id: str) -> dict:
    if not isinstance(provider_refund_id, str) or not provider_refund_id.strip():
        raise PaymentValidationError("Пустой идентификатор возврата.")
    async with _connection(write=True) as conn:
        attempt = await _one(conn, "SELECT * FROM refund_attempts WHERE attempt_id=?", (attempt_id,))
        if not attempt or attempt["provider_refund_id"] not in (None, provider_refund_id):
            raise PaymentValidationError("Возврат уже связан с другим идентификатором.")
        order = await _one(conn, "SELECT provider FROM payment_orders WHERE order_id=?", (attempt["order_id"],))
        if order["provider"] != "yookassa":
            raise PaymentValidationError("Этот возврат не относится к внешнему провайдеру.")
        other = await _one(conn, "SELECT 1 FROM refund_attempts WHERE provider_refund_id=? AND attempt_id<>?", (provider_refund_id, attempt_id))
        if other:
            raise PaymentValidationError("Идентификатор возврата уже использован.")
        await conn.execute("UPDATE refund_attempts SET provider_refund_id=?,updated_at=? WHERE attempt_id=?", (provider_refund_id, _iso(), attempt_id))
        return dict(await _one(conn, "SELECT * FROM refund_attempts WHERE attempt_id=?", (attempt_id,)))


async def list_pending_external_refunds(limit: int = 100) -> list[dict]:
    async with _connection(write=True) as conn:
        rows = await (await conn.execute("""SELECT o.*,r.attempt_id,r.reason,r.actor_id,
            r.created_at AS refund_created_at,r.provider_refund_id,r.request_json AS refund_request_json
            FROM payment_orders o JOIN refund_attempts r ON o.refund_attempt_id=r.attempt_id
            WHERE o.provider='yookassa' AND o.status='refund_pending' AND r.status='pending'
            ORDER BY COALESCE(r.last_reconcile_at,r.created_at),r.attempt_id LIMIT ?""", (min(max(limit, 1), 1000),))).fetchall()
        await conn.executemany("UPDATE refund_attempts SET last_reconcile_at=? WHERE attempt_id=?", [(_iso(), row["attempt_id"]) for row in rows])
        return [dict(row) for row in rows]


async def _refund_debit(conn, order, actor_id, reason):
    if order["status"] == "refunded":
        return {"refunded": False, "balance": await _balance(conn, order["user_id"]), "order_id": order["order_id"]}
    if order["status"] not in ("paid", "refund_pending"):
        raise CommerceError("Возврат требует сверки: начисление не найдено.")
    await conn.execute("UPDATE users SET tokens=COALESCE(tokens,0)-?,updated_at=? WHERE telegram_id=?", (order["tokens"], _iso(), order["user_id"]))
    await conn.execute("UPDATE payment_orders SET status='refunded',refunded_at=? WHERE order_id=?", (_iso(), order["order_id"]))
    balance = await _ledger(conn, order["user_id"], -order["tokens"], "refund", reason, "refund:" + order["order_id"], actor_id)
    if order["refund_attempt_id"]:
        await conn.execute("UPDATE refund_attempts SET status='succeeded',updated_at=? WHERE attempt_id=?", (_iso(), order["refund_attempt_id"]))
    return {"refunded": True, "balance": balance, "order_id": order["order_id"]}


async def finish_refund(attempt_id: str, success: bool, error: str = "") -> dict:
    async with _connection(write=True) as conn:
        attempt = await _one(conn, "SELECT * FROM refund_attempts WHERE attempt_id=?", (attempt_id,))
        if not attempt:
            raise CommerceError("Попытка возврата не найдена.")
        order = await _one(conn, "SELECT * FROM payment_orders WHERE order_id=?", (attempt["order_id"],))
        if order["status"] == "refunded":
            return {"refunded": False, "balance": await _balance(conn, order["user_id"]), "order_id": order["order_id"]}
        if order["refund_attempt_id"] != attempt_id or attempt["status"] == "failed":
            raise CommerceError("Попытка устарела; требуется сверка.")
        if success:
            return await _refund_debit(conn, order, attempt["actor_id"], attempt["reason"])
        await conn.execute("UPDATE refund_attempts SET status='failed',error=?,updated_at=? WHERE attempt_id=?", (error[:1000], _iso(), attempt_id))
        await conn.execute("UPDATE payment_orders SET status='paid' WHERE order_id=?", (order["order_id"],))
        return {"refunded": False, "balance": await _balance(conn, order["user_id"]), "order_id": order["order_id"]}


async def mark_refund_uncertain(attempt_id: str, error: str) -> None:
    async with _connection(write=True) as conn:
        await conn.execute("UPDATE refund_attempts SET error=?,updated_at=? WHERE attempt_id=? AND status='pending'", (error[:1000], _iso(), attempt_id))


async def reconcile_refund(telegram_charge_id: str, user_id: int, currency: str, total_amount: int) -> dict:
    async with _connection(write=True) as conn:
        order = await _one(conn, "SELECT * FROM payment_orders WHERE telegram_charge_id=?", (telegram_charge_id,))
        _validate(order, user_id, currency, total_amount, "telegram_stars")
        return await _refund_debit(conn, order, None, "Возврат подтверждён Telegram")


async def list_payments(limit: int = 20, user_id: int | None = None, offset: int = 0) -> list[dict]:
    async with _connection() as conn:
        where, args = ("WHERE user_id=?", [user_id]) if user_id is not None else ("", [])
        rows = await (await conn.execute(f"SELECT * FROM payment_orders {where} ORDER BY created_at DESC LIMIT ? OFFSET ?", (*args, min(max(limit, 1), 100), max(offset, 0)))).fetchall()
        return [dict(row) for row in rows]


async def list_transactions(user_id: int | None = None, limit: int = 20, offset: int = 0) -> list[dict]:
    async with _connection() as conn:
        where, args = ("WHERE user_id=?", [user_id]) if user_id is not None else ("", [])
        rows = await (await conn.execute(f"SELECT * FROM token_transactions {where} ORDER BY id DESC LIMIT ? OFFSET ?", (*args, min(max(limit, 1), 100), max(offset, 0)))).fetchall()
        return [dict(row) for row in rows]


async def stats() -> dict:
    async with _connection() as conn:
        users = await _one(conn, "SELECT COUNT(*),COALESCE(SUM(tokens),0) FROM users")
        subscriptions = await (await conn.execute("SELECT subscription_until FROM users WHERE subscription_until IS NOT NULL")).fetchall()
        payment = await _one(conn, """SELECT
            SUM(CASE WHEN status IN ('paid','refund_pending','refunded') THEN 1 ELSE 0 END),
            SUM(CASE WHEN status IN ('paid','refund_pending','refunded') THEN stars ELSE 0 END),
            SUM(CASE WHEN status='refunded' THEN stars ELSE 0 END),
            SUM(CASE WHEN status='refund_pending' THEN 1 ELSE 0 END),
            SUM(CASE WHEN status IN ('paid','refund_pending','refunded') THEN amount_kopecks ELSE 0 END),
            SUM(CASE WHEN status='refunded' THEN amount_kopecks ELSE 0 END)
            FROM payment_orders""")
        return dict(zip(("users", "total_tokens", "subscriptions_active", "paid_orders", "stars_gross", "stars_refunded", "refunds_pending", "rub_gross_kopecks", "rub_refunded_kopecks"),
                        (users[0], users[1], sum(bool(_datetime(r[0]) and _datetime(r[0]) > _now()) for r in subscriptions), *(v or 0 for v in payment))))
