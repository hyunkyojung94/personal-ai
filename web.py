"""Web app: a mobile-friendly chat and notes page for your own devices.

Listens on localhost and, if Tailscale is running, on this machine's Tailscale
address. Never on all interfaces, so people on the same café Wi-Fi can't reach it.
"""

import json
import os
import socket
import subprocess
import time
import urllib.error
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Header
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field
from starlette.datastructures import Headers

import chats
import notes
from assistant import chat

PORT = int(os.environ.get("PORT", "8000"))
INDEX = Path(__file__).parent / "static" / "index.html"
# The Mac App Store version of Tailscale doesn't put its CLI on PATH.
TAILSCALE_CLIS = ["tailscale", "/Applications/Tailscale.app/Contents/MacOS/Tailscale"]
ERROR_STATUS = {
    notes.InvalidPath: 400,
    notes.NotFound: 404,
    notes.Conflict: 409,
    notes.DestinationExists: 409,
    notes.PreconditionRequired: 428,
    chats.NotFound: 404,
}

# Names this server may be reached by, filled in at startup.
allowed_hosts = {"127.0.0.1", "localhost"}


def tailscale_self():
    """This machine's Tailscale IPv4 address and DNS names, or (None, [])."""
    for cli in TAILSCALE_CLIS:
        try:
            result = subprocess.run([cli, "status", "--json"], capture_output=True, text=True)
        except FileNotFoundError:
            continue
        if result.returncode != 0:
            continue
        me = json.loads(result.stdout)["Self"]
        ipv4 = next((ip for ip in me["TailscaleIPs"] if "." in ip), None)
        full_name = me["DNSName"].rstrip(".")  # e.g. my-mac.tail1234.ts.net
        return ipv4, [full_name, full_name.split(".")[0]]
    return None, []


def untrusted_request_error(headers, method):
    """Why a request could come from a malicious web page, or None if it's fine.

    Host allowlist (every request): blocks DNS rebinding, where an attacker's
    domain is re-pointed at this machine so the browser treats this app as the
    attacker's own site.
    Origin must match Host (writes): blocks cross-site request forgery. The
    same-origin policy stops other sites reading responses, but not sending
    requests, and a write only needs to be sent to do damage.
    JSON body (writes): another site can't send that without a CORS preflight,
    which this server never approves.
    """
    host = headers.get("host", "")
    if host.rsplit(":", 1)[0] not in allowed_hosts:
        return 403, "Unknown Host"
    if method in ("GET", "HEAD"):
        return None
    origin = headers.get("origin")
    # Browsers always send Origin on these requests; tools like curl don't.
    if origin is not None and origin != f"http://{host}":
        return 403, "Cross-origin request"
    if method in ("POST", "PUT") and headers.get("content-type", "").split(";")[0] != "application/json":
        return 415, "Expected application/json"
    return None


