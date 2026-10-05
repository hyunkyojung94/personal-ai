"""Chat history: conversations, messages and feedback in SQLite.

Notes are documents people edit, so they live as files under git. Chats are
append-only logs that get listed and queried, so they live in a database.
"""

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

from config import DATA_DIR

DB_PATH = DATA_DIR / "chats.sqlite"
FEEDBACK_REASONS = {"wrong_fact", "missed_notes", "made_up", "too_long", "not_helpful", "tone"}

# Each entry upgrades the schema by one version. PRAGMA user_version records
# how many have been applied, so existing databases get only the new ones.
MIGRATIONS = [
    """
    CREATE TABLE conversations (
        id TEXT PRIMARY KEY,
        title TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE INDEX conversations_by_updated ON conversations(updated_at);

    CREATE TABLE messages (
        id INTEGER PRIMARY KEY,
        conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
        role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
        content TEXT NOT NULL,
        -- The rest describe how an assistant answer was produced, so later
        -- evaluation and fine-tuning can tell answers from different setups apart.
        reasoning TEXT,
        think INTEGER,
        model TEXT,
        prompt_tokens INTEGER,
        completion_tokens INTEGER,
        latency_ms INTEGER,
        created_at TEXT NOT NULL
    );
    CREATE INDEX messages_by_conversation ON messages(conversation_id, id);

    CREATE TABLE feedback (
        message_id INTEGER PRIMARY KEY REFERENCES messages(id) ON DELETE CASCADE,
        rating INTEGER CHECK (rating IN (-1, 1)),
        reasons TEXT NOT NULL DEFAULT '[]',  -- JSON list from FEEDBACK_REASONS
        comment TEXT,
        correction TEXT,  -- the answer as it should have been
        updated_at TEXT NOT NULL
    );
    """,
    # v2: tool use. SQLite can't change a CHECK constraint in place, so the
    # messages table is rebuilt (create, copy, drop, rename), which is SQLite's
    # documented procedure. init() runs with foreign keys off, so dropping the
    # old table doesn't cascade into feedback.
    """
    CREATE TABLE messages_v2 (
        id INTEGER PRIMARY KEY,
        conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
        role TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'tool')),
        content TEXT NOT NULL,
        tool_calls TEXT,    -- assistant: JSON list of the tool calls it made
        tool_call_id TEXT,  -- tool: the call this result answers (tool_calls.call_id)
        reasoning TEXT,
        think INTEGER,
        model TEXT,
        prompt_tokens INTEGER,
        completion_tokens INTEGER,
        latency_ms INTEGER,
        created_at TEXT NOT NULL
    );
    INSERT INTO messages_v2 (id, conversation_id, role, content, reasoning, think, model,
                             prompt_tokens, completion_tokens, latency_ms, created_at)
        SELECT id, conversation_id, role, content, reasoning, think, model,
               prompt_tokens, completion_tokens, latency_ms, created_at FROM messages;
    DROP TABLE messages;
    ALTER TABLE messages_v2 RENAME TO messages;
    CREATE INDEX messages_by_conversation ON messages(conversation_id, id);

    -- Audit log: one row per tool call, written before it runs and updated after.
    CREATE TABLE tool_calls (
        id INTEGER PRIMARY KEY,
        call_id TEXT NOT NULL UNIQUE,   -- the id the model sees
        conversation_id TEXT NOT NULL,  -- no foreign key: the record outlives a deleted chat
        tool TEXT NOT NULL,
        tier INTEGER,                   -- NULL if the model named an unknown tool
        arguments TEXT NOT NULL,        -- exactly what the model sent
        arguments_hash TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN (
            'running', 'executed', 'failed', 'refused',
            'pending', 'approved', 'denied', 'cancelled')),
        result TEXT,                    -- JSON outcome given back to the model
        created_at TEXT NOT NULL,
        decided_at TEXT,
        finished_at TEXT
    );
    CREATE INDEX tool_calls_by_conversation ON tool_calls(conversation_id);
    """,
    # v3: outcome of checking an answer's claims against what tools did:
    # JSON {"status": "corrected" | "unverified", "problem", "retracted"?}.
    """
    ALTER TABLE messages ADD COLUMN verification TEXT;
    """,
]


