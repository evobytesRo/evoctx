"""Store location resolution.

Precedence:
1. CONTEXT_STORE env var — explicit user override, always wins
2. ~/.context — legacy location; used only if a store.db already exists there,
   so pre-packaging installs keep working without migration
3. platformdirs user data dir — correct per-OS default
   (Windows: %LOCALAPPDATA%\\evoctx, macOS: ~/Library/Application Support/evoctx,
    Linux: ~/.local/share/evoctx)
"""
import os
from pathlib import Path

import platformdirs

APP_NAME = "evoctx"


def store_dir() -> Path:
    env = os.environ.get("CONTEXT_STORE")
    if env:
        return Path(env)

    legacy = Path.home() / ".context"
    if (legacy / "store.db").exists():
        return legacy

    return Path(platformdirs.user_data_dir(APP_NAME, appauthor=False))
