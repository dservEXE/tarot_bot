"""Private, authenticated Telegram administration. No side effects on import.

Register make_admin_router(ADMIN_IDS) before the public router. Broadcasts use
the existing notify_enabled preference: this is not separate marketing consent.
All mutations require a short-lived, author-scoped confirmation in the FSM.
"""
from __future__ import annotations

import asyncio
import html
import logging
import secrets
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

import aiosqlite
from aiogram import BaseMiddleware, F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

import commerce
import db

logger = logging.getLogger(__name__)
PAGE_SIZE = 10
MAX_GRANT = 100_000
CONFIRM_TTL = 15 * 60


class AdminPanelStates(StatesGroup):
    lookup = State()
    grant_target = State()
    grant_amount = State()
    grant_reason = State()
    refund_reason = State()
    broadcast_text = State()
    confirm = State()


def e(value: Any) -> str:
    return html.escape(str(value if value is not None else "—"))


def keyboard(*rows: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=text, callback_data=data) for text, data in row]
        for row in rows
    ])


def panel_keyboard() -> InlineKeyboardMarkup:
    return keyboard(
        [("📊 Обзор", "admin:stats"), ("👥 Пользователи", "admin:users:0")],
        [("🔎 Найти пользователя", "admin:search")],
        [("💳 Платежи", "admin:payments:0"), ("📒 Журнал токенов", "admin:ledger:0")],
        [("✨ Начислить токены", "admin:give_tokens")],
        [("📣 Подготовить рассылку", "admin:broadcast")],
        [("← В бот", "back:main")],
    )


def back_keyboard() -> InlineKeyboardMarkup:
    return keyboard([("← Админ-панель / отмена", "admin:panel")])


def confirmation_keyboard(action: str, owner: int, nonce: str) -> InlineKeyboardMarkup:
    label = {"grant": "Подтвердить начисление", "refund": "Подтвердить возврат", "broadcast": "Отправить рассылку",
             "delete_user": "Удалить все данные"}[action]
    return keyboard(
        [(label, f"admin:confirm:{action}:{owner}:{nonce}")],
        [("Отмена", "admin:panel")],
    )


def parse_amount(raw: str) -> int:
    raw = raw.strip()
    if not raw.isascii() or not raw.isdecimal() or len(raw) > 6:
        raise ValueError("Введите целое число токенов.")
    amount = int(raw)
    if not 1 <= amount <= MAX_GRANT:
        raise ValueError(f"Можно начислить от 1 до {MAX_GRANT:,} токенов за одну операцию.".replace(",", " "))
    return amount


def money(payment: dict) -> str:
    if payment.get("currency") == "RUB" or payment.get("provider") == "yookassa":
        amount = int(payment.get("amount_kopecks") or payment.get("amount") or 0)
        return f"{amount / 100:,.2f} ₽".replace(",", " ")
    return f"{int(payment.get('stars') or payment.get('amount') or 0)} ⭐"


def short_date(value: Any) -> str:
    if not value:
        return "—"
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.strftime("%d.%m.%Y %H:%M")
    except (ValueError, TypeError):
        return "—"


def subscription_label(value: Any) -> str:
    if not value:
        return "не активна"
    try:
        expiry = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        active = expiry > datetime.now(timezone.utc)
        return ("до " if active else "истекла ") + expiry.strftime("%d.%m.%Y")
    except (ValueError, TypeError):
        return "нужно проверить дату"


def user_card(user: dict) -> str:
    complete = all(user.get(key) for key in ("birth_date", "birth_time", "birth_place"))
    username = "@" + str(user["username"]) if user.get("username") else "не указан"
    return (
        f"<b>👤 {e(user.get('name'))}</b>\n"
        f"ID: <code>{int(user['telegram_id'])}</code> · {e(username)}\n\n"
        f"<b>Баланс:</b> {int(user.get('tokens') or 0)} токенов\n"
        f"<b>Подписка:</b> {subscription_label(user.get('subscription_until'))}\n"
        f"<b>Профиль:</b> {'заполнен' if complete else 'не завершён'}\n"
        f"Дата рождения: {e(user.get('birth_date'))}\n"
        f"Время и место: {e(user.get('birth_time'))} · {e(str(user.get('birth_place') or '—')[:250])}\n"
        f"Часовой пояс: {e(user.get('timezone'))}\n"
        f"Уведомления: {'включены' if user.get('notify_enabled') else 'выключены'}\n"
        f"Матрица: {'открыта' if user.get('_matrix_owned', user.get('matrix_access')) else 'не открыта'}\n"
        f"Разбор матрицы: {'открыт' if user.get('_matrix_report_owned') else 'не открыт'}\n"
        f"Дата регистрации: {short_date(user.get('created_at'))} UTC"
    )