class NotFound(Exception):
    pass


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def _db():
    """A connection whose statements commit together, or roll back on error."""
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    try:
        with db:
            yield db
    finally:
        db.close()


def init():
    db = sqlite3.connect(DB_PATH)
    try:
        # Write-ahead logging lets readers keep reading while a write happens.
        db.execute("PRAGMA journal_mode = WAL")
        version = db.execute("PRAGMA user_version").fetchone()[0]
        for number, migration in enumerate(MIGRATIONS[version:], start=version + 1):
            db.executescript(f"BEGIN; {migration} PRAGMA user_version = {number}; COMMIT;")
    finally:
        db.close()


def history(conversation_id):
    """The conversation's messages in the shape the model expects, tool calls included."""
    with _db() as db:
        if not db.execute("SELECT 1 FROM conversations WHERE id = ?", (conversation_id,)).fetchone():
            raise NotFound(conversation_id)
        rows = db.execute(
            """SELECT role, content, tool_calls, tool_call_id FROM messages
               WHERE conversation_id = ? ORDER BY id""",
            (conversation_id,),
        )
        messages = []
        for row in rows:
            message = {"role": row["role"], "content": row["content"]}
            if row["tool_calls"]:
                message["tool_calls"] = json.loads(row["tool_calls"])
            if row["tool_call_id"]:
                message["tool_call_id"] = row["tool_call_id"]
            messages.append(message)
        return messages


def new_conversation_id():
    """Chosen when a conversation's first turn starts, so tool calls can be
    logged against it before the turn (and the conversation row) is saved."""
    return str(uuid.uuid4())


def save_turn(conversation_id, is_new, question, steps, answer, *,
              reasoning, think, model, timings, latency_ms, verification=None):
    """Store a whole turn as one unit: the question, any tool-calling steps
    (model-format assistant and tool messages), and the final answer.

    Called only after the answer is complete, so a failed or abandoned answer
    leaves no half-saved turn behind. Returns the final answer's message id.
    """
    now = _now()
    with _db() as db:
        if is_new:
            title = " ".join(question.split())[:60]
            db.execute(
                "INSERT INTO conversations (id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
                (conversation_id, title, now, now),
            )
        else:
            db.execute("UPDATE conversations SET updated_at = ? WHERE id = ?", (now, conversation_id))
        db.execute(
            "INSERT INTO messages (conversation_id, role, content, created_at) VALUES (?, 'user', ?, ?)",
            (conversation_id, question, now),
        )
        for step in steps:
            db.execute(
                """INSERT INTO messages (conversation_id, role, content, tool_calls, tool_call_id, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    conversation_id, step["role"], step["content"] or "",
                    json.dumps(step["tool_calls"]) if step.get("tool_calls") else None,
                    step.get("tool_call_id"), now,
                ),
            )
        cursor = db.execute(
            """INSERT INTO messages (conversation_id, role, content, reasoning, think, model,
                                     prompt_tokens, completion_tokens, latency_ms, verification,
                                     created_at)
               VALUES (?, 'assistant', ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                conversation_id, answer, reasoning or None, int(think), model,
                timings and timings["cache_n"] + timings["prompt_n"],
                timings and timings["predicted_n"], latency_ms,
                json.dumps(verification) if verification else None, now,
            ),
        )
        return cursor.lastrowid