class RejectUntrustedRequests:
    """Runs the checks above before any route, so no endpoint can forget them."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            if error := untrusted_request_error(Headers(scope=scope), scope["method"]):
                status, message = error
                await JSONResponse({"error": message}, status_code=status)(scope, receive, send)
                return
        await self.app(scope, receive, send)


@asynccontextmanager
async def lifespan(app):
    notes.ensure_repo()
    chats.init()
    yield


# No auto-generated API docs: less surface, and the client is our own page.
app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(RejectUntrustedRequests)


@app.exception_handler(notes.NoteError)
@app.exception_handler(chats.NotFound)
async def known_error(request, error):
    return JSONResponse({"error": type(error).__name__}, status_code=ERROR_STATUS[type(error)])


@app.exception_handler(RequestValidationError)
@app.exception_handler(ValueError)
async def bad_request(request, error):
    return JSONResponse({"error": "Bad request"}, status_code=400)


class NoteBody(BaseModel):
    content: str


class MoveBody(BaseModel):
    source: str = Field(alias="from")
    destination: str = Field(alias="to")


class JournalBody(BaseModel):
    text: str


class ChatBody(BaseModel):
    message: str
    conversation_id: str | None = None
    think: bool = False


class FeedbackBody(BaseModel):
    rating: int | None = None
    reasons: list[str] = []
    comment: str | None = None
    correction: str | None = None


@app.get("/")
def index():
    return Response(INDEX.read_bytes(), media_type="text/html; charset=utf-8")


# ---- Notes ----

@app.get("/api/notes")
def list_notes():
    return notes.list_notes()


@app.post("/api/notes/move")
def move_note(body: MoveBody, if_match: str | None = Header(None)):
    tag = notes.move_note(body.source, body.destination, if_match=if_match)
    return JSONResponse({"path": body.destination}, headers={"ETag": tag})


@app.get("/api/notes/{path:path}")
def read_note(path: str):
    content, tag = notes.read_note(path)
    return JSONResponse({"path": path, "content": content}, headers={"ETag": tag})


@app.put("/api/notes/{path:path}")
def write_note(
    path: str, body: NoteBody,
    if_match: str | None = Header(None), if_none_match: str | None = Header(None),
):
    tag = notes.write_note(path, body.content, if_match=if_match, create_only=if_none_match == "*")
    return JSONResponse({"path": path}, headers={"ETag": tag})


@app.delete("/api/notes/{path:path}", status_code=204)
def delete_note(path: str, if_match: str | None = Header(None)):
    notes.delete_note(path, if_match=if_match)


@app.post("/api/journal", status_code=201)
def append_journal(body: JournalBody):
    if not body.text.strip():
        raise ValueError("empty journal entry")
    return {"path": notes.append_journal(body.text)}


# ---- Chat ----

@app.post("/api/chat")
def chat_endpoint(body: ChatBody):
    """Answer `message` in conversation `conversation_id` (null starts a new one).

    The history comes from the database, not the client, so a conversation
    can continue on any device.
    """
    if not body.message.strip():
        raise ValueError("empty message")
    history = chats.history(body.conversation_id) if body.conversation_id else []
    return StreamingResponse(
        stream_answer(body.conversation_id, history, body.message, body.think),
        media_type="application/x-ndjson",
    )


def stream_answer(conversation_id, history, question, think):
    """Yield one JSON object per line as pieces of the reply arrive."""
    def event(kind, value):
        return json.dumps({"kind": kind, "value": value}) + "\n"

    start = time.monotonic()
    pieces = {"content": [], "reasoning": []}
    model = timings = None
    try:
        for kind, value in chat([*history, {"role": "user", "content": question}], think=think):
            if kind == "model":
                model = value
                continue
            if kind == "timings":
                timings = value
            else:
                pieces[kind].append(value)
            yield event(kind, value)
        # Saved only once the answer is complete. If the page is closed, the
        # server stops iterating this generator before reaching this point, so
        # no question is ever stored without its answer.
        conversation_id, message_id = chats.save_turn(
            conversation_id, question, "".join(pieces["content"]),
            reasoning="".join(pieces["reasoning"]), think=think, model=model,
            timings=timings, latency_ms=round((time.monotonic() - start) * 1000),
        )
        yield event("saved", {"conversation_id": conversation_id, "message_id": message_id})
    except urllib.error.HTTPError as error:
        yield event("error", f"Model server error {error.code}: {error.read().decode()}")
    except urllib.error.URLError:
        yield event("error", "Can't reach the model server. Is serve.sh running?")


@app.get("/api/conversations")
def list_conversations():
    return chats.list_conversations()


@app.get("/api/conversations/{conversation_id}")
def get_conversation(conversation_id: str):
    return chats.get_conversation(conversation_id)


@app.delete("/api/conversations/{conversation_id}", status_code=204)
def delete_conversation(conversation_id: str):
    chats.delete_conversation(conversation_id)


@app.put("/api/messages/{message_id}/feedback", status_code=204)
def save_feedback(message_id: int, body: FeedbackBody):
    chats.set_feedback(message_id, body.rating, body.reasons, body.comment, body.correction)


def listening_socket(host):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, PORT))
    return sock


def main():
    hosts = ["127.0.0.1"]
    ip, names = tailscale_self()
    if ip:
        hosts.append(ip)
        allowed_hosts.update([ip, *names])
    else:
        print("Tailscale isn't running, so this is only reachable from this Mac.")

    for host in hosts:
        print(f"Listening on http://{host}:{PORT}")
    # One server, one socket per address: never 0.0.0.0.
    uvicorn.Server(uvicorn.Config(app, log_level="info")).run(
        sockets=[listening_socket(host) for host in hosts]
    )


if __name__ == "__main__":
    main()
