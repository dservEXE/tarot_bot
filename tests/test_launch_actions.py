import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from launch_actions import reset_balances_once


class BalanceResetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'users.db'
        self.backups = Path(self.temp.name) / 'backups'
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE users(telegram_id INTEGER PRIMARY KEY,tokens INTEGER,updated_at TEXT);
                CREATE TABLE commerce_migrations(name TEXT PRIMARY KEY,applied_at TEXT);
                CREATE TABLE token_transactions(request_id TEXT UNIQUE,user_id INTEGER,delta INTEGER,
                    balance_after INTEGER,kind TEXT,reason TEXT,actor_id INTEGER,created_at TEXT);
                CREATE TABLE product_entitlements(user_id INTEGER,product TEXT);
                INSERT INTO users VALUES(1,200,''),(2,-10,''),(3,0,''),(99,800,''),(100,900,'');
                INSERT INTO product_entitlements VALUES(1,'matrix');
            ''')

    def run_reset(self):
        return reset_balances_once(self.path, {99, 100}, 'release-20260910', self.backups)

    def balances(self):
        with closing(sqlite3.connect(self.path)) as conn:
            return dict(conn.execute('SELECT telegram_id,tokens FROM users'))

    def test_reset_preserves_admins_entitlements_and_backed_up_balances(self):
        before = self.balances()
        result = self.run_reset()
        self.assertEqual(result['changed_users'], 2)
        self.assertEqual(self.balances(), {1: 0, 2: 0, 3: 0, 99: 800, 100: 900})
        with closing(sqlite3.connect(result['backup'])) as saved:
            self.assertEqual(dict(saved.execute('SELECT telegram_id,tokens FROM users')), before)
            self.assertEqual(saved.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
        with closing(sqlite3.connect(self.path)) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM product_entitlements').fetchone()[0], 1)
            self.assertEqual(dict(conn.execute('SELECT user_id,delta FROM token_transactions')), {1: -200, 2: 10})

    def test_restart_does_not_erase_new_topups_or_new_users(self):
        self.run_reset()
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute('UPDATE users SET tokens=50 WHERE telegram_id=1')
            conn.execute("INSERT INTO users VALUES(4,75,'')")
        self.assertEqual(self.run_reset()['status'], 'already_applied')
        self.assertEqual(self.balances()[1], 50)
        self.assertEqual(self.balances()[4], 75)

    def test_ledger_failure_rolls_back_every_balance_and_marker(self):
        before = self.balances()
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute("CREATE TRIGGER fail_ledger BEFORE INSERT ON token_transactions BEGIN SELECT RAISE(ABORT,'test'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.run_reset()
        self.assertEqual(self.balances(), before)
        with closing(sqlite3.connect(self.path)) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM commerce_migrations').fetchone()[0], 0)

    def test_empty_admins_fail_before_changes(self):
        with self.assertRaises(ValueError):
            reset_balances_once(self.path, set(), 'release-20260910', self.backups)
        self.assertFalse(self.backups.exists())
