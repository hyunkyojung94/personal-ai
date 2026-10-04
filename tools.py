"""Tools the model can call, each declaring how risky it is.

The agent loop, not the model, decides whether a call may run: policy lives
in code, so no prompt (or injected instruction) can talk its way past it.
"""

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

    def schema(self):
        """The definition sent to the model."""
        return {"type": "function", "function": {
            "name": self.name, "description": self.description, "parameters": self.parameters,
        }}


TOOLS = {}


def tool(name, description, tier, requires_approval=False, **properties):
    """Register a tool whose arguments are all required strings, described by `properties`."""
    if tier >= Tier.ACT_EXTERNAL and not requires_approval:
        raise ValueError(f"{name}: tier {tier.name} tools must require approval")

    def register(run):
        TOOLS[name] = Tool(
            name, description,
            {
                "type": "object",
                "properties": {key: {"type": "string", "description": text} for key, text in properties.items()},
                "required": list(properties),
            },
            tier, requires_approval, run,
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
