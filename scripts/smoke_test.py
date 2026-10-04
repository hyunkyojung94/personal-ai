"""End-to-end API checks against a running web app (and model server).

    python3 scripts/smoke_test.py [base_url]     # default http://127.0.0.1:8000

Uses throwaway notes under zz-test/ and test conversations, deletes them
afterwards, and verifies the real notes are byte-for-byte unchanged. It never
appends to the journal: that would write to today's real journal note.
"""

import hashlib
import json
import sqlite3
import time
import sys
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote, urlsplit

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
DATA_DIR = Path.home() / "personal-ai-data"
NOTES_DIR = DATA_DIR / "notes"
TEST_PREFIX = "zz-test/"

failures = []


def request(method, path, body=None, headers=None, raw=None):
    """Returns (status, headers, body bytes) without raising on HTTP errors."""
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    all_headers = {"Content-Type": "application/json"} if data is not None else {}
    all_headers.update(headers or {})
    req = urllib.request.Request(BASE + path, data=data, method=method, headers=all_headers)
    try:
        with urllib.request.urlopen(req, timeout=120) as response:
            return response.status, response.headers, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.headers, error.read()


def check(name, actual, expected):
    ok = actual == expected
    print(f"{'PASS' if ok else 'FAIL'}  {name}: {actual!r}" + ("" if ok else f" (expected {expected!r})"))
    if not ok:
        failures.append(name)


def note_url(path):
    return "/api/notes/" + quote(path, safe="")


def etag(path):
    return request("GET", note_url(path))[1].get("ETag")


def real_notes_fingerprint():
    digest = hashlib.sha256()
    for path in sorted(NOTES_DIR.rglob("*.md")):
        relative = path.relative_to(NOTES_DIR).as_posix()
        if not relative.startswith(TEST_PREFIX):
            digest.update(relative.encode() + path.read_bytes())
    return digest.hexdigest()


def chat(message, conversation_id=None):
    status, _, body = request("POST", "/api/chat", {"message": message, "conversation_id": conversation_id})
    if status != 200:
        return status, "", None
    answer, saved = "", None
    for line in body.decode().splitlines():
        event = json.loads(line)
        if event["kind"] == "content":
            answer += event["value"]
        elif event["kind"] == "saved":
            saved = event["value"]
    return status, answer.strip(), saved


