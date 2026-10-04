"""Where things live. PERSONAL_AI_DATA points the app at another data folder (e.g. for tests)."""

import os
from pathlib import Path

DATA_DIR = Path(os.environ.get("PERSONAL_AI_DATA", Path.home() / "personal-ai-data"))
NOTES_DIR = DATA_DIR / "notes"
SERVER = os.environ.get("LLAMA_SERVER", "http://127.0.0.1:8080")