def start_tool_call(call_id, conversation_id, tool, tier, arguments, arguments_hash):
    """Record a tool call before it runs (status 'running'); returns the audit row id.

    Logging intent first means even a crash mid-action leaves a trace that it
    was attempted. Committed immediately, independent of the turn.
    """
    with _db() as db:
        return db.execute(
            """INSERT INTO tool_calls (call_id, conversation_id, tool, tier, arguments,
                                       arguments_hash, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, 'running', ?)""",
            (call_id, conversation_id, tool, tier, arguments, arguments_hash, _now()),
        ).lastrowid


def finish_tool_call(row_id, status, outcome):
    with _db() as db:
        db.execute(
            "UPDATE tool_calls SET status = ?, result = ?, finished_at = ? WHERE id = ?",
            (status, json.dumps(outcome), _now(), row_id),
        )


def list_conversations():
    with _db() as db:
        rows = db.execute("SELECT id, title, updated_at FROM conversations ORDER BY updated_at DESC")
        return [dict(row) for row in rows]


def get_conversation(conversation_id):
    with _db() as db:
        conversation = db.execute(
            "SELECT id, title FROM conversations WHERE id = ?", (conversation_id,)
        ).fetchone()
        if not conversation:
            raise NotFound(conversation_id)
        rows = db.execute(
            """SELECT m.id, m.role, m.content, m.reasoning, m.tool_calls, m.tool_call_id, m.verification,
                      f.rating, f.reasons, f.comment, f.correction,
                      t.tool, t.arguments, t.status, t.result
               FROM messages m
               LEFT JOIN feedback f ON f.message_id = m.id
               LEFT JOIN tool_calls t ON t.call_id = m.tool_call_id
               WHERE m.conversation_id = ? ORDER BY m.id""",
            (conversation_id,),
        )
        messages = []
        for row in rows:
            message = {key: row[key] for key in ("id", "role", "content", "reasoning")}
            if row["tool_calls"]:
                message["tool_calls"] = json.loads(row["tool_calls"])
            elif row["role"] == "tool":
                message["tool"] = {
                    "call_id": row["tool_call_id"], "name": row["tool"],
                    "arguments": row["arguments"], "status": row["status"],
                    **json.loads(row["result"] or row["content"] or "{}"),
                }
            elif row["role"] == "assistant":
                message["verification"] = json.loads(row["verification"]) if row["verification"] else None
                message["feedback"] = {
                    "rating": row["rating"],
                    "reasons": json.loads(row["reasons"] or "[]"),
                    "comment": row["comment"],
                    "correction": row["correction"],
                }
            messages.append(message)
        return {**dict(conversation), "messages": messages}


def delete_conversation(conversation_id):
    with _db() as db:
        # Messages and their feedback go too (ON DELETE CASCADE).
        if not db.execute("DELETE FROM conversations WHERE id = ?", (conversation_id,)).rowcount:
            raise NotFound(conversation_id)


def set_feedback(message_id, rating, reasons, comment, correction):
    """Replace the feedback on an assistant answer. All-empty feedback removes it."""
    if (
        rating not in (1, -1, None)
        or not set(reasons) <= FEEDBACK_REASONS
        or not all(isinstance(text, (str, type(None))) for text in (comment, correction))
    ):
        raise ValueError("invalid feedback")
    with _db() as db:
        row = db.execute("SELECT role, tool_calls FROM messages WHERE id = ?", (message_id,)).fetchone()
        # Feedback is on answers, not on the intermediate tool-calling steps.
        if not row or row["role"] != "assistant" or row["tool_calls"]:
            raise NotFound(message_id)
        if rating is None and not reasons and not comment and not correction:
            db.execute("DELETE FROM feedback WHERE message_id = ?", (message_id,))
            return
        db.execute(
            """INSERT INTO feedback (message_id, rating, reasons, comment, correction, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT (message_id) DO UPDATE SET rating = excluded.rating,
                   reasons = excluded.reasons, comment = excluded.comment,
                   correction = excluded.correction, updated_at = excluded.updated_at""",
            (message_id, rating, json.dumps(sorted(reasons)), comment or None,
             correction or None, _now()),
        )
