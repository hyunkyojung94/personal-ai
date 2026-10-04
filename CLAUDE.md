# CLAUDE.md

A personal AI assistant that runs entirely on the owner's own hardware: a local LLM (llama.cpp) answering from the owner's notes, with a mobile web app reachable over Tailscale. Learning-oriented side project.

**This repo is public.** Never commit personal data, notes, chat contents, keys, hostnames or Tailscale addresses. Personal context belongs in the private docs (below).

## Docs: read these first

- `docs/internal/ROADMAP.md`: phases and what's next.
- `docs/internal/DECISIONS.md`: why things are the way they are. Read it before changing architecture; add an entry when making a new decision.
- `docs/internal/` is a **separate private repo** (`hyunkyojung94/personal-ai-internal`), git-ignored here. It's missing in git worktrees and fresh clones; read it from the main checkout at `~/code/personal-ai/docs/internal/`, and commit changes to it from that folder.
- `docs/external/`: public docs, committed here.

## Layout

| File | Role |
|---|---|
| `serve.sh` | Starts llama-server (localhost:8080, API key from the data dir) |
| `assistant.py` | Builds the prompt (all notes + today's date) and streams from llama-server |
| `web.py` | FastAPI app: routes, plus middleware with the request security checks |
| `notes.py` | Notes as Markdown files in their own git repo: ETag concurrency, moves, journal |
| `chats.py` | SQLite conversations, messages, feedback; migrations via `PRAGMA user_version` |
| `static/index.html` | The whole web UI (vanilla JS, no build step) |
| `scripts/smoke_test.py` | End-to-end API checks against the running app (stdlib only) |

Data lives outside the repo in `~/personal-ai-data/` (`notes/`, `chats.sqlite`, `api-key`); model weights in `~/models/`.

## Run

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt   # once
./serve.sh                        # model server (loads ~15 GB)
.venv/bin/python web.py           # web app on :8000 (localhost + Tailscale address; start after Tailscale connects)
PORT=8001 .venv/bin/python web.py # a second copy for testing without touching the owner's running app
python3 scripts/smoke_test.py [http://127.0.0.1:8001]   # must pass before committing API changes
```

## Rules

- **Security checks on every endpoint.** The `RejectUntrustedRequests` middleware in `web.py` applies them to all routes (Host allowlist against DNS rebinding; Origin = Host and JSON bodies on writes against CSRF). Don't bypass or weaken it. Validate note paths with `notes._resolve`.
- **Never bind to all interfaces.** Only localhost and the Tailscale IP.
- **Policy in code, not prompts.** Tool permissions and approvals are enforced by the agent loop, never by instructions to the model (see the decision log).
- **Don't touch the owner's real data in tests.** Use throwaway notes (e.g. a `zz-test/` folder) and test conversations, then delete them. Verify the real notes are unchanged.
- **Dependencies:** only those in `requirements.txt` (pinned). Ask before adding one.
- **Verify changes for real:** run `scripts/smoke_test.py` (extend it when adding endpoints, including failure cases) and check the UI at phone width (375px).
- Restart `web.py` after Python changes; `static/index.html` is re-read on every request.
- Git: repo-local identity is configured; push with plain `git push` (remote uses the `github-personal` SSH alias). Commit only when asked.
