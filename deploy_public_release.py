"""Deploy the authorized public checkout, UI and one-off reset to the NL mount."""
import hashlib
import json
import os
import re
from datetime import datetime
from pathlib import Path

local = Path(__file__).resolve().parent
remote = Path('V:/tarot_bot')
stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
files = ['legal_documents.py', 'launch_actions.py', 'config.py', 'payment_router.py',
         'check_payments.py', '.env.example', 'docs/PUBLIC_UPDATE_20260910.md', 'main.py']
originals = {name: (remote / name).read_bytes() if (remote / name).exists() else None for name in files}
for name in files:
    if name.endswith('.py'):
        compile((local / name).read_bytes(), name, 'exec')

env_path = remote / '.env'
original_env = env_path.read_bytes()
env = original_env.decode('utf-8-sig')
updates = {'PAYMENTS_ENABLED': 'true', 'PAYMENTS_ADMIN_ONLY': 'false',
           'PAYMENT_DOCUMENTS_IN_BOT': 'true',
           'RESET_NON_ADMIN_TOKENS_ONCE': 'public-release-20260910-v1'}
for key, value in updates.items():
    pattern = rf'(?m)^{key}=.*$'
    env = re.sub(pattern, f'{key}={value}', env) if re.search(pattern, env) else env.rstrip() + f'\n{key}={value}\n'

def values(text):
    return dict(line.split('=', 1) for line in text.splitlines()
                if line and not line.lstrip().startswith('#') and '=' in line)

before, after = values(original_env.decode('utf-8-sig')), values(env)
assert {k: v for k, v in before.items() if k not in updates} == {k: v for k, v in after.items() if k not in updates}
assert before['ADMIN_IDS'].strip() == '982023162'
assert before['YOOKASSA_TEST'].strip() == 'false'
backup = remote / 'backups' / (stamp + '-public-ui')
backup.mkdir(parents=True, exist_ok=False)
for name in files:
    destination, original = remote / name, originals[name]
    assert (destination.read_bytes() if destination.exists() else None) == original, f'Concurrent change: {name}'
    if original is not None:
        saved = backup / name
        saved.parent.mkdir(parents=True, exist_ok=True)
        saved.write_bytes(original)
    content = (local / name).read_bytes()
    temp = destination.with_name(destination.name + '.staged-' + stamp)
    temp.write_bytes(content)
    assert temp.read_bytes() == content
    os.replace(temp, destination)
    assert destination.read_bytes() == content

assert env_path.read_bytes() == original_env, 'Concurrent .env change'
env_path.write_bytes(env.encode('utf-8'))
assert env_path.read_bytes() == env.encode('utf-8')
manifest = {'files': files, 'settings_changed': updates,
            'previous_flags': {k: before.get(k) for k in updates},
            'hashes': {name: hashlib.sha256((remote / name).read_bytes()).hexdigest() for name in files},
            'service_restarted': False, 'balance_reset_executed': False}
(backup / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
print(json.dumps({'uploaded_files': len(files), 'backup': str(backup),
                  'public_checkout_configured': True, 'balance_reset': 'scheduled_on_next_start',
                  'service_restarted': False, 'original_credentials_preserved': True}, ensure_ascii=False))
