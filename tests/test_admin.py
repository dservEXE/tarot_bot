"""Dispatch real aiogram updates against temporary SQLite; never contact Telegram."""
from __future__ import annotations

import asyncio
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.exceptions import TelegramNetworkError, TelegramRetryAfter
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import AnswerCallbackQuery, EditMessageText, RefundStarPayment, SendMessage
from aiogram.types import CallbackQuery, Chat, Message, Update, User

import admin_panel
import commerce
import db


class FakeTelegramSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls = []
        self.refund_error = None

    async def close(self):
        pass

    async def stream_content(self, *args, **kwargs):
        if False:
            yield b""

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        if isinstance(method, AnswerCallbackQuery):
            return True
        if isinstance(method, RefundStarPayment):
            if self.refund_error:
                raise self.refund_error
            return True
        if isinstance(method, (SendMessage, EditMessageText)):
            return Message(message_id=999, date=datetime.now(timezone.utc),
                chat=Chat(id=int(method.chat_id), type="private"), text=method.text,
                reply_markup=method.reply_markup)
        raise AssertionError(f"Unexpected Telegram method: {method}")


class AdminIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path_patch = patch.object(db, "DB_NAME", str(Path(self.tmp.name) / "test.sqlite"))
        self.path_patch.start()
        await db.init_db()
        await commerce.init_commerce()
        await db.upsert_user(100, "Владелец", "owner")
        await db.upsert_user(101, "Второй администратор", "other_admin")
        await db.upsert_user(200, "<b>Клиент</b>", "customer")
        self.session = FakeTelegramSession()
        self.bot = Bot("123456:ABCDEF_fake_token_for_unit_tests", session=self.session)
        self.dp = Dispatcher(storage=MemoryStorage())
        self.dp.include_router(admin_panel.make_admin_router({100, 101}))
        self.sequence = 0

    async def asyncTearDown(self):
        await self.dp.storage.close()
        await self.bot.session.close()
        self.path_patch.stop()
        self.tmp.cleanup()

    def message(self, actor=100, text="", chat_id=None, chat_type="private"):
        return Message(message_id=self.sequence + 1, date=datetime.now(timezone.utc),
            from_user=User(id=actor, is_bot=False, first_name="Test"),
            chat=Chat(id=actor if chat_id is None else chat_id, type=chat_type), text=text)

    async def send(self, text, actor=100, chat_id=None, chat_type="private"):
        self.sequence += 1
        await self.dp.feed_update(self.bot, Update(update_id=self.sequence,
            message=self.message(actor, text, chat_id, chat_type)))

    async def click(self, data, actor=100, chat_id=None, chat_type="private"):
        self.sequence += 1
        callback = CallbackQuery(id=str(self.sequence), chat_instance="test",
            from_user=User(id=actor, is_bot=False, first_name="Test"),
            message=self.message(actor, "panel", chat_id, chat_type), data=data)
        await self.dp.feed_update(self.bot, Update(update_id=self.sequence, callback_query=callback))

    def last_text(self):
        return next(method.text for method in reversed(self.session.calls) if isinstance(method, (SendMessage, EditMessageText)))

    def last_confirmation(self):
        for method in reversed(self.session.calls):
            markup = getattr(method, "reply_markup", None)
            if markup:
                for row in markup.inline_keyboard:
                    for button in row:
                        if (button.callback_data or "").startswith("admin:confirm:"):
                            return button.callback_data
        raise AssertionError("No confirmation button")

    async def state_for(self, user_id=100):
        return self.dp.fsm.get_context(bot=self.bot, chat_id=user_id, user_id=user_id)

    async def test_admin_requires_authorized_private_chat(self):
        await self.send("/admin", actor=200)
        self.assertIn("только администратору", self.last_text())
        before = len(self.session.calls)
        await self.send("/admin", actor=100, chat_id=-123, chat_type="group")
        self.assertEqual(len(self.session.calls), before)
        await self.click("admin:stats", actor=200)
        self.assertIsInstance(self.session.calls[-1], AnswerCallbackQuery)
        self.assertTrue(self.session.calls[-1].show_alert)
        await self.send("/admin")
        self.assertIn("Управление ботом", self.last_text())

    async def test_guard_also_protects_forged_fsm_state(self):
        state = await self.state_for(200)
        await state.set_state(admin_panel.AdminPanelStates.grant_amount)
        await state.set_data({"user_id": 200})
        await self.send("99999", actor=200)
        self.assertEqual((await db.get_user(200))["tokens"], 0)
        self.assertIn("только администратору", self.last_text())

    async def test_grant_preview_confirmation_and_double_click_are_audited_once(self):
        await self.click("admin:give_tokens")
        await self.send("@customer")
        await self.send("499")
        await self.send("Тестовая компенсация <tag>")
        self.assertEqual(await db.get_tokens(200), 0)
        self.assertIn("&lt;tag&gt;", self.last_text())
        confirmation = self.last_confirmation()
        await asyncio.gather(self.click(confirmation), self.click(confirmation))
        self.assertEqual(await db.get_tokens(200), 499)
        transactions = await commerce.list_transactions(user_id=200)
        self.assertEqual(len(transactions), 1)
        self.assertEqual(transactions[0]["actor_id"], 100)
        self.assertEqual(transactions[0]["reason"], "Тестовая компенсация <tag>")

    async def test_stolen_and_expired_confirmation_cannot_change_balance(self):
        await self.click("admin:grant:200")
        await self.send("10")
        await self.send("Компенсация")
        confirmation = self.last_confirmation()
        await self.click(confirmation, actor=101)
        self.assertEqual(await db.get_tokens(200), 0)
        state = await self.state_for()
        await state.update_data(expires=time.time() - 1)
        await self.click(confirmation)
        self.assertEqual(await db.get_tokens(200), 0)

    async def test_invalid_grants_do_not_advance_to_confirmation(self):
        await self.click("admin:grant:200")
        for raw in ("-1", "0", "100001", "1.5", "1e5", "١٠"):
            await self.send(raw)
            self.assertEqual(await db.get_tokens(200), 0)
            self.assertEqual(await (await self.state_for()).get_state(), admin_panel.AdminPanelStates.grant_amount.state)

    async def test_user_list_paginates_and_escapes_profile(self):
        for uid in range(201, 215):
            await db.upsert_user(uid, f"Имя {uid}")
        await self.click("admin:users:0")
        markup = self.session.calls[-1].reply_markup
        user_buttons = [button for row in markup.inline_keyboard for button in row if (button.callback_data or "").startswith("admin:user:")]
        self.assertEqual(len(user_buttons), 10)
        self.assertIn("из 2", self.last_text())
        await self.click("admin:users:1")
        self.assertIn("Страница 2 из 2", self.last_text())
        await self.click("admin:user:200")
        self.assertIn("&lt;b&gt;Клиент&lt;/b&gt;", self.last_text())
        self.assertIn("Профиль:</b> не завершён", self.last_text())

    async def test_delete_user_requires_confirmation_and_removes_current_records(self):
        await db.set_birth_date(200, "2000-01-01")
        await db.set_birth_time(200, "12:00")
        await db.set_birth_place(200, "Москва")
        await commerce.adjust_tokens(100, 200, 25, "Тест удаления", "delete-test")
        await self.click("admin:user:200")
        await self.click("admin:delete:200")
        self.assertIn("Удалить все данные пользователя", self.last_text())
        confirmation = self.last_confirmation()
        self.assertEqual(await db.get_tokens(200), 25)
        await self.click(confirmation)
        self.assertIn("Данные удалены", self.last_text())
        self.assertIsNone(await db.get_user(200))
        self.assertIsNotNone(await db.get_user(100))

    async def test_admin_data_cannot_be_deleted_from_panel(self):
        await self.click("admin:user:100")
        await self.click("admin:delete:100")
        self.assertIn("администратора", self.last_text())
        self.assertIsNotNone(await db.get_user(100))

    async def test_broadcast_requires_confirmation_and_respects_notifications(self):
        await db.set_notify_enabled(100, False)
        await db.set_notify_enabled(101, False)
        await self.click("admin:broadcast")
        await self.send("Сервис <b>обновлён</b>")
        self.assertFalse(any(isinstance(m, SendMessage) and m.chat_id == 200 for m in self.session.calls))
        self.assertIn("&lt;b&gt;обновлён&lt;/b&gt;", self.last_text())
        confirmation = self.last_confirmation()
        await self.click(confirmation)
        sends = [m for m in self.session.calls if isinstance(m, SendMessage) and m.chat_id == 200]
        self.assertEqual(len(sends), 1)
        self.assertIsNone(sends[0].parse_mode)
        self.assertEqual(sends[0].text, "Сервис <b>обновлён</b>")
        await self.click(confirmation)
        self.assertEqual(len([m for m in self.session.calls if isinstance(m, SendMessage) and m.chat_id == 200]), 1)

    async def test_broadcast_preference_changed_after_preview_is_respected(self):
        await db.set_notify_enabled(100, False)
        await db.set_notify_enabled(101, False)
        await self.click("admin:broadcast")
        await self.send("Сообщение о сервисе")
        confirmation = self.last_confirmation()
        await db.set_notify_enabled(200, False)
        await self.click(confirmation)
        self.assertFalse(any(isinstance(m, SendMessage) and m.chat_id == 200 for m in self.session.calls))

    async def test_cancel_removes_authorization_to_confirm(self):
        await self.click("admin:grant:200")
        await self.send("50")
        await self.send("Проверка отмены")
        confirmation = self.last_confirmation()
        await self.send("/cancel")
        await self.click(confirmation)
        self.assertEqual(await db.get_tokens(200), 0)

    async def test_broadcast_only_retries_explicit_rate_limit(self):
        mock_bot = AsyncMock()
        method = SendMessage(chat_id=200, text="Текст")
        mock_bot.send_message.side_effect = [TelegramRetryAfter(method=method, message="slow", retry_after=1), True]
        with patch("admin_panel.asyncio.sleep", new_callable=AsyncMock):
            result = await admin_panel.deliver_broadcast(mock_bot, [200], "Текст")
        self.assertEqual(result["sent"], 1)
        self.assertEqual(mock_bot.send_message.await_count, 2)
        mock_bot.send_message.reset_mock()
        mock_bot.send_message.side_effect = TelegramNetworkError(method=method, message="timeout")
        with patch("admin_panel.asyncio.sleep", new_callable=AsyncMock):
            result = await admin_panel.deliver_broadcast(mock_bot, [200], "Текст")
        self.assertEqual(result["failed"], 1)
        self.assertEqual(mock_bot.send_message.await_count, 1)

    async def test_uncertain_star_refund_is_reserved_without_debit_or_retry(self):
        payment = {"order_id": "order-test", "user_id": 200, "provider": "telegram_stars", "status": "paid",
            "stars": 50, "tokens": 50, "currency": "XTR", "telegram_charge_id": "charge-test"}
        with patch.object(commerce, "get_payment", new_callable=AsyncMock, return_value=payment), \
             patch.object(commerce, "begin_refund", new_callable=AsyncMock, return_value={**payment, "attempt_id": "attempt"}) as begin, \
             patch.object(commerce, "mark_refund_uncertain", new_callable=AsyncMock) as uncertain, \
             patch.object(commerce, "finish_refund", new_callable=AsyncMock) as finish:
            self.session.refund_error = TelegramNetworkError(method=RefundStarPayment(user_id=200, telegram_payment_charge_id="charge-test"), message="timeout")
            await self.click("admin:refund:order-test")
            await self.send("Возврат по обращению")
            confirmation = self.last_confirmation()
            self.assertFalse(any(isinstance(m, RefundStarPayment) for m in self.session.calls))
            await self.click(confirmation)
            await self.click(confirmation)
            self.assertEqual(begin.await_count, 1)
            uncertain.assert_awaited_once()
            finish.assert_not_awaited()
            self.assertEqual(len([m for m in self.session.calls if isinstance(m, RefundStarPayment)]), 1)

    async def test_yookassa_refund_uses_provider_and_never_telegram(self):
        payment = {"order_id": "rub-order", "user_id": 200, "provider": "yookassa", "status": "paid",
            "amount_kopecks": 5000, "tokens": 50, "currency": "RUB", "provider_payment_id": "yoo-id"}
        with patch.object(commerce, "get_payment", new_callable=AsyncMock, return_value=payment), \
             patch("yookassa_provider.refund_order", new_callable=AsyncMock, return_value={"status": "refunded"}) as provider:
            await self.click("admin:refund:rub-order")
            await self.send("Возврат по обращению")
            self.assertIn("50.00 ₽", self.last_text())
            provider.assert_not_awaited()
            await self.click(self.last_confirmation())
            provider.assert_awaited_once_with(order_id="rub-order", admin_id=100, reason="Возврат по обращению")
            self.assertIn("Возврат выполнен", self.last_text())
            self.assertFalse(any(isinstance(m, RefundStarPayment) for m in self.session.calls))

    async def test_canceled_yookassa_refund_is_not_reported_as_pending(self):
        payment = {"order_id": "rub-order", "user_id": 200, "provider": "yookassa", "status": "paid",
            "amount_kopecks": 5000, "tokens": 50, "currency": "RUB", "provider_payment_id": "yoo-id"}
        with patch.object(commerce, "get_payment", new_callable=AsyncMock, return_value=payment), \
             patch("yookassa_provider.refund_order", new_callable=AsyncMock, return_value={"status": "paid"}):
            await self.click("admin:refund:rub-order")
            await self.send("Возврат по обращению")
            await self.click(self.last_confirmation())
            self.assertIn("Возврат отменён", self.last_text())
            self.assertIn("баланс токенов сохранён", self.last_text())


if __name__ == "__main__":
    unittest.main()
