import asyncio
from contextlib import closing
import sqlite3
import tempfile
import unittest
from pathlib import Path

import commerce
import db


class CommerceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old_db = db.DB_NAME
        db.DB_NAME = str(Path(self.temp.name) / "test.db")
        await db.init_db()
        await commerce.init_commerce()
        await db.upsert_user(1, "Аня")
        await db.upsert_user(2, "Борис")
        await db.set_birth_date(1, "1994-05-12")

    async def asyncTearDown(self):
        db.DB_NAME = self.old_db
        self.temp.cleanup()

    async def fund(self, amount=1000):
        await commerce.adjust_tokens(999, 1, amount, "Тестовое начисление", str(amount))

    async def paid(self, pack="100", charge="charge-1"):
        order = await commerce.create_order(1, pack)
        await commerce.confirm_payment(1, order["payload"], "XTR", order["stars"], charge)
        return order

    def sql(self, query, args=()):
        with closing(sqlite3.connect(db.DB_NAME)) as conn, conn:
            cursor = conn.execute(query, args)
            return cursor.fetchall()

    async def assert_ledger_balanced(self, uid=1):
        total = self.sql("SELECT COALESCE(SUM(delta),0) FROM token_transactions WHERE user_id=?", (uid,))[0][0]
        self.assertEqual(total, await db.get_tokens(uid))

    def test_token_package_catalog_and_ruble_discounts(self):
        self.assertEqual(list(commerce.PACKAGES), ["10", "50", "100", "200", "250", "500", "1000"])
        self.assertEqual(commerce.PACKAGES["10"].amount_kopecks, 1000)
        self.assertEqual(commerce.PACKAGES["200"].amount_kopecks, 20000)
        self.assertEqual(commerce.PACKAGES["500"].amount_kopecks, 34900)
        self.assertEqual(commerce.PACKAGES["1000"].amount_kopecks, 69900)
        self.assertEqual(commerce.PACKAGES["500"].discount_percent, 30)
        self.assertEqual(commerce.PACKAGES["1000"].discount_percent, 30)
        self.assertEqual(commerce.PACKAGES["250"].savings_kopecks, 0)

    async def test_concurrent_legacy_debits_never_overspend(self):
        await self.fund(500)
        result = await asyncio.gather(*(db.spend_tokens(1, 100) for _ in range(20)))
        self.assertEqual(sum(result), 5)
        self.assertEqual(await db.get_tokens(1), 0)
        await self.assert_ledger_balanced()

    async def test_debit_rejects_negative_zero_and_float(self):
        for amount in (-1, 0, 1.5, True):
            with self.assertRaises(ValueError):
                await db.spend_tokens(1, amount)
        self.assertFalse(await db.spend_tokens(1234, 1))

    async def test_concurrent_profile_creation(self):
        result = await asyncio.gather(*(db.upsert_user(55, "Пользователь") for _ in range(10)))
        self.assertEqual(sum(result), 1)

    async def test_success_is_atomic_idempotent_and_charge_unique(self):
        order = await commerce.create_order(1, "100")
        results = await asyncio.gather(*(commerce.confirm_payment(1, order["payload"], "XTR", 100, "charge") for _ in range(10)))
        self.assertEqual(sum(r["credited"] for r in results), 1)
        self.assertEqual(await db.get_tokens(1), 100)
        second = await commerce.create_order(1, "100")
        with self.assertRaises(commerce.PaymentValidationError):
            await commerce.confirm_payment(1, second["payload"], "XTR", 100, "charge")
        with self.assertRaises(commerce.PaymentValidationError):
            await commerce.confirm_payment(1, order["payload"], "XTR", 100, "other-charge")
        await self.assert_ledger_balanced()

    async def test_tampered_invoice_never_credits(self):
        order = await commerce.create_order(1, "100")
        for uid, payload, currency, amount in (
            (2, order["payload"], "XTR", 100), (1, "fake", "XTR", 100),
            (1, order["payload"], "RUB", 100), (1, order["payload"], "XTR", 1),
            (1, order["payload"], "XTR", 100.0),
        ):
            with self.assertRaises(commerce.PaymentValidationError):
                await commerce.validate_pre_checkout(uid, payload, currency, amount)
            with self.assertRaises(commerce.PaymentValidationError):
                await commerce.confirm_payment(uid, payload, currency, amount, "charge")
        self.assertEqual(await db.get_tokens(1), 0)

    async def test_late_verified_payment_honored_after_expiry(self):
        order = await commerce.create_order(1, "100")
        self.sql("UPDATE payment_orders SET expires_at='2000-01-01T00:00:00+00:00' WHERE order_id=?", (order["order_id"],))
        with self.assertRaises(commerce.PaymentValidationError):
            await commerce.validate_pre_checkout(1, order["payload"], "XTR", 100)
        result = await commerce.confirm_payment(1, order["payload"], "XTR", 100, "late-charge")
        self.assertTrue(result["credited"])

    async def test_failed_ledger_insert_rolls_back_credit_and_order(self):
        order = await commerce.create_order(1, "100")
        self.sql("CREATE TRIGGER fail_ledger BEFORE INSERT ON token_transactions BEGIN SELECT RAISE(ABORT, 'test failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            await commerce.confirm_payment(1, order["payload"], "XTR", 100, "charge")
        self.assertEqual(await db.get_tokens(1), 0)
        self.assertEqual((await commerce.get_payment(order["order_id"]))["status"], "pending")

    async def test_concurrent_product_purchase_debits_once(self):
        await self.fund()
        results = await asyncio.gather(*(commerce.purchase_product(1, "matrix_report", "1994-05-12") for _ in range(10)))
        self.assertEqual(sum(r.status == "purchased" for r in results), 1)
        self.assertEqual(await db.get_tokens(1), 501)
        self.assertTrue(await commerce.has_entitlement(1, "matrix_report", "1994-05-12"))
        self.assertTrue(await commerce.has_entitlement(1, "matrix", "1994-05-12"))
        self.assertEqual((await commerce.purchase_product(1, "matrix", "1994-05-12")).status, "already_owned")
        await self.assert_ledger_balanced()

    async def test_birthdate_scopes_new_matrix_and_report(self):
        await self.fund()
        await commerce.purchase_product(1, "matrix_report", "1994-05-12")
        await db.set_birth_date(1, "2000-02-20")
        self.assertFalse(await commerce.has_entitlement(1, "matrix_report", "2000-02-20"))
        self.assertFalse(await commerce.has_entitlement(1, "matrix", "2000-02-20"))
        with self.assertRaises(commerce.CommerceError):
            await commerce.purchase_product(1, "matrix_report", "1994-05-12")
        self.assertEqual((await commerce.purchase_product(1, "matrix_report", "2000-02-20")).status, "purchased")
        await db.set_birth_date(1, "1994-05-12")
        self.assertEqual((await commerce.purchase_product(1, "matrix_report")).status, "already_owned")

    async def test_product_failure_rolls_back_debit(self):
        await self.fund()
        self.sql("CREATE TRIGGER fail_entitlement BEFORE INSERT ON product_entitlements BEGIN SELECT RAISE(ABORT, 'test failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            await commerce.purchase_product(1, "matrix")
        self.assertEqual(await db.get_tokens(1), 1000)
        self.assertEqual((await db.get_user(1))["matrix_access"], 0)
        await self.assert_ledger_balanced()

    async def test_subscription_and_daily_repeat_access(self):
        await self.fund()
        self.assertEqual((await commerce.purchase_product(1, "full_reading", "2026-09-08")).status, "purchased")
        self.assertEqual((await commerce.purchase_product(1, "full_reading", "2026-09-08")).status, "already_owned")
        self.assertEqual((await commerce.purchase_product(1, "full_reading", "2026-09-09")).status, "purchased")
        result = await asyncio.gather(*(commerce.purchase_product(1, "subscription") for _ in range(4)))
        self.assertEqual(sum(r.status == "purchased" for r in result), 1)
        self.assertEqual((await commerce.purchase_product(1, "match", "2026-09-09")).status, "already_owned")
        self.assertEqual(await db.get_tokens(1), 781)

    async def test_refund_pending_blocks_retry_and_debits_debt_once(self):
        order = await self.paid()
        await commerce.purchase_product(1, "full_reading", "2026-09-08")
        attempt = await commerce.begin_refund(99, order["order_id"], "Запрос пользователя")
        await commerce.mark_refund_uncertain(attempt["attempt_id"], "timeout")
        with self.assertRaises(commerce.CommerceError):
            await commerce.begin_refund(99, order["order_id"], "Повтор возврата")
        results = await asyncio.gather(commerce.finish_refund(attempt["attempt_id"], True), commerce.reconcile_refund("charge-1", 1, "XTR", 100))
        self.assertEqual(sum(r["refunded"] for r in results), 1)
        self.assertEqual(await db.get_tokens(1), -10)
        self.assertEqual((await commerce.purchase_product(1, "match", "2026-09-08")).status, "insufficient")
        self.assertEqual((await commerce.purchase_product(1, "full_reading", "2026-09-08")).status, "already_owned")
        replay = await commerce.confirm_payment(1, order["payload"], "XTR", 100, "charge-1")
        self.assertFalse(replay["credited"])
        await self.assert_ledger_balanced()

    async def test_refund_validates_amount_and_definitive_failure_retry(self):
        order = await self.paid()
        with self.assertRaises(commerce.PaymentValidationError):
            await commerce.reconcile_refund("charge-1", 2, "XTR", 100)
        attempt = await commerce.begin_refund(99, order["order_id"], "Возврат оплаты")
        await commerce.finish_refund(attempt["attempt_id"], False, "definitive rejection")
        retry = await commerce.begin_refund(99, order["order_id"], "Повтор после отказа")
        with self.assertRaises(commerce.CommerceError):
            await commerce.finish_refund(attempt["attempt_id"], True)
        await commerce.finish_refund(retry["attempt_id"], True)
        self.assertEqual(await db.get_tokens(1), 0)

    async def test_admin_operation_cannot_be_replayed_or_repurposed(self):
        first = await commerce.adjust_tokens(99, 1, 50, "Компенсация ошибки", "op-1")
        second = await commerce.adjust_tokens(99, 1, 50, "Компенсация ошибки", "op-1")
        self.assertTrue(first["changed"])
        self.assertFalse(second["changed"])
        with self.assertRaises(commerce.CommerceError):
            await commerce.adjust_tokens(99, 2, 50, "Компенсация ошибки", "op-1")
        rows = await commerce.list_transactions(1)
        self.assertEqual(rows[0]["actor_id"], 99)
        await self.assert_ledger_balanced()

    async def test_external_verified_payment_and_binding(self):
        order = await commerce.create_external_order(1, "250")
        self.assertEqual(order["amount_kopecks"], 25000)
        await commerce.attach_external_payment(order["order_id"], "yk-1", "https://yookassa.ru/checkout/test")
        await commerce.attach_external_payment(order["order_id"], "yk-1")
        with self.assertRaises(commerce.PaymentValidationError):
            await commerce.attach_external_payment(order["order_id"], "yk-other")
        for uid, amount, currency, paid, status in ((2,25000,"RUB",True,"succeeded"),(1,250,"RUB",True,"succeeded"),(1,25000,"USD",True,"succeeded"),(1,25000,"RUB",False,"pending")):
            with self.assertRaises(commerce.PaymentValidationError):
                await commerce.confirm_external_payment(order["order_id"], "yk-1", amount, currency, uid, paid, status)
        result = await commerce.confirm_external_payment(order["order_id"], "yk-1", 25000, "RUB", 1, True, "succeeded")
        replay = await commerce.confirm_external_payment(order["order_id"], "yk-1", 25000, "RUB", 1, True, "succeeded")
        self.assertTrue(result["credited"])
        self.assertFalse(replay["credited"])
        totals = await commerce.stats()
        self.assertEqual(totals["rub_gross_kopecks"], 25000)
        self.assertEqual(totals["stars_gross"], 0)
        await self.assert_ledger_balanced()

    async def test_external_cancel_never_credits_or_downgrades_paid_order(self):
        order = await commerce.create_external_order(1, "100")
        await commerce.attach_external_payment(order["order_id"], "cancel-case")
        await commerce.mark_external_canceled(order["order_id"])
        await commerce.mark_external_canceled(order["order_id"])
        self.assertEqual((await commerce.get_payment(order["order_id"]))["status"], "canceled")
        self.assertEqual(await db.get_tokens(1), 0)
        self.assertEqual(await commerce.list_pending_external_payments(), [])
        # If a verified success is delivered late, money is never discarded locally.
        paid = await commerce.confirm_external_payment(order["order_id"], "cancel-case", 10000, "RUB", 1, True, "succeeded")
        self.assertEqual(set(paid), {"credited", "balance", "tokens", "order_id"})
        await commerce.mark_external_canceled(order["order_id"])
        replay = await commerce.confirm_external_payment(order["order_id"], "cancel-case", 10000, "RUB", 1, True, "succeeded")
        self.assertFalse(replay["credited"])
        self.assertEqual((await commerce.get_payment(order["order_id"]))["status"], "paid")
        self.assertEqual(await db.get_tokens(1), 100)
        await self.assert_ledger_balanced()

    async def test_durable_request_replay_survives_reinitialization(self):
        order = await commerce.create_external_order(1, "100")
        body = {"metadata": {"user_id": "1", "order_id": order["order_id"]}, "amount": {"value": "100.00", "currency": "RUB"}}
        original = await commerce.save_external_request(order["order_id"], body)
        await commerce.init_commerce()
        replay = await commerce.save_external_request(order["order_id"], original["request_json"])
        self.assertEqual(replay["request_json"], original["request_json"])
        with self.assertRaises(commerce.PaymentValidationError):
            await commerce.save_external_request(order["order_id"], {"amount": "999.00"})
        await commerce.attach_external_payment(order["order_id"], "external-100")
        self.assertEqual((await commerce.get_payment_by_provider_id("external-100"))["order_id"], order["order_id"])
        self.assertEqual((await commerce.get_payment_by_payload(order["payload"]))["request_json"], original["request_json"])

    async def test_external_refund_persists_body_identifier_and_debits_once(self):
        order = await commerce.create_external_order(1, "100")
        await commerce.attach_external_payment(order["order_id"], "external-100")
        await commerce.confirm_external_payment(order["order_id"], "external-100", 10000, "RUB", 1, True, "succeeded")
        attempt = await commerce.begin_refund(99, order["order_id"], "Возврат по запросу")
        body = {"payment_id": "external-100", "amount": {"value": "100.00", "currency": "RUB"}}
        await commerce.save_refund_request(attempt["attempt_id"], body)
        await commerce.attach_external_refund(attempt["attempt_id"], "refund-100")
        await commerce.init_commerce()
        pending = await commerce.list_pending_external_refunds()
        self.assertEqual(pending[0]["provider_refund_id"], "refund-100")
        self.assertIn('"payment_id":"external-100"', pending[0]["refund_request_json"])
        with self.assertRaises(commerce.PaymentValidationError):
            await commerce.save_refund_request(attempt["attempt_id"], {"payment_id": "other"})
        with self.assertRaises(commerce.PaymentValidationError):
            await commerce.attach_external_refund(attempt["attempt_id"], "other-refund")
        result = await asyncio.gather(*(commerce.finish_refund(attempt["attempt_id"], True) for _ in range(5)))
        self.assertEqual(sum(r["refunded"] for r in result), 1)
        self.assertEqual(await db.get_tokens(1), 0)
        self.assertEqual(await commerce.list_pending_external_refunds(), [])
        self.assertEqual((await commerce.stats())["rub_refunded_kopecks"], 10000)
        await self.assert_ledger_balanced()

    async def test_failed_refund_ledger_insert_rolls_back_all_state(self):
        order = await self.paid()
        attempt = await commerce.begin_refund(99, order["order_id"], "Запрос возврата")
        self.sql("CREATE TRIGGER fail_refund_ledger BEFORE INSERT ON token_transactions WHEN NEW.kind='refund' BEGIN SELECT RAISE(ABORT, 'test failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            await commerce.finish_refund(attempt["attempt_id"], True)
        self.assertEqual(await db.get_tokens(1), 100)
        self.assertEqual((await commerce.get_payment(order["order_id"]))["status"], "refund_pending")
        self.sql("DROP TRIGGER fail_refund_ledger")
        self.assertTrue((await commerce.finish_refund(attempt["attempt_id"], True))["refunded"])
        await self.assert_ledger_balanced()

    async def test_reconciliation_rotation_prevents_old_pending_order_starvation(self):
        created = [await commerce.create_external_order(1, "100") for _ in range(4)]
        first = await commerce.list_pending_external_payments(limit=2)
        second = await commerce.list_pending_external_payments(limit=2)
        self.assertTrue({r["order_id"] for r in first}.isdisjoint({r["order_id"] for r in second}))
        self.assertEqual({r["order_id"] for r in first + second}, {r["order_id"] for r in created})

    async def test_legacy_migration_preserves_balance_and_lifetime_access(self):
        legacy = Path(self.temp.name) / "legacy.db"
        with closing(sqlite3.connect(legacy)) as conn, conn:
            conn.execute("CREATE TABLE users(telegram_id INTEGER PRIMARY KEY,name TEXT,birth_date TEXT,tokens INTEGER,matrix_access INTEGER)")
            conn.execute("INSERT INTO users VALUES(77,'Старый профиль','1980-01-01',723,1)")
        db.DB_NAME = str(legacy)
        await db.init_db()
        await commerce.init_commerce()
        await commerce.init_commerce()
        self.assertEqual(await db.get_tokens(77), 723)
        self.assertEqual((await db.get_user(77))["name"], "Старый профиль")
        self.assertTrue(await commerce.has_entitlement(77, "matrix", "1980-01-01"))
        self.assertTrue(await commerce.has_entitlement(77, "matrix", "2001-01-01"))
        self.assertFalse(await commerce.has_entitlement(77, "matrix_report", "1980-01-01"))
        await self.assert_ledger_balanced(77)


if __name__ == "__main__":
    unittest.main()
