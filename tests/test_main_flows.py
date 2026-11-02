"""Full dispatcher user journeys with fake Telegram and temporary SQLite."""
from __future__ import annotations

import asyncio
import importlib
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.exceptions import TelegramNetworkError
from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendDocument, SendMessage, SendPhoto
from aiogram.types import CallbackQuery, Chat, InlineKeyboardMarkup, Message, Update, User

import app_services
import commerce
import db
import main


class FakeSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls = []
        self.fail_photo = False
        self.fail_report = False

    async def close(self):
        pass

    async def stream_content(self, *args, **kwargs):
        if False:
            yield b""

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        if isinstance(method, AnswerCallbackQuery):
            return True
        if isinstance(method, SendPhoto) and self.fail_photo:
            raise TelegramNetworkError(method=method, message="simulated delivery timeout")
        if isinstance(method, SendMessage) and self.fail_report and method.text == "<b>FULL_REPORT_PAGE_1</b>":
            raise TelegramNetworkError(method=method, message="simulated report timeout")
        if isinstance(method, (SendPhoto, SendDocument)):
            media = method.photo if isinstance(method, SendPhoto) else method.document
            if not Path(media.path).exists():
                raise AssertionError("Upload file removed before delivery")
        if isinstance(method, (SendMessage, EditMessageText, SendPhoto, SendDocument)):
            return Message(message_id=len(self.calls) + 1000, date=datetime.now(timezone.utc),
                chat=Chat(id=int(method.chat_id), type="private"),
                text=getattr(method, "text", None),
                reply_markup=method.reply_markup if isinstance(method.reply_markup, InlineKeyboardMarkup) else None).as_(bot)
        raise AssertionError(f"Unexpected Telegram method: {method}")