async def find_user(raw: str) -> dict | None:
    value = raw.strip()
    if value.isascii() and value.isdecimal() and len(value) <= 19:
        identifier = int(value)
        if identifier <= 0 or identifier > 2**63 - 1:
            return None
        result = await db.get_user(identifier)
    elif 1 <= len(value.lstrip("@")) <= 32:
        result = await db.get_user_by_username(value)
    else:
        return None
    return dict(result) if result else None


async def user_page(page: int) -> tuple[list[dict], int]:
    async with aiosqlite.connect(db.DB_NAME) as connection:
        connection.row_factory = aiosqlite.Row
        count_cursor = await connection.execute("SELECT COUNT(*) FROM users")
        total = (await count_cursor.fetchone())[0]
        cursor = await connection.execute(
            "SELECT * FROM users ORDER BY telegram_id DESC LIMIT ? OFFSET ?",
            (PAGE_SIZE, page * PAGE_SIZE),
        )
        return [dict(row) for row in await cursor.fetchall()], total


async def broadcast_recipients() -> list[int]:
    async with aiosqlite.connect(db.DB_NAME) as connection:
        cursor = await connection.execute("SELECT telegram_id FROM users WHERE notify_enabled = 1 ORDER BY telegram_id")
        return [int(row[0]) for row in await cursor.fetchall()]


async def render(target: Message | CallbackQuery, text: str, markup: InlineKeyboardMarkup | None = None) -> None:
    if isinstance(target, CallbackQuery):
        if not isinstance(target.message, Message):
            return
        try:
            await target.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
            return
        except TelegramBadRequest as exc:
            if "message is not modified" in str(exc).lower():
                return
            await target.message.answer(text, reply_markup=markup, parse_mode="HTML")
    else:
        await target.answer(text, reply_markup=markup, parse_mode="HTML")


class AdminGuard(BaseMiddleware):
    """Inner middleware protects matched commands, state messages, and callbacks."""

    def __init__(self, admin_ids: set[int]):
        self.admin_ids = frozenset(admin_ids)

    async def __call__(self, handler, event: Message | CallbackQuery, data: dict):
        actor = event.from_user
        source = event.message if isinstance(event, CallbackQuery) else event
        private = source is not None and source.chat.type == "private" and source.chat.id == (actor.id if actor else None)
        if not actor or actor.id not in self.admin_ids or not private:
            if isinstance(event, CallbackQuery):
                await event.answer("Админ-панель доступна владельцу в личном чате с ботом.", show_alert=True)
            elif private:
                await event.answer("Эта команда доступна только администратору.", parse_mode=None)
            return
        if isinstance(event, CallbackQuery):
            # Acknowledge before potentially slow queries or broadcast delivery.
            try:
                await event.answer()
            except TelegramAPIError:
                pass
        try:
            return await handler(event, data)
        except commerce.CommerceError as exc:
            await render(event, "Операция не выполнена: " + e(str(exc)) + "\nОткройте панель и проверьте состояние.", back_keyboard())
        except Exception:
            logger.exception("Admin operation failed for administrator %s", actor.id)
            await render(event, "Не удалось завершить действие. Проверьте журнал и состояние операции перед повтором.", back_keyboard())


async def prepare_confirmation(state: FSMContext, action: str, owner: int, **payload) -> str:
    nonce = secrets.token_hex(6)
    await state.set_state(AdminPanelStates.confirm)
    await state.set_data({"action": action, "owner": owner, "nonce": nonce, "expires": time.time() + CONFIRM_TTL, **payload})
    return nonce


def confirmation_matches(data: dict, action: str, owner: int, nonce: str) -> bool:
    return (
        data.get("action") == action and data.get("owner") == owner
        and isinstance(data.get("nonce"), str) and secrets.compare_digest(data["nonce"], nonce)
        and data.get("expires", 0) > time.time()
    )


