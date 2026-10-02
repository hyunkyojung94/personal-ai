# personal-ai

A personal AI assistant that runs entirely on my own hardware.

Current stage: a chat (terminal or web) that puts every note into the prompt (no retrieval yet).

## Layout

- Code: this repo. No dependencies beyond Python 3 and `llama-server` (`brew install llama.cpp`).
- Data: `~/personal-ai-data/` (override with `PERSONAL_AI_DATA`). Kept outside the repo so personal notes and the API key can't be committed.
  - `notes/` — Markdown notes and journal entries, any subfolders. Its own git repo: every save/delete from the web app is a commit, so `git -C ~/personal-ai-data/notes log` shows history and anything can be restored.
  - `api-key` — created by `serve.sh` on first run.
- Model: `~/models/gemma-4-26B_q4_0-it.gguf` (override with `MODEL`).

## Run

```bash
./serve.sh        # terminal 1: model server on 127.0.0.1:8080
python3 web.py    # terminal 2: web app on :8000 (localhost + Tailscale address)
python3 chat.py   # or: terminal chat
```

From the phone: install Tailscale on both devices, signed in to the same account, then open `http://<mac's Tailscale name>:8000`. Start `web.py` after Tailscale is connected, since it looks up the Tailscale address once at startup.