def main():
    fingerprint = real_notes_fingerprint()
    origin = f"http://{urlsplit(BASE).netloc}"

    print("--- pages and security")
    check("GET /", request("GET", "/")[0], 200)
    check("GET /api/notes", request("GET", "/api/notes")[0], 200)
    check("foreign Host (DNS rebinding)", request("GET", "/api/notes", headers={"Host": "evil.com:8000"})[0], 403)
    check("cross-origin write (CSRF)",
          request("POST", "/api/journal", {"text": "x"}, {"Origin": "http://evil.com"})[0], 403)
    check("non-JSON write", request("POST", "/api/journal", raw=b'{"text":"x"}',
                                    headers={"Content-Type": "text/plain"})[0], 415)
    check("same-origin write passes the checks (empty text -> 400)",
          request("POST", "/api/journal", {"text": " "}, {"Origin": origin})[0], 400)

    print("--- notes")
    a, b = TEST_PREFIX + "a.md", TEST_PREFIX + "b.md"
    check("create", request("PUT", note_url(a), {"content": "# A"}, {"If-None-Match": "*"})[0], 200)
    check("create again", request("PUT", note_url(a), {"content": "# A"}, {"If-None-Match": "*"})[0], 409)
    request("PUT", note_url(b), {"content": "# B"}, {"If-None-Match": "*"})
    check("update without precondition", request("PUT", note_url(a), {"content": "x"})[0], 428)
    v1 = etag(a)
    check("update with current etag", request("PUT", note_url(a), {"content": "# A2"}, {"If-Match": v1})[0], 200)
    check("update with stale etag", request("PUT", note_url(a), {"content": "lost"}, {"If-Match": v1})[0], 409)
    check("content kept", json.loads(request("GET", note_url(a))[2])["content"], "# A2")
    check("encoded traversal", request("GET", note_url("../secret.md"))[0], 400)
    check("into .git", request("PUT", note_url(".git/x.md"), {"content": "x"}, {"If-None-Match": "*"})[0], 400)
    check("not .md", request("GET", note_url("api-key"))[0], 400)
    check("missing note", request("GET", note_url(TEST_PREFIX + "nope.md"))[0], 404)
    check("bad body", request("PUT", note_url(a), [1], {"If-Match": etag(a)})[0], 400)

    print("--- moves")
    v2 = etag(a)
    move = lambda src, dst, tag: request("POST", "/api/notes/move", {"from": src, "to": dst},
                                         {"If-Match": tag} if tag else {})
    check("move without If-Match", move(a, TEST_PREFIX + "x.md", None)[0], 428)
    status, _, body = move(a, b, v2)
    check("move onto existing", (status, json.loads(body).get("error")), (409, "DestinationExists"))
    check("move with stale etag", move(a, TEST_PREFIX + "y.md", '"stale"')[0], 409)
    check("move to ../", move(a, "../a.md", v2)[0], 400)
    deep = TEST_PREFIX + "deep/a.md"
    check("move into new folder", move(a, deep, v2)[0], 200)
    check("etag unchanged by move", etag(deep), v2)

    print("--- deletes")
    check("delete with stale etag", request("DELETE", note_url(deep), headers={"If-Match": v1})[0], 409)
    for path in (deep, b):
        check(f"delete {path}", request("DELETE", note_url(path), headers={"If-Match": etag(path)})[0], 204)
    check("read deleted", request("GET", note_url(b))[0], 404)

    print("--- chat and history")
    status, answer, saved = chat("Remember the code word pineapple. Reply with just OK.")
    check("new conversation", (status, saved is not None), (200, True))
    cid, mid = saved["conversation_id"], saved["message_id"]
    status, answer, _ = chat("What code word did I give you? Reply with one word.", cid)
    check("history from the database", "pineapple" in answer.lower(), True)
    check("unknown conversation", request("POST", "/api/chat", {"message": "hi", "conversation_id": "nope"})[0], 404)
    check("empty message", request("POST", "/api/chat", {"message": "  "})[0], 400)
    check("non-object body", request("POST", "/api/chat", [1])[0], 400)

    print("--- feedback")
    good = {"rating": -1, "reasons": ["too_long"], "comment": "c", "correction": "OK"}
    check("valid feedback", request("PUT", f"/api/messages/{mid}/feedback", good)[0], 204)
    check("bad reason", request("PUT", f"/api/messages/{mid}/feedback", {"rating": 1, "reasons": ["bogus"]})[0], 400)
    check("feedback on a user message", request("PUT", f"/api/messages/{mid - 1}/feedback", {"rating": 1})[0], 404)
    check("cross-origin feedback",
          request("PUT", f"/api/messages/{mid}/feedback", {"rating": 1}, {"Origin": "http://evil.com"})[0], 403)
    conversation = json.loads(request("GET", f"/api/conversations/{cid}")[2])
    check("feedback stored", conversation["messages"][1]["feedback"]["correction"], "OK")

    print("--- aborted answer saves nothing")
    db = sqlite3.connect(DATA_DIR / "chats.sqlite")
    before = db.execute("SELECT count(*) FROM messages").fetchone()[0]
    req = urllib.request.Request(BASE + "/api/chat", method="POST", headers={"Content-Type": "application/json"},
                                 data=json.dumps({"message": "Write a long essay about soccer.", "think": True}).encode())
    with urllib.request.urlopen(req, timeout=60) as response:
        response.readline()  # first streamed piece, then hang up
    time.sleep(3)
    check("message count unchanged", db.execute("SELECT count(*) FROM messages").fetchone()[0], before)

    print("--- conversations")
    listed = [c["id"] for c in json.loads(request("GET", "/api/conversations")[2])]
    check("listed", cid in listed, True)
    check("delete conversation", request("DELETE", f"/api/conversations/{cid}")[0], 204)
    check("deleted conversation", request("GET", f"/api/conversations/{cid}")[0], 404)
    check("feedback cascaded", db.execute("SELECT count(*) FROM feedback WHERE message_id = ?", (mid,)).fetchone()[0], 0)

    print("--- cleanup")
    check("zz-test folder removed", (NOTES_DIR / TEST_PREFIX).exists(), False)
    check("real notes unchanged", real_notes_fingerprint(), fingerprint)

    print(f"\n{'ALL PASSED' if not failures else f'{len(failures)} FAILED: {failures}'}")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
