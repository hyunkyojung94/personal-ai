"""Web app: a mobile-friendly chat and notes page for your own devices.

Listens on localhost and, if Tailscale is running, on this machine's Tailscale
address. Never on all interfaces, so people on the same café Wi-Fi can't reach it.
"""

import json
import subprocess
import threading
import time
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote

import chats
import notes
from assistant import chat

PORT = 8000
INDEX = Path(__file__).parent / "static" / "index.html"
# The Mac App Store version of Tailscale doesn't put its CLI on PATH.
TAILSCALE_CLIS = ["tailscale", "/Applications/Tailscale.app/Contents/MacOS/Tailscale"]
ERROR_STATUS = {
    notes.InvalidPath: 400,
    notes.NotFound: 404,
    notes.Conflict: 409,
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


class Handler(BaseHTTPRequestHandler):
    def _allowed(self, has_body):
        """Reject requests that could come from a malicious web page.

        Host allowlist (every request): blocks DNS rebinding, where an
        attacker's domain is re-pointed at this machine so the browser treats
        this app as the attacker's own site.
        Origin must match Host (writes): blocks cross-site request forgery.
        The same-origin policy stops other sites reading responses, but not
        sending requests, and a write only needs to be sent to do damage.
        JSON body (writes): another site can't send that without a CORS
        preflight, which this server never approves.
        """
        host = self.headers.get("Host", "")
        if host.rsplit(":", 1)[0] not in allowed_hosts:
            self.send_error(403, "Unknown Host")
            return False
        if self.command == "GET":
            return True
        origin = self.headers.get("Origin")
        # Browsers always send Origin on these requests; tools like curl don't.
        if origin is not None and origin != f"http://{host}":
            self.send_error(403, "Cross-origin request")
            return False
        if has_body and self.headers.get("Content-Type", "").split(";")[0] != "application/json":
            self.send_error(415, "Expected application/json")
            return False
        return True

    def _read_json(self):
        return json.loads(self.rfile.read(int(self.headers["Content-Length"])))

    def _send_json(self, status, payload, headers=()):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for name, value in headers:
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _tail(self, prefix):
        """The rest of the path after `prefix`, or None if it doesn't match."""
        return unquote(self.path[len(prefix):]) if self.path.startswith(prefix) else None

    def _note_path(self):
        return self._tail("/api/notes/")

    def _handle_errors(self, action):
        try:
            action()
        except (notes.NoteError, chats.NotFound) as error:
            self._send_json(ERROR_STATUS[type(error)], {"error": type(error).__name__})
        except (AttributeError, KeyError, TypeError, ValueError):
            self.send_error(400)

    def do_GET(self):
        if not self._allowed(has_body=False):
            return
        if self.path == "/":
            body = INDEX.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/api/notes":
            self._send_json(200, notes.list_notes())
        elif path := self._note_path():
            def read():
                content, tag = notes.read_note(path)
                self._send_json(200, {"path": path, "content": content}, [("ETag", tag)])
            self._handle_errors(read)
        elif self.path == "/api/conversations":
            self._send_json(200, chats.list_conversations())
        elif conversation_id := self._tail("/api/conversations/"):
            self._handle_errors(
                lambda: self._send_json(200, chats.get_conversation(conversation_id))
            )
        else:
            self.send_error(404)

    def do_PUT(self):
        if not self._allowed(has_body=True):
            return
        feedback_for = self._tail("/api/messages/")
        if feedback_for and feedback_for.endswith("/feedback"):
            def save_feedback():
                body = self._read_json()
                chats.set_feedback(
                    int(feedback_for.removesuffix("/feedback")),
                    body.get("rating"), list(body.get("reasons") or []),
                    body.get("comment"), body.get("correction"),
                )
                self.send_response(204)
                self.end_headers()
            self._handle_errors(save_feedback)
            return
        if not (path := self._note_path()):
            self.send_error(404)
            return

        def write():
            content = self._read_json()["content"]
            if not isinstance(content, str):
                raise TypeError
            tag = notes.write_note(
                path, content,
                if_match=self.headers.get("If-Match"),
                create_only=self.headers.get("If-None-Match") == "*",
            )
            self._send_json(200, {"path": path}, [("ETag", tag)])
        self._handle_errors(write)

    def do_DELETE(self):
        if not self._allowed(has_body=False):
            return
        if path := self._note_path():
            def delete():
                notes.delete_note(path, if_match=self.headers.get("If-Match"))
        elif conversation_id := self._tail("/api/conversations/"):
            def delete():
                chats.delete_conversation(conversation_id)
        else:
            self.send_error(404)
            return

        def delete_and_respond():
            delete()
            self.send_response(204)
            self.end_headers()
        self._handle_errors(delete_and_respond)

    def do_POST(self):
        if not self._allowed(has_body=True):
            return
        if self.path == "/api/journal":
            def append():
                text = self._read_json()["text"]
                if not isinstance(text, str) or not text.strip():
                    raise ValueError
                self._send_json(201, {"path": notes.append_journal(text)})
            self._handle_errors(append)
        elif self.path == "/api/chat":
            self._chat()
        else:
            self.send_error(404)

    def _chat(self):
        """Answer `message` in conversation `conversation_id` (null starts a new one).

        The history comes from the database, not the client, so a conversation
        can continue on any device.
        """
        try:
            body = self._read_json()
            question = body["message"]
            conversation_id = body.get("conversation_id")
            if not isinstance(question, str) or not question.strip():
                raise ValueError
            think = body.get("think") is True
            history = chats.history(conversation_id) if conversation_id else []
        except chats.NotFound:
            self.send_error(404)
            return
        except (AttributeError, KeyError, TypeError, ValueError):
            self.send_error(400)
            return

        # Stream one JSON object per line as pieces of the reply arrive.
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.end_headers()

        def send(kind, value):
            self.wfile.write(json.dumps({"kind": kind, "value": value}).encode() + b"\n")
            self.wfile.flush()

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
                send(kind, value)
            # Saved only once the answer is complete: an error or a closed page
            # leaves no question without its answer in the history.
            conversation_id, message_id = chats.save_turn(
                conversation_id, question, "".join(pieces["content"]),
                reasoning="".join(pieces["reasoning"]), think=think, model=model,
                timings=timings, latency_ms=round((time.monotonic() - start) * 1000),
            )
            send("saved", {"conversation_id": conversation_id, "message_id": message_id})
        except urllib.error.HTTPError as error:
            send("error", f"Model server error {error.code}: {error.read().decode()}")
        except urllib.error.URLError:
            send("error", "Can't reach the model server. Is serve.sh running?")
        except (BrokenPipeError, ConnectionResetError):
            pass  # The page was closed mid-answer; dropping the stream stops generation.


def main():
    notes.ensure_repo()
    chats.init()
    hosts = ["127.0.0.1"]
    ip, names = tailscale_self()
    if ip:
        hosts.append(ip)
        allowed_hosts.update([ip, *names])
    else:
        print("Tailscale isn't running, so this is only reachable from this Mac.")

    servers = [ThreadingHTTPServer((host, PORT), Handler) for host in hosts]
    for server in servers[1:]:
        threading.Thread(target=server.serve_forever, daemon=True).start()
    for host in hosts:
        print(f"Listening on http://{host}:{PORT}")
    try:
        servers[0].serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
