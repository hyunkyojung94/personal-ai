"""Terminal chat with the assistant."""

import time
import urllib.error

import chats
from assistant import NOTES_DIR, chat, load_notes

DIM, RESET = "\033[2m", "\033[0m"


def main():
    chats.init()
    print(f"Loaded {len(load_notes())} notes from {NOTES_DIR}.")
    print("Start a message with /think for a slower, more careful answer. Ctrl+D to quit.")
    messages = []
    conversation_id = None

    while True:
        try:
            question = input("\n> ")
        except (EOFError, KeyboardInterrupt):
            print()
            break
        think = question.split(maxsplit=1)[:1] == ["/think"]
        if think:
            question = question.strip().removeprefix("/think")
        question = question.strip()
        if not question:
            continue
        messages.append({"role": "user", "content": question})

        start, first_token, last_kind = time.monotonic(), None, None
        answer, reasoning, timings, model = [], [], None, None
        try:
            for kind, value in chat(messages, think=think):
                if kind == "timings":
                    timings = value
                    continue
                if kind == "model":
                    model = value
                    continue
                first_token = first_token or time.monotonic()
                if last_kind and kind != last_kind:
                    print("\n")
                last_kind = kind
                # The model "thinks" before answering; show that dimmed.
                style = DIM if kind == "reasoning" else ""
                print(f"{style}{value}{RESET if style else ''}", end="", flush=True)
                (answer if kind == "content" else reasoning).append(value)
        except urllib.error.HTTPError as error:
            print(f"Server error {error.code}: {error.read().decode()}")
            messages.pop()
            continue
        messages.append({"role": "assistant", "content": "".join(answer)})
        conversation_id, _ = chats.save_turn(
            conversation_id, question, "".join(answer),
            reasoning="".join(reasoning), think=think, model=model, timings=timings,
            latency_ms=round((time.monotonic() - start) * 1000),
        )

        if timings and first_token:
            # The server caches the prompt prefix it has already read, so only
            # new tokens (the latest question) cost reading time on later turns.
            total = timings["cache_n"] + timings["prompt_n"]
            print(
                f"\n\n{DIM}[prompt {total} tokens, {timings['cache_n']} cached"
                f" · first token after {first_token - start:.1f}s"
                f" · {timings['predicted_per_second']:.0f} tok/s]{RESET}"
            )


if __name__ == "__main__":
    main()