async def deliver_broadcast(bot, recipients: list[int], text: str, progress=None, stop_event: asyncio.Event | None = None) -> dict:
    """Send plain text; recheck notifications immediately before each delivery.

    Only explicit Telegram rate-limit responses are retried. Network failures
    have unknown delivery status and are not blindly resent.
    """
    result = {"sent": 0, "blocked": 0, "failed": 0, "skipped": 0, "stopped": 0}
    for index, user_id in enumerate(recipients):
        if stop_event and stop_event.is_set():
            result["stopped"] = len(recipients) - index
            break
        user = await db.get_user(user_id)
        if not user or user["notify_enabled"] != 1:
            result["skipped"] += 1
            continue
        for attempt in range(3):
            try:
                await bot.send_message(user_id, text, parse_mode=None)
                result["sent"] += 1
                break
            except TelegramRetryAfter as exc:
                if attempt == 2 or exc.retry_after > 30:
                    result["failed"] += 1
                    break
                await asyncio.sleep(max(0.1, exc.retry_after) + 0.2)
                if stop_event and stop_event.is_set():
                    result["stopped"] = len(recipients) - index
                    return result
            except TelegramForbiddenError:
                result["blocked"] += 1
                break
            except TelegramAPIError:
                result["failed"] += 1
                break
        if progress and (index + 1) % 25 == 0:
            await progress(index + 1, len(recipients), result)
        await asyncio.sleep(0.06)
    return result


