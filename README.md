# personal-ai

A personal AI assistant that runs entirely on my own hardware.

Current stage: a web chat that puts every note into the prompt (no retrieval yet), plus notes editing and chat history.

## Layout

- Code: this repo. Needs Python 3, `llama-server` (`brew install llama.cpp`), and the packages in `requirements.txt` (FastAPI, Uvicorn) in a virtualenv.
- Data: `~/personal-ai-data/` (override with `PERSONAL_AI_DATA`). Kept outside the repo so personal notes and the API key can't be committed.
  - `notes/` — Markdown notes and journal entries, any subfolders. Its own git repo: every save/delete from the web app is a commit, so `git -C ~/personal-ai-data/notes log` shows history and anything can be restored.
  - `chats.sqlite` — conversations, messages (with model/think/token stats) and feedback (rating, reasons, comment, corrected answer). Inspect with `sqlite3 ~/personal-ai-data/chats.sqlite`.
  - `api-key` — created by `serve.sh` on first run.
- Model: `~/models/gemma-4-26B_q4_0-it.gguf` (override with `MODEL`).
- Docs: `docs/external/` (public, in this repo) and `docs/internal/` (a separate private repo, git-ignored here). On a new machine, clone it into place: `git clone git@github-personal:hyunkyojung94/personal-ai-internal.git docs/internal`.

## Setup (once)

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

## Run

```bash
./serve.sh               # terminal 1: model server on 127.0.0.1:8080
.venv/bin/python web.py  # terminal 2: web app on :8000 (localhost + Tailscale address)
```

Check everything end to end (with both running): `python3 scripts/smoke_test.py`. It uses throwaway notes and conversations, cleans up, and verifies real notes are unchanged.

From the phone: install Tailscale on both devices, signed in to the same account, then open `http://<mac's Tailscale name>:8000`. Start `web.py` after Tailscale is connected, since it looks up the Tailscale address once at startup.
