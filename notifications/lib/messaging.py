# vim: filetype=python
"""Pure helpers for the agent-messaging attention model (Phase B).

This module is the home of the vocabulary shared by the daemon (sender-side
loudness) and the relay shim (receiver-side attention): the intent enum, the
``ambient < direct < urgent`` level scale and the ``all < direct < urgent``
threshold scale (one shared ordinal axis), the wake-gating predicates, and the
topic-key / name-validation helpers. Nothing here touches the clock, the
filesystem, or any WebSocket — it is all referentially transparent so both sides
of the daemon/shim split can compute the same surface decision.
"""

import re

INTENTS = frozenset({"fyi", "question", "request", "reply"})  # reaction = Phase C

# Level (how loud the sender made a message *for this recipient*) and threshold
# (the recipient's bar) live on one shared ordinal axis so ``should_surface`` is
# a plain ``>=``. ``all`` (0) admits everything; ``urgent`` (2) admits only an
# urgent message.
_LEVELS = {"ambient": 0, "direct": 1, "urgent": 2}
_THRESHOLDS = {"all": 0, "direct": 1, "urgent": 2}
THRESHOLD_NAMES = frozenset(_THRESHOLDS)

# Channel names reuse the agent-name rule: lowercase kebab-case, 2-64 chars, no
# leading/trailing hyphen.
_NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$")
_NAME_MIN = 2
_NAME_MAX = 64


class MessagingError(ValueError):
    """Base class for messaging errors; the daemon maps these to ERROR replies."""


class InvalidChannelName(MessagingError):
    """The channel name is not valid kebab-case within length bounds."""


class InvalidThreshold(MessagingError):
    """The threshold is not one of {all, direct, urgent}."""


def compute_level(*, severity: str, addressed: bool) -> str:
    """Sender-side loudness for one recipient.

    ``urgent`` when the message is an ``@here`` (``severity == "high"``); else
    ``direct`` when the recipient is addressed (a DM recipient or ``@``-mentioned);
    else ``ambient``. Intent is orthogonal — it carries reply/display semantics,
    not loudness.
    """
    if severity == "high":
        return "urgent"
    if addressed:
        return "direct"
    return "ambient"


def effective_threshold(override: str | None, default: str) -> str:
    """The recipient's bar for a topic: the per-topic override if set, else the
    agent record's global default."""
    return override or default


def should_surface(level: str, threshold: str) -> bool:
    """True iff a message at ``level`` clears ``threshold`` on the shared scale."""
    return _LEVELS[level] >= _THRESHOLDS[threshold]


def dm_key(names: list[str]) -> str:
    """Stable topic key for a DM among ``names``.

    Deduped and sorted so a given participant set always maps to one thread,
    regardless of who initiated or the order ``to`` was given in.
    """
    return "dm:" + ",".join(sorted(set(names)))


def channel_key(name: str) -> str:
    """Topic key for a named channel (validates ``name`` first)."""
    validate_channel_name(name)
    return "chan:" + name


def validate_channel_name(name: str) -> None:
    if not (_NAME_MIN <= len(name) <= _NAME_MAX) or _NAME_RE.match(name) is None:
        raise InvalidChannelName(
            f"invalid channel name {name!r}: must be {_NAME_MIN}-{_NAME_MAX} "
            "chars, lowercase kebab-case (a-z, 0-9, hyphens; no leading/trailing hyphen)"
        )


def validate_threshold(threshold: str) -> None:
    if threshold not in _THRESHOLDS:
        raise InvalidThreshold(
            f"invalid threshold {threshold!r}: must be one of "
            f"{', '.join(sorted(_THRESHOLDS))}"
        )