class MainFlows(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.env = patch.dict(os.environ, {"AI_ENABLED": "false", "PAYMENTS_ENABLED": "false", "ADMIN_IDS": "100"})
        self.env.start()
        self.app = importlib.reload(main)
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = patch.object(db, "DB_NAME", str(Path(self.tmp.name) / "main.sqlite"))
        self.db_path.start()
        await db.init_db()
        await commerce.init_commerce()
        await app_services.init_app()
        self.session = FakeSession()
        self.bot = Bot("123456:ABCDEF_fake_token_for_unit_tests", session=self.session)
        self.update_id = 0
        self.file_id = 0
        self.image = patch.object(self.app, "build_matrix_image", side_effect=lambda user: self.file(".png"))
        self.image_mock = self.image.start()
        self.report = patch.object(self.app, "build_matrix_report", return_value=["<b>FULL_REPORT_PAGE_1</b>", "FULL_REPORT_PAGE_2"])
        self.report_mock = self.report.start()
        self.report_file = patch.object(self.app, "build_matrix_report_html", side_effect=lambda user: self.file(".html"))
        self.report_file.start()

    async def asyncTearDown(self):
        self.image.stop()
        self.report.stop()
        self.report_file.stop()
        await self.app.dp.storage.close()
        await self.bot.session.close()
        self.db_path.stop()
        self.tmp.cleanup()
        self.env.stop()

    def file(self, suffix):
        self.file_id += 1
        path = Path(self.tmp.name) / f"render-{self.file_id}{suffix}"
        path.write_bytes(b"fake output for dispatcher-only test")
        return str(path)

    def message(self, actor=200, text="", group=False):
        return Message(message_id=self.update_id + 1, date=datetime.now(timezone.utc),
            from_user=User(id=actor, first_name="<User>", username=f"user_{actor}", is_bot=False),
            chat=Chat(id=-777 if group else actor, type="group" if group else "private"), text=text)

    async def send(self, text, actor=200, group=False):
        self.update_id += 1
        await self.app.dp.feed_update(self.bot, Update(update_id=self.update_id, message=self.message(actor, text, group)))

    async def click(self, data, actor=200, group=False):
        self.update_id += 1
        await self.app.dp.feed_update(self.bot, Update(update_id=self.update_id,
            callback_query=CallbackQuery(id=str(self.update_id), chat_instance="test", data=data,
                from_user=self.message(actor).from_user, message=self.message(actor, "panel", group))))

    def texts(self):
        return [method.text for method in self.session.calls if isinstance(method, (SendMessage, EditMessageText))]

    async def complete_profile(self, actor=200, tokens=1000):
        await db.upsert_user(actor, "<User>", f"user_{actor}")
        await app_services.accept_consent(actor)
        await db.set_birth_date(actor, "2000-02-29")
        await db.set_birth_time(actor, "07:30")
        await db.set_birth_place(actor, "Казань, Россия")
        if tokens:
            await commerce.adjust_tokens(100, actor, tokens, "Test seed", f"seed-{actor}")

    async def test_start_consent_and_complete_profile_fsm(self):
        await self.send("/start")
        self.assertFalse(await app_services.has_consent(200))
        self.assertTrue(any("Согласен" in text for text in self.texts()))
        self.assertIsNone((await db.get_user(200))["birth_date"])
        await self.click("profile:consent")
        for data in ("birth_year:2000", "birth_month:2", "birth_day:29", "birth_hour:7", "birth_minute:30"):
            await self.click(data)
        await self.send("Казань, Россия")
        user = await db.get_user(200)
        self.assertTrue(await app_services.has_consent(200))
        self.assertEqual((user["birth_date"], user["birth_time"], user["birth_place"]), ("2000-02-29", "07:30", "Казань, Россия"))
        self.assertTrue(self.app.profile_complete(user))
        await self.send("/profile")
        self.assertTrue(any("&lt;User&gt;" in text for text in self.texts()))
        await self.send("/menu")
        self.assertIn("Твой день", self.texts()[-1])

    async def test_group_updates_cannot_create_profile_or_trigger_purchases(self):
        await self.send("/start", group=True)
        self.assertIsNone(await db.get_user(200))
        await self.complete_profile()
        await self.click("matrix:buy:2000-02-29", group=True)
        self.assertEqual(await db.get_tokens(200), 1000)
        self.image_mock.assert_not_called()

    async def test_private_payment_callbacks_block_other_users(self):
        await self.complete_profile()
        with patch.dict(os.environ, {'PAYMENTS_ENABLED': 'true', 'PAYMENTS_ADMIN_ONLY': 'true',
                'SUPPORT_CONTACT': '@support', 'SELLER_NAME': 'Test', 'SELLER_INN': '123456789012',
                'YOOKASSA_SHOP_ID': '123', 'YOOKASSA_SECRET_KEY': 'fake',
                'PAYMENT_PROVIDER': 'yookassa', 'TERMS_URL': '', 'PRIVACY_URL': ''}), \
                patch('payment_router.start_checkout', AsyncMock()) as checkout:
            await self.click('tokens:buy:100')
            await self.click('payments:create:100')
            checkout.assert_not_called()
        self.assertEqual(await commerce.list_payments(user_id=200), [])

    async def test_public_checkout_exposes_readable_documents_and_confirmation(self):
        await self.complete_profile()
        with patch.dict(os.environ, {'PAYMENTS_ENABLED': 'true', 'PAYMENTS_ADMIN_ONLY': 'false',
                'PAYMENT_DOCUMENTS_IN_BOT': 'true', 'SUPPORT_CONTACT': '@support',
                'SELLER_NAME': 'Test', 'SELLER_INN': '123456789012',
                'YOOKASSA_SHOP_ID': '123', 'YOOKASSA_SECRET_KEY': 'fake',
                'PAYMENT_PROVIDER': 'yookassa', 'TERMS_URL': '', 'PRIVACY_URL': ''}):
            await self.click('tokens:buy:50')
            self.assertIn('50.00 ₽', self.texts()[-1])
            markup = self.session.calls[-1].reply_markup
            data = [b.callback_data for row in markup.inline_keyboard for b in row]
            self.assertIn('payments:terms', data)
            self.assertIn('payments:privacy', data)
            self.assertIn('payments:create:50', data)
            for callback in ('payments:terms', 'payments:privacy'):
                await self.click(callback)
                self.assertLess(len(self.texts()[-1]), 4096)
                self.assertNotIn('123456789012', self.texts()[-1])
                if callback == 'payments:privacy':
                    self.assertIn('/terms', self.texts()[-1])
                self.assertNotIn('href=""', self.texts()[-1])

    async def test_quick_keyboard_buttons_navigate_out_of_input_state(self):
        await self.complete_profile()
        for text, expected in [('🔮 Мой расклад', 'Твой день в деталях'),
                               ('✦ Матрица судьбы', 'Матрица'), ('💳 Пополнить токены', 'кошелёк')]:
            context = self.app.dp.fsm.get_context(bot=self.bot, chat_id=200, user_id=200)
            await context.set_state(self.app.Onboarding.birth_place)
            await self.send(text)
            self.assertTrue(any(expected.lower() in t.lower() for t in self.texts()[-2:]))
            self.assertIsNone(await context.get_state())

    async def test_private_owner_preview_explains_real_money_without_empty_links(self):
        await self.complete_profile(actor=100)
        with patch.dict(os.environ, {'PAYMENTS_ENABLED': 'true', 'PAYMENTS_ADMIN_ONLY': 'true',
                'SUPPORT_CONTACT': '@support', 'SELLER_NAME': 'Test', 'SELLER_INN': '123456789012',
                'YOOKASSA_SHOP_ID': '123', 'YOOKASSA_SECRET_KEY': 'fake', 'YOOKASSA_TEST': 'false',
                'PAYMENT_PROVIDER': 'yookassa', 'TERMS_URL': '', 'PRIVACY_URL': ''}):
            await self.click('tokens:buy:100', actor=100)
        text = self.texts()[-1]
        self.assertIn('100.00 ₽', text)
        self.assertIn('деньги будут списаны по-настоящему', text)
        self.assertNotIn('href=""', text)

    async def test_locked_matrix_and_report_buttons_do_not_deliver(self):
        await self.complete_profile()
        for data in ("matrix:open", "matrix:download", "matrix:report:open", "matrix:report:page:1", "matrix:report:download"):
            await self.click(data)
        self.assertEqual(await db.get_tokens(200), 1000)
        self.image_mock.assert_not_called()
        self.report_mock.assert_not_called()
        self.assertFalse(any(isinstance(method, (SendPhoto, SendDocument)) for method in self.session.calls))

    async def test_matrix_buy_duplicate_charges_once_and_keeps_download(self):
        await self.complete_profile()
        await self.click("matrix:buy:2000-02-29")
        await self.click("matrix:buy:2000-02-29")
        await self.click("matrix:download")
        self.assertEqual(await db.get_tokens(200), 651)
        self.assertTrue(await commerce.has_entitlement(200, "matrix", "2000-02-29"))
        purchases = [row for row in await commerce.list_transactions(user_id=200) if row["kind"] == "purchase"]
        self.assertEqual(len(purchases), 1)
        self.assertEqual(len([m for m in self.session.calls if isinstance(m, SendPhoto)]), 2)
        self.assertEqual(len([m for m in self.session.calls if isinstance(m, SendDocument)]), 1)
        self.assertFalse(list(Path(self.tmp.name).glob("render-*")))

    async def test_matrix_generation_failure_does_not_charge(self):
        await self.complete_profile()
        self.image_mock.side_effect = ValueError("bad render")
        await self.click("matrix:buy:2000-02-29")
        self.assertEqual(await db.get_tokens(200), 1000)
        self.assertFalse(await commerce.has_entitlement(200, "matrix", "2000-02-29"))
        self.assertNotIn(200, self.app._matrix_active)

    async def test_matrix_delivery_failure_preserves_purchase_without_second_charge(self):
        await self.complete_profile()
        self.session.fail_photo = True
        await self.click("matrix:buy:2000-02-29")
        self.assertEqual(await db.get_tokens(200), 651)
        self.assertTrue(await commerce.has_entitlement(200, "matrix", "2000-02-29"))
        self.assertNotIn(200, self.app._matrix_active)
        self.session.fail_photo = False
        await self.click("matrix:buy:2000-02-29")
        self.assertEqual(await db.get_tokens(200), 651)

    async def test_report_499_includes_chart_and_repeat_access_is_free(self):
        await self.complete_profile()
        await self.click("matrix:report:prompt")
        self.assertEqual(await db.get_tokens(200), 1000)
        await self.click("matrix:report:buy:2000-02-29")
        self.assertEqual(await db.get_tokens(200), 501)
        self.assertTrue(await commerce.has_entitlement(200, "matrix_report", "2000-02-29"))
        self.assertTrue(await commerce.has_entitlement(200, "matrix", "2000-02-29"))
        for data in ("matrix:report:buy:2000-02-29", "matrix:report:page:1", "matrix:report:download", "matrix:buy:2000-02-29"):
            await self.click(data)
        self.assertEqual(await db.get_tokens(200), 501)
        self.assertTrue(any(isinstance(m, SendDocument) for m in self.session.calls))
        self.assertTrue(any(isinstance(m, SendPhoto) for m in self.session.calls))

    async def test_report_delivery_failure_preserves_entitlement(self):
        await self.complete_profile()
        self.session.fail_report = True
        await self.click("matrix:report:buy:2000-02-29")
        self.assertEqual(await db.get_tokens(200), 501)
        self.assertTrue(await commerce.has_entitlement(200, "matrix_report", "2000-02-29"))
        self.session.fail_report = False
        await self.click("matrix:report:buy:2000-02-29")
        self.assertEqual(await db.get_tokens(200), 501)

    async def test_stale_birthday_purchase_never_charges_or_generates(self):
        await self.complete_profile()
        for data in ("matrix:buy:2001-01-01", "matrix:report:buy:2001-01-01"):
            await self.click(data)
        self.assertEqual(await db.get_tokens(200), 1000)
        self.image_mock.assert_not_called()
        self.report_mock.assert_not_called()
        self.assertIn("Дата в профиле изменилась", self.texts()[-1])

    async def test_changed_birthday_locks_new_material_and_preserves_old_purchase(self):
        await self.complete_profile()
        await self.click("matrix:report:buy:2000-02-29")
        await db.set_birth_date(200, "2001-01-01")
        self.image_mock.reset_mock()
        self.report_mock.reset_mock()
        await self.click("matrix:open")
        await self.click("matrix:report:open")
        self.image_mock.assert_not_called()
        self.report_mock.assert_not_called()
        self.assertEqual(await db.get_tokens(200), 501)
        await db.set_birth_date(200, "2000-02-29")
        await self.click("matrix:report:open")
        self.assertTrue(self.report_mock.called)
        self.assertEqual(await db.get_tokens(200), 501)

    async def test_subscription_double_click_and_delivery_cancel_preserve_paid_access(self):
        await self.complete_profile(tokens=199)
        await self.click("sub:buy")
        expiry = (await db.get_user(200))["subscription_until"]
        await self.click("sub:buy")
        self.assertEqual(await db.get_tokens(200), 0)
        self.assertEqual((await db.get_user(200))["subscription_until"], expiry)
        await self.click("reading:full_pay")
        self.assertEqual(await db.get_tokens(200), 0)
        self.assertTrue(any("Полный расклад" in text for text in self.texts()))
        await self.click("sub:cancel")
        user = await db.get_user(200)
        self.assertEqual(user["notify_enabled"], 0)
        self.assertEqual(user["subscription_until"], expiry)
        self.assertTrue(self.app.subscription_active(user))

    async def test_full_reading_duplicate_charge_once(self):
        await self.complete_profile(tokens=20)
        await self.click("reading:full_pay")
        await self.click("reading:full_pay")
        self.assertEqual(await db.get_tokens(200), 10)
        purchases = [row for row in await commerce.list_transactions(user_id=200) if row["kind"] == "purchase"]
        self.assertEqual(len(purchases), 1)

    async def test_unfunded_full_reading_does_not_call_paid_ai_generation(self):
        await self.complete_profile(tokens=0)
        reading = self.app.generate_daily_reading(await db.get_user(200), self.app.current_local_date(await db.get_user(200)))
        with patch.object(self.app, "get_daily_reading", new_callable=AsyncMock, return_value=reading) as generation:
            await self.click("reading:full_pay")
            generation.assert_not_awaited()
        self.assertEqual(await db.get_tokens(200), 0)

    async def test_resume_profile_back_restores_birth_year_before_month_selection(self):
        await db.upsert_user(200, "Имя", "user_200")
        await app_services.accept_consent(200)
        await db.set_birth_date(200, "2000-02-29")
        await self.click("profile:continue")
        await self.click("back:birth_day")
        await self.click("birth_month:2")
        await self.click("birth_day:28")
        self.assertEqual((await db.get_user(200))["birth_date"], "2000-02-28")


if __name__ == "__main__":
    unittest.main()
