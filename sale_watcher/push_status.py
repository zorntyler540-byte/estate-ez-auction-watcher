"""Per-sale Estate-Ez push status: last push time, result, and errors.

Each sale keeps its own record, so one sale's update never overwrites another's,
and a "skipped" pass never hides the last real result or the last error.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

SENT = "sent"
PARTIAL = "partial"
SKIPPED = "skipped"
FAILED = "failed"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def clock(moment: datetime | None) -> str:
    """Local wall-clock time like ``2:31:05 PM``."""
    if moment is None:
        return "—"
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone().strftime("%I:%M:%S %p").lstrip("0")


@dataclass(frozen=True)
class PushResult:
    """Outcome of one engagement push attempt for one sale."""

    sale_id: int
    outcome: str
    message: str = ""
    at: datetime = field(default_factory=_now)
    item_count: int = 0
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.outcome in (SENT, PARTIAL)


@dataclass
class SalePushStatus:
    sale_id: int
    last_attempt_at: datetime | None = None
    last_outcome: str = ""
    last_message: str = ""
    last_sent_at: datetime | None = None
    last_sent_message: str = ""
    last_sent_items: int = 0
    last_sent_partial: bool = False
    last_error: str = ""
    last_error_at: datetime | None = None
    consecutive_failures: int = 0
    attempts: int = 0

    def record(self, result: PushResult) -> bool:
        """Apply ``result``. Returns False (and changes nothing) for another sale or an older attempt."""
        if int(result.sale_id) != int(self.sale_id):
            return False
        if self.last_attempt_at is not None and result.at < self.last_attempt_at:
            return False
        self.attempts += 1
        self.last_attempt_at = result.at
        self.last_outcome = result.outcome
        self.last_message = result.message
        if result.ok:
            self.last_sent_at = result.at
            self.last_sent_message = result.message
            self.last_sent_items = int(result.item_count or 0)
            self.last_sent_partial = result.outcome == PARTIAL
            self.consecutive_failures = 0
        elif result.outcome == FAILED:
            self.last_error = result.error or result.message
            self.last_error_at = result.at
            self.consecutive_failures += 1
        return True

    @property
    def has_current_error(self) -> bool:
        if not self.last_error_at:
            return False
        return self.last_sent_at is None or self.last_error_at > self.last_sent_at

    @property
    def level(self) -> str:
        """``idle`` / ``ok`` / ``warn`` / ``error`` for the status indicator colour."""
        if not self.last_outcome:
            return "idle"
        if self.has_current_error:
            return "error"
        if self.last_sent_partial or (self.last_outcome == SKIPPED and not self.last_sent_at):
            return "warn"
        return "ok"

    def summary(self) -> str:
        """Short line for the status bar."""
        if not self.last_outcome:
            return "Estate-Ez push: not yet"
        if self.has_current_error:
            when = clock(self.last_error_at)
            tail = f" · last sent {clock(self.last_sent_at)}" if self.last_sent_at else ""
            return f"Estate-Ez push failed {when}{tail}"
        if self.last_sent_at:
            kind = "partial" if self.last_sent_partial else "sent"
            return f"Estate-Ez push {kind} {clock(self.last_sent_at)} · {self.last_sent_items} lots"
        return "Estate-Ez push: skipped"

    def details(self) -> list[tuple[str, str]]:
        """Label / value rows for the push status window."""
        rows = [
            ("Last attempt", clock(self.last_attempt_at)),
            ("Result", (self.last_outcome or "—").capitalize()),
            ("Message", self.last_message or "—"),
            ("Last successful push", clock(self.last_sent_at)),
            ("Lots in last push", str(self.last_sent_items) if self.last_sent_at else "—"),
        ]
        if self.last_error:
            rows.append(("Last error", f"{clock(self.last_error_at)} — {self.last_error}"))
        if self.consecutive_failures:
            rows.append(("Failures in a row", str(self.consecutive_failures)))
        return rows
