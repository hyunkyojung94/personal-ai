"""Notes storage: Markdown files in a folder that is its own git repository.

The files are the source of truth, so they still work in any editor. Every
save and delete made through the app is committed, so any change can be undone.
"""

import hashlib
import subprocess
import threading
from datetime import datetime
from pathlib import Path

from config import NOTES_DIR

# The web server handles requests on parallel threads. Check-then-write must
# happen as one step, or two saves could both pass the version check.
_write_lock = threading.Lock()


class NoteError(Exception):
    """Base class; the web layer maps each subclass to an HTTP status."""


class InvalidPath(NoteError):
    pass


class NotFound(NoteError):
    pass


class Conflict(NoteError):
    """The note changed since the client read it (or already exists)."""


class DestinationExists(Conflict):
    """A move would overwrite another note."""


class PreconditionRequired(NoteError):
    """Writes must say which version they replace, so edits can't be lost silently."""


def etag(content):
    return '"' + hashlib.sha256(content.encode()).hexdigest()[:16] + '"'


def _resolve(relative):
    parts = Path(relative).parts
    # Rejects "..", hidden files and the .git folder in one check.
    if not relative.endswith(".md") or Path(relative).is_absolute() or any(
        part.startswith(".") for part in parts
    ):
        raise InvalidPath(relative)
    full = (NOTES_DIR / relative).resolve()
    # Catches symlinks that point outside the notes folder.
    if not full.is_relative_to(NOTES_DIR.resolve()):
        raise InvalidPath(relative)
    return full


def _remove_empty_folders(full):
    """Folders exist only to hold notes, so drop any a move or delete emptied."""
    folder = full.parent
    while folder != NOTES_DIR.resolve() and not any(folder.iterdir()):
        folder.rmdir()
        folder = folder.parent


def _git(*args):
    subprocess.run(
        ["git", "-C", str(NOTES_DIR), "-c", "user.name=personal-ai", "-c",
         "user.email=personal-ai@localhost", *args],
        check=True, capture_output=True,
    )


def _commit(message, *paths):
    # Stage exactly these paths. A path git has never seen (e.g. a note made
    # in another editor, then moved or deleted here) must not make this fail
    # after the file has already changed on disk.
    existing = [str(p) for p in paths if p.exists()]
    gone = [str(p) for p in paths if not p.exists()]
    if existing:
        _git("add", "-A", "--", *existing)
    if gone:
        _git("rm", "-q", "--cached", "--ignore-unmatch", "--", *gone)
    _git("commit", "-q", "--allow-empty", "-m", message)


def ensure_repo():
    NOTES_DIR.mkdir(parents=True, exist_ok=True)
    if not (NOTES_DIR / ".git").exists():
        _git("init", "-q")
        _git("add", "-A")
        _git("commit", "-q", "--allow-empty", "-m", "Initial import")


def _title(content, relative):
    for line in content.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return relative.stem


def list_notes():
    notes = []
    for path in NOTES_DIR.rglob("*.md"):
        relative = path.relative_to(NOTES_DIR)
        if any(part.startswith(".") for part in relative.parts):
            continue
        notes.append({
            "path": relative.as_posix(),
            "title": _title(path.read_text(), relative),
            "modified": path.stat().st_mtime,
        })
    return sorted(notes, key=lambda note: note["modified"], reverse=True)


def read_note(relative):
    full = _resolve(relative)
    if not full.is_file():
        raise NotFound(relative)
    content = full.read_text()
    return content, etag(content)


def write_note(relative, content, if_match=None, create_only=False):
    """Create (create_only=True) or replace the version `if_match`. Returns the new ETag."""
    if if_match is None and not create_only:
        raise PreconditionRequired(relative)
    full = _resolve(relative)
    with _write_lock:
        exists = full.is_file()
        if create_only and exists:
            raise Conflict(relative)
        if if_match is not None and (not exists or etag(full.read_text()) != if_match):
            raise Conflict(relative)
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content)
        _commit(f"{'Update' if exists else 'Create'} {relative}", full)
    return etag(content)


def delete_note(relative, if_match):
    if if_match is None:
        raise PreconditionRequired(relative)
    full = _resolve(relative)
    with _write_lock:
        if not full.is_file():
            raise NotFound(relative)
        if etag(full.read_text()) != if_match:
            raise Conflict(relative)
        full.unlink()
        _commit(f"Delete {relative}", full)
        _remove_empty_folders(full)


def move_note(source, destination, if_match):
    """Move or rename a note as one step: one atomic rename, one commit.

    Never overwrites: fails if a different note already exists at
    `destination`. Returns the note's ETag (unchanged, since content is too).
    """
    if if_match is None:
        raise PreconditionRequired(source)
    full_source, full_destination = _resolve(source), _resolve(destination)
    with _write_lock:
        if not full_source.is_file():
            raise NotFound(source)
        content = full_source.read_text()
        if etag(content) != if_match:
            raise Conflict(source)
        # samefile allows case-only renames on macOS's case-insensitive disk.
        if full_destination.exists() and not full_destination.samefile(full_source):
            raise DestinationExists(destination)
        full_destination.parent.mkdir(parents=True, exist_ok=True)
        # rename() is atomic within one filesystem: the note is never in both
        # places or neither.
        full_source.rename(full_destination)
        _commit(f"Move {source} to {destination}", full_source, full_destination)
        _remove_empty_folders(full_source)
    return etag(content)


def append_journal(text):
    """Add a timestamped entry to today's journal note. Returns its path.

    Appends need no version check: the server does read-and-append under the
    lock, so two concurrent entries both land instead of one overwriting the other.
    """
    now = datetime.now()
    relative = f"journal/{now:%Y-%m-%d}.md"
    full = _resolve(relative)
    with _write_lock:
        existing = full.read_text() if full.is_file() else f"# Journal — {now:%Y-%m-%d}\n"
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(f"{existing.rstrip()}\n\n## {now:%H:%M}\n{text.strip()}\n")
        _commit(f"Journal entry {relative}", full)
    return relative
