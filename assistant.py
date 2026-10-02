"""The assistant itself, shared by the terminal chat and the web app.

There is no retrieval yet: all notes go into the prompt on every turn. That is
fine for a handful of notes and breaks as the folder grows, which is the point.
"""

import json
import os
import urllib.request
from datetime import date
from pathlib import Path

DATA_DIR = Path(os.environ.get("PERSONAL_AI_DATA", Path.home() / "personal-ai-data"))
NOTES_DIR = DATA_DIR / "notes"
SERVER = os.environ.get("LLAMA_SERVER", "http://127.0.0.1:8080")


def load_notes():
    return [
        f"### {path.relative_to(NOTES_DIR)}\n{path.read_text().strip()}"
        for path in sorted(NOTES_DIR.rglob("*.md"))
    ]


def system_prompt(notes):
    return (
        "You are a personal assistant for one person. Below are their notes and journal "
        "entries.\n"
        "- For facts about their life (what happened, plans, health, goals), rely on the "
        "notes and mention which note you used. If the notes don't say, tell them rather "
        "than inventing details.\n"
        "- For advice and general questions, use your general knowledge, tailored to what "
        "the notes say about them.\n"
        "- Be concise.\n\n"
        f"Today is {date.today():%A, %Y-%m-%d}.\n\n# Notes\n\n" + "\n\n".join(notes)
    )


def chat(messages, think=False):
    """Stream a reply to `messages` (user/assistant turns, no system prompt).

    `think` turns on the model's step-by-step reasoning before it answers:
    better for reflection and planning, but often 10-20x slower.

    Yields ("reasoning", text) and ("content", text) pieces as they arrive,
    then ("timings", dict) with llama-server's speed stats.
    Raises urllib.error.HTTPError if the server rejects the request.
    """
    api_key = (DATA_DIR / "api-key").read_text().split()[0]
    messages = [{"role": "system", "content": system_prompt(load_notes())}, *messages]
    request = urllib.request.Request(
        f"{SERVER}/v1/chat/completions",
        data=json.dumps({
            "messages": messages,
            "stream": True,
            "chat_template_kwargs": {"enable_thinking": think},
        }).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
    )
    timings = None
    with urllib.request.urlopen(request) as response:
        # Server-sent events: one "data: {json}" line per chunk.
        for line in response:
            line = line.decode().strip()
            if not line.startswith("data: ") or line == "data: [DONE]":
                continue
            chunk = json.loads(line[len("data: "):])
            timings = chunk.get("timings", timings)
            if not chunk.get("choices"):
                continue
            delta = chunk["choices"][0]["delta"]
            if delta.get("reasoning_content"):
                yield "reasoning", delta["reasoning_content"]
            if delta.get("content"):
                yield "content", delta["content"]
    if timings:
        yield "timings", timings
