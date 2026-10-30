"""Console output and secret redaction. Kept free of other taxameter imports so providers can use it."""

from __future__ import annotations

import sys
import time
from datetime import UTC, datetime
from typing import Any


class Redactor:
    """Replaces every registered secret in anything that is printed, logged or saved."""

    def __init__(self) -> None:
        self.secrets: set[str] = set()

    def add(self, s: str | None) -> None:
        if s and len(s) >= 6:
            self.secrets.add(s)

    def __call__(self, text: Any) -> str:
        text = str(text)
        for s in self.secrets:
            text = text.replace(s, "***")
        return text


redact = Redactor()


def ts(t: float | None = None) -> str:
    return datetime.fromtimestamp(time.time() if t is None else t, tz=UTC).astimezone().strftime("%H:%M:%S")


def iso(t: float | None) -> str:
    return datetime.fromtimestamp(t, tz=UTC).astimezone().strftime("%Y-%m-%d %H:%M:%S") if t else "-"


class Out:
    json_mode = False  # stdout carries only the JSON result, log lines go to stderr

    @classmethod
    def say(cls, msg: str) -> None:
        stream = sys.stderr if cls.json_mode else sys.stdout
        print(f"[{ts()}] {redact(msg)}", file=stream, flush=True)


say = Out.say
