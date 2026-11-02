"""Upload the token package catalog and checkout UI to the mounted servers."""
import hashlib
import json
import os
from datetime import datetime
from pathlib import Path

local = Path(__file__).resolve().parent
files = [
    "commerce.py",
    "payment_router.py",
    "docs/PAYMENTS.md",
    "docs/PAYMENT_OWNER_CHECK.md",
    "docs/legal/purchase-terms.md",
]
stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
results = []

for mount in (Path("V:/tarot_bot"), Path("R:/tarot_bot")):
    backup = mount / "backups" / f"{stamp}-token-packages"
    backup.mkdir(parents=True, exist_ok=False)
    manifest = {"files": files, "service_restarted": False, "hashes": {}}
    for name in files:
        source = local / name
        target = mount / name
        payload = source.read_bytes()
        if name.endswith(".py"):
            compile(payload, name, "exec")
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            saved = backup / name
            saved.parent.mkdir(parents=True, exist_ok=True)
            saved.write_bytes(target.read_bytes())
        staged = target.with_name(target.name + ".staged-" + stamp)
        staged.write_bytes(payload)
        if staged.read_bytes() != payload:
            raise RuntimeError("Staged hash mismatch: " + str(target))
        os.replace(staged, target)
        if target.read_bytes() != payload:
            raise RuntimeError("Uploaded hash mismatch: " + str(target))
        manifest["hashes"][name] = hashlib.sha256(payload).hexdigest()
    (backup / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    results.append({"mount": str(mount), "backup": str(backup), "files": files})

print(json.dumps({"uploaded": results}, ensure_ascii=False))