def make_admin_router(admin_ids: set[int]) -> Router:
    router = Router(name="secure_admin_panel")
    guard = AdminGuard(admin_ids)
    router.message.middleware(guard)
    router.callback_query.middleware(guard)
    locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
    active_broadcasts: dict[int, asyncio.Event] = {}

    async def panel(target, state: FSMContext):
        await state.clear()
        await render(target,
            "<b>⚙️ Управление ботом</b>\n\n"
            "Пользователи, платежи и токены — в одном месте. "
            "Начисления, возвраты и рассылки выполняются только после предпросмотра и подтверждения.\n\n"
            "Все даты в журнале показаны в UTC.", panel_keyboard())

    router.message.register(panel, Command("admin"))
    router.message.register(panel, StateFilter(AdminPanelStates), Command("cancel"))
    router.callback_query.register(panel, F.data.in_({"admin:panel", "admin_menu"}))

    @router.callback_query(F.data.in_({"admin:stats", "admin_stats"}))
    async def show_stats(callback: CallbackQuery, state: FSMContext):
        await state.clear()
        stats = await commerce.stats()
        rub_gross = int(stats.get("rub_gross_kopecks", 0)) / 100
        rub_refund = int(stats.get("rub_refunded_kopecks", 0)) / 100
        await render(callback,
            "<b>📊 Обзор проекта</b>\n\n"
            f"Пользователей: <b>{int(stats.get('users', 0))}</b>\n"
            f"Активных подписок: <b>{int(stats.get('subscriptions_active', 0))}</b>\n"
            f"Токенов на балансах: <b>{int(stats.get('total_tokens', 0))}</b>\n\n"
            f"Оплаченных заказов: <b>{int(stats.get('paid_orders', 0))}</b>\n"
            f"Поступления: <b>{rub_gross:.2f} ₽ · {int(stats.get('stars_gross', 0))} ⭐</b>\n"
            f"Возвраты: <b>{rub_refund:.2f} ₽ · {int(stats.get('stars_refunded', 0))} ⭐</b>\n"
            f"Возвраты на проверке: <b>{int(stats.get('refunds_pending', 0))}</b>\n\n"
            "Суммы до комиссий и налогов. Платежи и движение токенов смотрите в журналах.", panel_keyboard())

    @router.callback_query(F.data.startswith("admin:users"))
    @router.callback_query(F.data == "admin_users")
    async def show_users(callback: CallbackQuery, state: FSMContext):
        await state.clear()
        raw = (callback.data or "").split(":")
        page = min(int(raw[2]), 1_000_000) if len(raw) == 3 and raw[2].isdigit() else 0
        users, total = await user_page(page)
        if not users and page:
            page = max(0, (total - 1) // PAGE_SIZE)
            users, total = await user_page(page)
        lines = [f"<b>👥 Пользователи · {total}</b>", f"Страница {page + 1} из {max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)}", ""]
        rows = []
        for user in users:
            user_id = int(user["telegram_id"])
            label = str(user.get("name") or user_id)[:36]
            lines.append(f"<code>{user_id}</code> · {e(label)} · {int(user.get('tokens') or 0)} ток.")
            rows.append([(f"👤 {label}", f"admin:user:{user_id}")])
        nav = []
        if page:
            nav.append(("←", f"admin:users:{page - 1}"))
        if (page + 1) * PAGE_SIZE < total:
            nav.append(("→", f"admin:users:{page + 1}"))
        if nav:
            rows.append(nav)
        rows.extend([[("🔎 Поиск по ID / @username", "admin:search")], [("← Админ-панель", "admin:panel")]])
        await render(callback, "\n".join(lines) if users else "<b>👥 Пользователей пока нет</b>", keyboard(*rows))

    async def show_user(target, user: dict):
        uid = int(user["telegram_id"])
        birthday = str(user.get("birth_date") or "")
        if birthday:
            chart, report = await asyncio.gather(
                commerce.has_entitlement(uid, "matrix", birthday),
                commerce.has_entitlement(uid, "matrix_report", birthday),
            )
            user = dict(user, _matrix_owned=chart, _matrix_report_owned=report)
        await render(target, user_card(user), keyboard(
            [("✨ Начислить токены", f"admin:grant:{uid}")],
            [("💳 Платежи", f"admin:payments:0:{uid}"), ("📒 Токены", f"admin:ledger:0:{uid}")],
            [("🗑 Удалить все данные", f"admin:delete:{uid}")],
            [("← Пользователи", "admin:users:0")],
            [("← Админ-панель", "admin:panel")],
        ))

    @router.callback_query(F.data.startswith("admin:user:"))
    async def show_user_callback(callback: CallbackQuery, state: FSMContext):
        await state.clear()
        user = await find_user(callback.data.rsplit(":", 1)[-1])
        if not user:
            await render(callback, "Пользователь не найден.", back_keyboard())
            return
        await show_user(callback, user)

    @router.callback_query(F.data.startswith("admin:delete:"))
    async def delete_user_start(callback: CallbackQuery, state: FSMContext):
        raw = callback.data.rsplit(":", 1)[-1]
        if not raw.isdigit():
            await render(callback, "Некорректный Telegram ID.", back_keyboard())
            return
        uid = int(raw)
        if uid in guard.admin_ids:
            await render(callback, "Удаление данных администратора через эту панель запрещено.", back_keyboard())
            return
        user = await find_user(raw)
        if not user:
            await render(callback, "Пользователь уже отсутствует.", back_keyboard())
            return
        nonce = await prepare_confirmation(state, "delete_user", callback.from_user.id,
                                           user_id=uid, name=str(user.get("name") or ""))
        await render(callback,
            "<b>Удалить все данные пользователя?</b>\n\n"
            f"Пользователь: {e(user.get('name'))} · <code>{uid}</code>\n"
            "Будут удалены профиль, согласие, токены, журнал токенов, покупки, права доступа и кэш раскладов.\n"
            "Связи приглашений у других пользователей будут очищены. Восстановление из панели невозможно.\n\n"
            "Сначала сохрани номер заказа, если он нужен для отдельного бухгалтерского учёта.",
            confirmation_keyboard("delete_user", callback.from_user.id, nonce))

    @router.callback_query(F.data == "admin:search")
    async def search(callback: CallbackQuery, state: FSMContext):
        await state.clear()
        await state.set_state(AdminPanelStates.lookup)
        await render(callback, "<b>🔎 Найти пользователя</b>\n\nОтправьте Telegram ID или @username. Пользователь должен хотя бы раз открыть бота.", back_keyboard())

    @router.message(AdminPanelStates.lookup)
    async def receive_lookup(message: Message, state: FSMContext):
        user = await find_user(message.text or "")
        if not user:
            await render(message, "Не найден. Проверьте ID или @username и попробуйте ещё раз.", back_keyboard())
            return
        await state.clear()
        await show_user(message, user)

    @router.callback_query(F.data.in_({"admin:give_tokens", "admin_give_tokens"}))
    async def grant_start(callback: CallbackQuery, state: FSMContext):
        await state.clear()
        await state.set_state(AdminPanelStates.grant_target)
        await render(callback, "<b>✨ Начислить токены</b>\n\nОтправьте ID или @username получателя. Далее укажем количество и причину.", back_keyboard())

    async def grant_select(target, state: FSMContext, raw: str):
        user = await find_user(raw)
        if not user:
            await render(target, "Пользователь не найден. Укажите корректный ID или @username.", back_keyboard())
            return
        await state.set_data({"user_id": int(user["telegram_id"]), "name": user.get("name")})
        await state.set_state(AdminPanelStates.grant_amount)
        await render(target, f"Получатель: <b>{e(user.get('name'))}</b> · <code>{user['telegram_id']}</code>\n"
            f"Баланс: {int(user.get('tokens') or 0)} токенов.\n\nВведите количество от 1 до 100 000.", back_keyboard())

    @router.callback_query(F.data.startswith("admin:grant:"))
    async def grant_user(callback: CallbackQuery, state: FSMContext):
        await state.clear()
        await grant_select(callback, state, callback.data.rsplit(":", 1)[-1])

    @router.message(AdminPanelStates.grant_target)
    async def grant_receive_target(message: Message, state: FSMContext):
        await grant_select(message, state, message.text or "")

    @router.message(AdminPanelStates.grant_amount)
    async def grant_receive_amount(message: Message, state: FSMContext):
        try:
            amount = parse_amount(message.text or "")
        except ValueError as exc:
            await render(message, e(str(exc)), back_keyboard())
            return
        await state.update_data(amount=amount)
        await state.set_state(AdminPanelStates.grant_reason)
        await render(message, "<b>Причина начисления</b>\n\nНапример: «Компенсация за ошибку расклада, обращение №12». От 3 до 300 символов; причина попадёт в журнал.", back_keyboard())

    @router.message(AdminPanelStates.grant_reason)
    async def grant_receive_reason(message: Message, state: FSMContext):
        reason = (message.text or "").strip()
        if not 3 <= len(reason) <= 300:
            await render(message, "Укажите причину от 3 до 300 символов.", back_keyboard())
            return
        draft = await state.get_data()
        nonce = await prepare_confirmation(state, "grant", message.from_user.id,
            user_id=draft["user_id"], amount=draft["amount"], reason=reason)
        await render(message, "<b>Проверьте начисление</b>\n\n"
            f"Получатель: {e(draft.get('name'))} · <code>{draft['user_id']}</code>\n"
            f"Начислить: <b>{draft['amount']} токенов</b>\nПричина: {e(reason)}\n\n"
            "Подтверждение действует 15 минут. Уведомление получателю отдельно не отправляется.",
            confirmation_keyboard("grant", message.from_user.id, nonce))

    @router.callback_query(F.data.startswith("admin:payments:"))
    @router.callback_query(F.data.startswith("admin:ledger:"))
    async def show_financial_page(callback: CallbackQuery, state: FSMContext):
        await state.clear()
        parts = callback.data.split(":")
        kind = parts[1]
        page = min(int(parts[2]), 1_000_000) if parts[2].isdigit() else 0
        uid = int(parts[3]) if len(parts) == 4 and parts[3].isdigit() and len(parts[3]) <= 18 else None
        loader = commerce.list_payments if kind == "payments" else commerce.list_transactions
        records = await loader(user_id=uid, limit=PAGE_SIZE + 1, offset=page * PAGE_SIZE)
        title = "💳 Платежи" if kind == "payments" else "📒 Журнал токенов"
        lines = [f"<b>{title}</b> · страница {page + 1}"]
        if uid:
            lines.append(f"Пользователь <code>{uid}</code>")
        rows = []
        if not records:
            lines.append("\nЗаписей пока нет.")
        for item in records[:PAGE_SIZE]:
            if kind == "payments":
                order_id = str(item["order_id"])
                lines.append(f"\n{short_date(item.get('created_at'))} · <code>{int(item['user_id'])}</code>\n"
                    f"{money(item)} → {int(item['tokens'])} ток. · {e(payment_status(item.get('status')))}")
                rows.append([(f"{order_id[:8]} · {money(item)}", f"admin:payment:{order_id}")])
            else:
                delta = int(item["delta"])
                lines.append(f"\n{short_date(item.get('created_at'))} · <code>{int(item['user_id'])}</code>\n"
                    f"<b>{delta:+d}</b> → {int(item.get('balance_after') or 0)} ток. · {e(str(item.get('kind') or '')[:40])}\n"
                    f"{e(str(item.get('reason') or '—')[:90])}" +
                    (f" · админ {int(item['actor_id'])}" if item.get("actor_id") else ""))
        suffix = f":{uid}" if uid else ""
        nav = []
        if page:
            nav.append(("←", f"admin:{kind}:{page - 1}{suffix}"))
        if len(records) > PAGE_SIZE:
            nav.append(("→", f"admin:{kind}:{page + 1}{suffix}"))
        if nav:
            rows.append(nav)
        rows.append([("← Карточка пользователя", f"admin:user:{uid}")] if uid else [("← Админ-панель", "admin:panel")])
        await render(callback, "\n".join(lines), keyboard(*rows))

    @router.callback_query(F.data.startswith("admin:payment:"))
    async def show_payment(callback: CallbackQuery, state: FSMContext):
        await state.clear()
        payment = await commerce.get_payment(callback.data.rsplit(":", 1)[-1])
        if not payment:
            await render(callback, "Платёж не найден.", back_keyboard())
            return
        rows = [[("👤 Пользователь", f"admin:user:{payment['user_id']}")]]
        if payment["status"] == "paid":
            rows.append([("↩️ Подготовить полный возврат", f"admin:refund:{payment['order_id']}")])
        rows.append([("← Платежи", "admin:payments:0")])
        await render(callback, "<b>💳 Платёж</b>\n\n"
            f"Заказ: <code>{e(payment['order_id'])}</code>\n"
            f"Пользователь: <code>{int(payment['user_id'])}</code>\n"
            f"Провайдер: {e(payment.get('provider') or 'telegram_stars')}\n"
            f"Сумма: <b>{money(payment)}</b>\nТокены: {int(payment['tokens'])}\n"
            f"Статус: <b>{e(payment_status(payment['status']))}</b>\n"
            f"Создан: {short_date(payment.get('created_at'))} UTC\n\n"
            "Возврат отменяет начисление токенов. Если они потрачены, баланс может стать отрицательным.", keyboard(*rows))

    @router.callback_query(F.data.startswith("admin:refund:"))
    async def refund_start(callback: CallbackQuery, state: FSMContext):
        payment = await commerce.get_payment(callback.data.rsplit(":", 1)[-1])
        if not payment or payment["status"] != "paid":
            await render(callback, "Этот платёж уже возвращён, ожидает сверки или ещё не оплачен.", back_keyboard())
            return
        await state.set_state(AdminPanelStates.refund_reason)
        await state.set_data({"order_id": payment["order_id"]})
        await render(callback, "<b>↩️ Подготовка возврата</b>\n\nУкажите причину (3–300 символов). На следующем шаге появятся сумма и подтверждение.", back_keyboard())

    @router.message(AdminPanelStates.refund_reason)
    async def refund_receive_reason(message: Message, state: FSMContext):
        reason = (message.text or "").strip()
        if not 3 <= len(reason) <= 300:
            await render(message, "Укажите причину от 3 до 300 символов.", back_keyboard())
            return
        draft = await state.get_data()
        payment = await commerce.get_payment(draft["order_id"])
        if not payment or payment["status"] != "paid":
            await state.clear()
            await render(message, "Статус платежа изменился. Проверьте его в журнале.", back_keyboard())
            return
        user = await db.get_user(int(payment["user_id"]))
        balance = int(user["tokens"] or 0) if user else 0
        nonce = await prepare_confirmation(state, "refund", message.from_user.id, order_id=payment["order_id"], reason=reason)
        await render(message, "<b>Подтвердите полный возврат</b>\n\n"
            f"Заказ: <code>{e(payment['order_id'])}</code>\nПользователь: <code>{int(payment['user_id'])}</code>\n"
            f"Вернуть: <b>{money(payment)}</b>\nСписать начисление: <b>{int(payment['tokens'])} токенов</b>\n"
            f"Ожидаемый баланс: {balance - int(payment['tokens'])} токенов\nПричина: {e(reason)}\n\n"
            "Это реальный возврат денег через платёжный сервис. Подтверждение действует 15 минут.",
            confirmation_keyboard("refund", message.from_user.id, nonce))

    @router.callback_query(F.data.in_({"admin:broadcast", "admin_broadcast"}))
    async def broadcast_start(callback: CallbackQuery, state: FSMContext):
        if callback.from_user.id in active_broadcasts:
            await render(callback, "Предыдущая рассылка ещё выполняется.", keyboard([("Остановить рассылку", "admin:broadcast_stop")]))
            return
        await state.clear()
        await state.set_state(AdminPanelStates.broadcast_text)
        await render(callback, "<b>📣 Подготовить рассылку</b>\n\n"
            "Отправьте обычный текст до 3500 символов. Сначала покажу предпросмотр и число получателей.\n\n"
            "Получатели — пользователи с включёнными уведомлениями. Это настройка уведомлений бота, а не отдельное согласие на рекламу: используйте рассылку для сообщений о работе сервиса. HTML-разметка не применяется.", back_keyboard())

    @router.message(AdminPanelStates.broadcast_text)
    async def broadcast_receive_text(message: Message, state: FSMContext):
        text = (message.text or "").strip()
        if not 1 <= len(text) <= 3500:
            await render(message, "Отправьте текст от 1 до 3500 символов. Фото и вложения в этом режиме не поддерживаются.", back_keyboard())
            return
        recipients = await broadcast_recipients()
        if not recipients:
            await state.clear()
            await render(message, "Нет пользователей с включёнными уведомлениями.", back_keyboard())
            return
        nonce = await prepare_confirmation(state, "broadcast", message.from_user.id, text=text)
        await render(message, f"<b>Предпросмотр рассылки</b>\nПолучателей сейчас: <b>{len(recipients)}</b>\n\n"
            f"{e(text)}\n\n──────────\nПосле подтверждения начнётся отправка. Отключившие уведомления будут пропущены. Подтверждение: 15 минут.",
            confirmation_keyboard("broadcast", message.from_user.id, nonce))

    @router.callback_query(F.data == "admin:broadcast_stop")
    async def broadcast_stop(callback: CallbackQuery):
        stop = active_broadcasts.get(callback.from_user.id)
        if stop:
            stop.set()
            await render(callback, "Останавливаю рассылку. Уже отправленные сообщения останутся у получателей.", back_keyboard())
        else:
            await render(callback, "Активной рассылки нет.", back_keyboard())

    @router.callback_query(F.data.startswith("admin:confirm:"))
    async def confirm(callback: CallbackQuery, state: FSMContext):
        parts = callback.data.split(":")
        if len(parts) != 5 or not parts[3].isdigit() or int(parts[3]) != callback.from_user.id:
            await render(callback, "Подтверждать действие может только автор предпросмотра.", back_keyboard())
            return
        _, _, action, _, nonce = parts
        async with locks[callback.from_user.id]:
            draft = await state.get_data()
            if not confirmation_matches(draft, action, callback.from_user.id, nonce):
                await render(callback, "Это подтверждение истекло, отменено или уже использовано. Подготовьте действие заново.", back_keyboard())
                return
            # Consume before awaiting any external service: duplicate clicks cannot resend.
            await state.clear()
            if action == "broadcast":
                if callback.from_user.id in active_broadcasts:
                    await render(callback, "Рассылка уже выполняется.", back_keyboard())
                    return
                active_broadcasts[callback.from_user.id] = asyncio.Event()
        if action == "grant":
            result = await commerce.adjust_tokens(
                actor_id=callback.from_user.id, user_id=draft["user_id"], delta=draft["amount"],
                reason=draft["reason"], request_id=f"admin-grant:{callback.from_user.id}:{nonce}",
            )
            await render(callback, f"<b>✅ Начисление сохранено</b>\n\nПользователь: <code>{draft['user_id']}</code>\n"
                f"Начислено: {draft['amount']} токенов\nБаланс: <b>{int(result['balance'])} токенов</b>\nПричина записана в журнал.",
                keyboard([("👤 Открыть пользователя", f"admin:user:{draft['user_id']}")], [("← Админ-панель", "admin:panel")]))
        elif action == "refund":
            await execute_refund(callback, draft)
        elif action == "broadcast":
            try:
                recipients = await broadcast_recipients()
                stop = active_broadcasts[callback.from_user.id]
                await render(callback, f"<b>📣 Рассылка началась</b>\nПолучателей: {len(recipients)}.\nПрогресс обновляется каждые 25 сообщений.",
                    keyboard([("Остановить рассылку", "admin:broadcast_stop")]))

                async def progress(done, total, counts):
                    try:
                        await render(callback, f"<b>📣 Отправка: {done} / {total}</b>\nДоставлено: {counts['sent']}\nНедоступно: {counts['blocked'] + counts['failed']}",
                            keyboard([("Остановить рассылку", "admin:broadcast_stop")]))
                    except TelegramAPIError:
                        pass

                result = await deliver_broadcast(callback.bot, recipients, draft["text"], progress, stop)
                logger.info("Broadcast by admin %s completed: %s", callback.from_user.id, result)
                await render(callback, "<b>Рассылка завершена</b>\n\n"
                    f"Доставлено: <b>{result['sent']}</b>\nБот заблокирован: {result['blocked']}\n"
                    f"Отключены уведомления: {result['skipped']}\nОшибки / доставка не подтверждена: {result['failed']}\n"
                    f"Осталось после остановки: {result['stopped']}\n\nНе повторяйте всю рассылку из-за отдельных ошибок: это создаст дубликаты.", back_keyboard())
            finally:
                active_broadcasts.pop(callback.from_user.id, None)
        elif action == "delete_user":
            uid = int(draft.get("user_id", 0))
            if uid in guard.admin_ids:
                await render(callback, "Удаление данных администратора запрещено.", back_keyboard())
                return
            result = await db.delete_user_data(uid)
            if not result["deleted"]:
                await render(callback, "Пользователь уже отсутствует.\n\nДанные в текущей базе не найдены.", panel_keyboard())
                return
            counts = ", ".join(f"{key}: {value}" for key, value in result["counts"].items() if value)
            await render(callback,
                "<b>✅ Данные удалены</b>\n\n"
                f"Пользователь <code>{uid}</code> удалён из текущей базы.\n"
                + (f"Удалено записей: {e(counts)}." if counts else "Связанные записи не найдены.")
                + "\n\nРезервные копии и внешние платёжные кабинеты не изменялись.", panel_keyboard())

    async def execute_refund(callback: CallbackQuery, draft: dict):
        payment = await commerce.get_payment(draft["order_id"])
        if not payment or payment["status"] != "paid":
            await render(callback, "Статус платежа изменился. Проверьте журнал платежей.", back_keyboard())
            return
        if payment.get("provider") == "yookassa":
            from yookassa_provider import refund_order
            result = await refund_order(order_id=draft["order_id"], admin_id=callback.from_user.id, reason=draft["reason"])
            if result.get("status") == "paid":
                await render(callback, "<b>Возврат отменён платёжным сервисом</b>\n\nДеньги не возвращены, баланс токенов сохранён. Проверьте причину отмены в кабинете сервиса перед новой попыткой.",
                    keyboard([("Открыть платёж", f"admin:payment:{draft['order_id']}")], [("← Админ-панель", "admin:panel")]))
                return
            done = result.get("status") == "refunded"
        elif payment.get("provider", "telegram_stars") == "telegram_stars":
            attempt = await commerce.begin_refund(callback.from_user.id, draft["order_id"], draft["reason"])
            try:
                success = await callback.bot.refund_star_payment(
                    user_id=int(attempt["user_id"]), telegram_payment_charge_id=attempt["telegram_charge_id"])
            except TelegramAPIError as exc:
                # Even a Telegram error may follow a previously completed refund.
                # Leave a reservation; never risk retrying a financial operation blindly.
                await commerce.mark_refund_uncertain(attempt["attempt_id"], type(exc).__name__)
                done = False
            else:
                if success:
                    await commerce.finish_refund(attempt["attempt_id"], success=True)
                    done = True
                else:
                    await commerce.mark_refund_uncertain(attempt["attempt_id"], "Telegram returned false")
                    done = False
        else:
            await render(callback, "Провайдер этого платежа не поддерживает возврат из панели. Проверьте платёж в кабинете сервиса.", back_keyboard())
            return
        await render(callback,
            "<b>✅ Возврат выполнен</b>\n\nДеньги возвращены, начисление токенов отменено и записано в журнал." if done else
            "<b>⏳ Возврат ожидает подтверждения</b>\n\nПроверьте статус в журнале и кабинете платёжного сервиса. Не запускайте повторный возврат: результат запроса может прийти позже.",
            keyboard([("Открыть платёж", f"admin:payment:{draft['order_id']}")], [("← Админ-панель", "admin:panel")]))

    @router.message(AdminPanelStates.confirm)
    async def waiting_for_confirmation(message: Message):
        await render(message, "Используйте кнопку подтверждения в последнем предпросмотре или отмените действие.", back_keyboard())

    @router.callback_query(F.data.startswith("admin:") | F.data.in_({"admin_menu", "admin_stats", "admin_users", "admin_give_tokens", "admin_broadcast"}))
    async def unknown_admin_callback(callback: CallbackQuery, state: FSMContext):
        await panel(callback, state)

    return router


def payment_status(status: Any) -> str:
    return {
        "created": "создан", "pending": "ожидает оплаты", "paid": "оплачен", "expired": "истёк",
        "canceled": "отменён", "cancelled": "отменён", "refund_pending": "возврат на проверке",
        "refunded": "возвращён", "failed": "ошибка",
    }.get(str(status), str(status or "неизвестен"))
