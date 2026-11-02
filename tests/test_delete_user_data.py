import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import aiosqlite

import db


class DeleteUserDataTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'data.sqlite'
        self.patch = patch.object(db, 'DB_NAME', str(self.path))
        self.patch.start()
        await db.init_db()
        async with aiosqlite.connect(db.DB_NAME) as conn:
            await conn.executescript('''
                CREATE TABLE user_consents(user_id INTEGER PRIMARY KEY, version TEXT, accepted_at TEXT);
                CREATE TABLE reading_cache(cache_key TEXT PRIMARY KEY, content TEXT, created_at TEXT);
                CREATE TABLE payment_orders(order_id TEXT PRIMARY KEY, user_id INTEGER, status TEXT);
                CREATE TABLE refund_attempts(attempt_id TEXT PRIMARY KEY, order_id TEXT);
                CREATE TABLE product_entitlements(user_id INTEGER, product TEXT, access_key TEXT);
                INSERT INTO users(telegram_id,name,referrer_id,tokens) VALUES(200,'User',100,40),(100,'Admin',NULL,50),(300,'Other',200,0);
                INSERT INTO user_consents VALUES(200,'v','now');
                INSERT INTO reading_cache VALUES('cache','private output','now');
                INSERT INTO payment_orders VALUES('order',200,'paid');
                INSERT INTO refund_attempts VALUES('refund','order');
                INSERT INTO product_entitlements VALUES(200,'matrix','date');
                INSERT INTO token_transactions(request_id,user_id,delta,balance_after,kind,reason,actor_id,created_at)
                VALUES('tx',200,40,40,'payment','test',NULL,'now');
            ''')
            await conn.commit()

    async def asyncTearDown(self):
        self.patch.stop()
        self.temp.cleanup()

    async def test_deletes_all_linked_records_and_clears_referrals(self):
        result = await db.delete_user_data(200)
        self.assertTrue(result['deleted'])
        async with aiosqlite.connect(db.DB_NAME) as conn:
            self.assertIsNone(await (await conn.execute('SELECT 1 FROM users WHERE telegram_id=200')).fetchone())
            self.assertEqual((await (await conn.execute('SELECT referrer_id FROM users WHERE telegram_id=300')).fetchone())[0], None)
            for table in ('user_consents','reading_cache','payment_orders','refund_attempts','product_entitlements','token_transactions'):
                self.assertEqual((await (await conn.execute(f'SELECT COUNT(*) FROM {table}')).fetchone())[0], 0)
            self.assertIsNotNone(await (await conn.execute('SELECT 1 FROM users WHERE telegram_id=100')).fetchone())

    async def test_repeat_delete_is_idempotent_and_never_deletes_admin(self):
        self.assertFalse((await db.delete_user_data(999))['deleted'])
        self.assertTrue((await db.delete_user_data(200))['deleted'])
        self.assertFalse((await db.delete_user_data(200))['deleted'])
        async with aiosqlite.connect(db.DB_NAME) as conn:
            self.assertIsNotNone(await (await conn.execute('SELECT 1 FROM users WHERE telegram_id=100')).fetchone())
