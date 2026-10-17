"""Create a local .env with fresh secrets, without printing them or overwriting a file."""

import os
import secrets
from pathlib import Path

root = Path(__file__).resolve().parent.parent
target = root / ".env"
if target.exists():
    raise SystemExit(".env already exists; no changes made")
text = (root / ".env.example").read_text()
password = secrets.token_hex(24)
text = text.replace("POSTGRES_PASSWORD=\n", f"POSTGRES_PASSWORD={password}\n")
text = text.replace(":CHANGE_ME@", f":{password}@")
for name in ("TELEGRAM_SECRET", "MEDIA_SECRET"):
    text = text.replace(name + "=\n", name + "=" + secrets.token_urlsafe(32) + "\n")
fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, "w") as file:
    file.write(text)
print("Created .env (mode 0600). Set bot credentials, domain, admin IDs and retention hours.")
