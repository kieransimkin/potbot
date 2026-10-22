"""Incremental output redaction that retains secret prefixes across deltas."""

from __future__ import annotations

from potbot.domain.errors import InvalidInputError
from potbot.tools.secrets import CaptureRedactor


class StreamingRedactor:
    def __init__(self, redactor: CaptureRedactor) -> None:
        if redactor.reject_patterns:
            raise InvalidInputError(
                "Streaming secret redaction does not accept whole-output rejection rules."
            )
        self.redactor = redactor
        self.pending = b""

    def feed(self, text: str, *, final: bool = False, complete: bool = True) -> str:
        self.pending += text.encode("utf-8")
        if final:
            result = self.redactor.redact_bytes(self.pending, complete=complete)
            self.pending = b""
            return result.decode("utf-8")
        cut = max(0, len(self.pending) - self.redactor.overlap_bytes + 1)
        for pattern in self.redactor.patterns:
            start = self.pending.find(pattern)
            while 0 <= start < cut:
                if start + len(pattern) > cut:
                    cut = start
                    break
                start = self.pending.find(pattern, start + 1)
        while cut and cut < len(self.pending) and self.pending[cut] & 0xC0 == 0x80:
            cut -= 1
        result = self.redactor.redact_bytes(self.pending[:cut])
        self.pending = self.pending[cut:]
        return result.decode("utf-8")
