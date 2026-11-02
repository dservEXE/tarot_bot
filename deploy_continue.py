"""Upload the completed privacy, referral and admin-delete update to the NL mount."""
import hashlib
import json
import os
from datetime import datetime
from pathlib import Path

local = Path(__file__).resolve().parent
remote = Path('V:/tarot_bot')
stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
files = ['admin_panel.py', 'db.py', 'legal_documents.py', 'config.py',
         'payment_router.py', 'main.py']
backup = remote / 'backups' / (stamp + '-privacy-admin-delete')
backup.mkdir(parents=True, exist_ok=False)
manifest = {'files': files, 'service_restarted': False, 'hashes': {}}
for name in files:
    source = local / name
    target = remote / name
    payload = source.read_bytes()
    if name.endswith('.py'):
        compile(payload, name, 'exec')
    original = target.read_bytes()
    saved = backup / name
    saved.write_bytes(original)
    staged = target.with_name(target.name + '.staged-' + stamp)
    staged.write_bytes(payload)
    if staged.read_bytes() != payload:
        raise RuntimeError('Staged hash mismatch: ' + name)
    os.replace(staged, target)
    if target.read_bytes() != payload:
        raise RuntimeError('Uploaded hash mismatch: ' + name)
    manifest['hashes'][name] = hashlib.sha256(payload).hexdigest()
(backup / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
print(json.dumps({'uploaded': files, 'backup': str(backup), 'service_restarted': False}, ensure_ascii=False))
