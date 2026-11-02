"""Owner-authorized one-off balance reset, executed locally on the Linux server."""
import json
import os
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path


def reset_balances_once(db_path, admin_ids, operation, backup_root):
    if not operation:
        return {'status': 'not_requested'}
    if not re.fullmatch(r'[a-zA-Z0-9_-]{1,80}', operation) or not admin_ids:
        raise ValueError('Invalid reset operation or empty admin list')
    path = Path(db_path).resolve(strict=True)
    marker = 'owner_balance_reset:' + operation
    now = datetime.now(timezone.utc).isoformat()
    with closing(sqlite3.connect(str(path), timeout=30)) as conn:
        conn.row_factory = sqlite3.Row
        try:
            conn.execute('BEGIN IMMEDIATE')
            if conn.execute('SELECT 1 FROM commerce_migrations WHERE name=?', (marker,)).fetchone():
                conn.rollback()
                return {'status': 'already_applied'}
            admins = set(admin_ids)
            rows = conn.execute('SELECT telegram_id, COALESCE(tokens,0) AS tokens FROM users').fetchall()
            admin_before = {row['telegram_id']: row['tokens'] for row in rows if row['telegram_id'] in admins}
            targets = [row for row in rows if row['telegram_id'] not in admins and row['tokens'] != 0]
            backup_dir = Path(backup_root) / (operation + '-' + datetime.now().strftime('%Y%m%d-%H%M%S-%f'))
            backup_dir.mkdir(parents=True, mode=0o700)
            backup_path = backup_dir / 'before-reset.sqlite'
            # A separate read connection can back up the committed snapshot while
            # BEGIN IMMEDIATE above prevents concurrent writers from changing it.
            with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=30)) as reader, \
                    closing(sqlite3.connect(str(backup_path))) as saved:
                reader.backup(saved)
                if saved.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                    raise RuntimeError('Backup integrity check failed')
            os.chmod(backup_path, 0o600)
            actor = min(admins)
            for row in targets:
                uid, balance = row['telegram_id'], row['tokens']
                conn.execute('UPDATE users SET tokens=0,updated_at=? WHERE telegram_id=?', (now, uid))
                conn.execute('''INSERT INTO token_transactions
                    (request_id,user_id,delta,balance_after,kind,reason,actor_id,created_at)
                    VALUES(?,?,?,?,?,?,?,?)''',
                    (marker + ':' + str(uid), uid, -balance, 0, 'admin_reset',
                     'Обнуление баланса по поручению владельца перед открытием оплаты', actor, now))
            for uid, balance in admin_before.items():
                if conn.execute('SELECT COALESCE(tokens,0) FROM users WHERE telegram_id=?', (uid,)).fetchone()[0] != balance:
                    raise RuntimeError('Admin balance changed')
            conn.execute('INSERT INTO commerce_migrations(name,applied_at) VALUES(?,?)', (marker, now))
            conn.commit()
            return {'status': 'applied', 'changed_users': len(targets), 'admins_preserved': len(admin_before),
                    'backup': str(backup_path)}
        except BaseException:
            conn.rollback()
            raise


def run_configured_actions():
    operation = os.getenv('RESET_NON_ADMIN_TOKENS_ONCE', '').strip()
    if not operation:
        return {'status': 'not_requested'}
    if os.name != 'posix':
        raise RuntimeError('Run the balance reset on the Linux server, not through a Windows network drive')
    from config import get_settings
    from db import DB_NAME
    return reset_balances_once(DB_NAME, get_settings().admin_ids, operation,
                               Path(__file__).resolve().parent / 'backups')


if __name__ == '__main__':
    from config import get_settings
    get_settings().validate_startup()
    print(json.dumps(run_configured_actions(), ensure_ascii=False))
