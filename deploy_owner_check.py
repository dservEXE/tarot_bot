"""One-off deployment over the existing NL mount; never prints credentials."""
import hashlib
import json
import os
import re
from datetime import datetime
from pathlib import Path

local = Path(__file__).resolve().parent
remote = Path('V:/tarot_bot')
stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
backup = remote / 'backups' / (stamp + '-owner-payments')
files = ['config.py', 'payment_router.py', 'yookassa_provider.py', 'main.py',
         'check_payments.py', '.env.example', 'docs/PAYMENT_OWNER_CHECK.md']
originals = {}

for name in files:
    path = remote / name
    originals[name] = path.read_bytes() if path.exists() else None

# Preserve other edits in the legal drafts and add the corrected email only.
for path in (remote / 'docs/legal').glob('*.md'):
    old = path.read_bytes()
    content = old.decode('utf-8-sig')
    if '@tarotDay_support' not in content:
        continue
    content = content.replace('z1n1ch@ilcoud.com', '')
    if '' not in content:
        content = content.replace('@tarotDay_support', '@tarotDay_support')
    name = path.relative_to(remote).as_posix()
    (local / name).write_text(content, encoding='utf-8', newline='\n')
    originals[name] = old
    files.append(name)

env_path = remote / '.env'
old_env = env_path.read_bytes()
env = old_env.decode('utf-8-sig')
updates = {'PAYMENTS_ENABLED': 'true', 'PAYMENTS_ADMIN_ONLY': 'true',
           'SUPPORT_CONTACT': '@tarotDay_support / '}
for key, value in updates.items():
    pattern = rf'(?m)^{key}=.*$'
    if re.search(pattern, env):
        env = re.sub(pattern, f'{key}={value}', env)
    else:
        env = env.rstrip() + f'\n{key}={value}\n'

def values(text):
    return dict(line.split('=', 1) for line in text.splitlines()
                if line and not line.lstrip().startswith('#') and '=' in line)

before, after = values(old_env.decode('utf-8-sig')), values(env)
assert {k: v for k, v in before.items() if k not in updates} == {k: v for k, v in after.items() if k not in updates}
assert before['YOOKASSA_TEST'].strip().lower() == 'false'
assert before['ADMIN_IDS'].strip() == '982023162'
assert before['PAYMENT_PROVIDER'].strip() == 'yookassa'

backup.mkdir(parents=True, exist_ok=False)
for name in files:
    destination = remote / name
    old = originals[name]
    assert (destination.read_bytes() if destination.exists() else None) == old, f'Concurrent change: {name}'
    if old is not None:
        saved = backup / name
        saved.parent.mkdir(parents=True, exist_ok=True)
        saved.write_bytes(old)
    content = (local / name).read_bytes()
    if name.endswith('.py'):
        compile(content, name, 'exec')
    temp = destination.with_name(destination.name + '.staged-' + stamp)
    temp.write_bytes(content)
    assert temp.read_bytes() == content
    os.replace(temp, destination)
    assert destination.read_bytes() == content

# Keep existing permissions; do not create a second plaintext copy of .env.
assert env_path.read_bytes() == old_env, 'Concurrent .env change'
env_path.write_bytes(env.encode('utf-8'))
assert env_path.read_bytes() == env.encode('utf-8')
manifest = {'files': files, 'settings_changed': updates,
            'previous_flags': {k: before.get(k) for k in updates},
            'hashes': {name: hashlib.sha256((remote / name).read_bytes()).hexdigest() for name in files},
            'service_restarted': False}
(backup / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
print(json.dumps({'uploaded_files': len(files), 'backup': str(backup),
                  'payments_enabled': True, 'payments_admin_only': True,
                  'original_credentials_preserved': True, 'service_restarted': False}, ensure_ascii=False))
