"""Tools the model can call, each declaring how risky it is.

The agent loop, not the model, decides whether a call may run: policy lives
in code, so no prompt (or injected instruction) can talk its way past it.
"""

import difflib
from dataclasses import dataclass
from enum import IntEnum
from typing import Callable

import notes


class Tier(IntEnum):
    READ_LOCAL = 0     # e.g. read a note: runs automatically
    WRITE_LOCAL = 1    # reversible local change (git can undo it): runs automatically, logged
    READ_EXTERNAL = 2  # data leaves this machine: approval per tool
    ACT_EXTERNAL = 3   # irreversible, costs money or speaks for me: always approved


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: dict  # JSON schema for the arguments
    tier: Tier
    requires_approval: bool
    run: Callable[..., dict]
    # Approval tools only: validates and previews without side effects.
    # Returns (preview shown to the user, precondition passed to `run`).
    propose: Callable[..., tuple] | None = None

    def schema(self):
        """The definition sent to the model."""
        return {"type": "function", "function": {
            "name": self.name, "description": self.description, "parameters": self.parameters,
        }}


TOOLS = {}


def tool(name, description, tier, requires_approval=False, propose=None, **properties):
    """Register a tool whose arguments are all required strings, described by `properties`.

    Tools that require approval need a `propose` step: the user approves its
    exact preview, and `run` then gets its precondition (e.g. the note version
    that was previewed), so a later change can't slip in unapproved.
    """
    if tier >= Tier.ACT_EXTERNAL and not requires_approval:
        raise ValueError(f"{name}: tier {tier.name} tools must require approval")
    if requires_approval != (propose is not None):
        raise ValueError(f"{name}: tools that require approval need a propose step (and only they)")

    def register(run):
        TOOLS[name] = Tool(
            name, description,
            {
                "type": "object",
                "properties": {key: {"type": "string", "description": text} for key, text in properties.items()},
                "required": list(properties),
            },
            tier, requires_approval, run, propose,
        )
        return run
    return register


def validate(tool, arguments):
    """Raise ValueError unless `arguments` match the tool's schema exactly."""
    if not isinstance(arguments, dict):
        raise ValueError("arguments must be a JSON object")
    expected = tool.parameters["properties"]
    if missing := [key for key in tool.parameters["required"] if key not in arguments]:
        raise ValueError(f"missing arguments: {', '.join(missing)}")
    if unknown := [key for key in arguments if key not in expected]:
        raise ValueError(f"unknown arguments: {', '.join(unknown)}")
    if wrong := [key for key, value in arguments.items() if not isinstance(value, str)]:
        raise ValueError(f"arguments must be strings: {', '.join(wrong)}")


@tool(
    "append_journal",
    "Add an entry to today's journal note. Use when the user asks to log, record or journal "
    "something about their day. The entry is timestamped automatically.",
    Tier.WRITE_LOCAL,
    text="The entry, written in the user's first person, e.g. 'Ran 5k, no hamstring tightness.'",
)
def append_journal(text):
    if not text.strip():
        raise ValueError("text must not be empty")
    return {"saved_to": notes.append_journal(text)}


@tool(
    "create_note",
    "Create a new Markdown note. Use only when the user asks for a new note. "
    "Fails if a note already exists at that path.",
    Tier.WRITE_LOCAL,
    path="Where to save it, e.g. 'training/plan.md' (folders are created as needed).",
    content="The full Markdown content, starting with a '# Title' line.",
)
def create_note(path, content):
    if not path.endswith(".md"):
        path += ".md"
    notes.write_note(path, content, create_only=True)
    return {"created": path}


def _diff(path, before, after):
    return "\n".join(difflib.unified_diff(
        before.splitlines(), after.splitlines(), fromfile=path, tofile=path, lineterm="",
    ))


def _propose_edit(path, old_text, new_text):
    content, version = notes.read_note(path)
    if not old_text:
        raise ValueError("old_text must not be empty")
    if (count := content.count(old_text)) != 1:
        raise ValueError(f"old_text must appear exactly once in {path} (found {count})")
    return {"path": path, "diff": _diff(path, content, content.replace(old_text, new_text, 1))}, version


@tool(
    "edit_note",
    "Change part of an existing note by replacing one exact passage. Use only when the user "
    "asks to change a note. Calling it shows the user the exact change to approve, so call "
    "it directly instead of asking for confirmation yourself.",
    Tier.WRITE_LOCAL, requires_approval=True, propose=_propose_edit,
    path="The note to change, e.g. 'training/plan.md'.",
    old_text="The exact text to replace, copied from the note; must appear exactly once.",
    new_text="The replacement text.",
)
def edit_note(path, old_text, new_text, precondition):
    content, _ = notes.read_note(path)
    # write_note refuses (Conflict) if the note changed since the user saw the preview.
    notes.write_note(path, content.replace(old_text, new_text, 1), if_match=precondition)
    return {"edited": path}


def _propose_move(source, destination):
    _, version = notes.read_note(source)
    try:
        notes.read_note(destination)
    except notes.NotFound:
        return {"from": source, "to": destination}, version
    raise notes.DestinationExists(destination)


@tool(
    "move_note",
    "Move or rename a note. Use only when the user asks. Calling it shows the user the move "
    "to approve, so call it directly instead of asking for confirmation yourself.",
    Tier.WRITE_LOCAL, requires_approval=True, propose=_propose_move,
    source="The note's current path, e.g. 'ideas.md'.",
    destination="The new path, ending in .md, e.g. 'projects/ideas.md'.",
)
def move_note(source, destination, precondition):
    notes.move_note(source, destination, if_match=precondition)
    return {"moved": source, "to": destination}


def _propose_delete(path):
    content, version = notes.read_note(path)
    return {"path": path, "content": content}, version


@tool(
    "delete_note",
    "Delete a note (it stays recoverable in the notes' history). Use only when the user "
    "asks. Calling it shows the user what will be deleted to approve, so call it directly "
    "instead of asking for confirmation yourself.",
    Tier.WRITE_LOCAL, requires_approval=True, propose=_propose_delete,
    path="The note to delete.",
)
def delete_note(path, precondition):
    notes.delete_note(path, if_match=precondition)
    return {"deleted": path}
