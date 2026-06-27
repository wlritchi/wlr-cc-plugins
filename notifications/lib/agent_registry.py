# vim: filetype=python
"""Agent directory: pure registration/identity logic plus per-agent persistence.

Phase A of the agent-messaging design. This module has no daemon/WebSocket
dependency: the clock is injected (``now: float``) and session liveness is
supplied as a callable, so the registration rules are fully testable without
spawning anything. Records persist one JSON file per agent under
``<data_dir>/agents/<safe_name>.json``.
"""

import json
import re
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

import storage

DEFAULT_THRESHOLD = "direct"
_THRESHOLDS = frozenset({"all", "direct", "urgent"})
_NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$")
_NAME_MIN = 2
_NAME_MAX = 64


class AgentRegistryError(ValueError):
    """Base class for registry errors; the daemon maps these to ERROR replies."""


class NameTaken(AgentRegistryError):
    """The requested name is held by a different, still-claimed session."""


class InvalidName(AgentRegistryError):
    """The requested name is not valid kebab-case within length bounds."""


class InvalidThreshold(AgentRegistryError):
    """The requested wake threshold is not one of {all, direct, urgent}."""


class NotRegistered(AgentRegistryError):
    """The session owns no agent record."""


@dataclass
class AgentRecord:
    name: str
    session_id: str
    description: str = ""
    capabilities: str = ""
    working_dir: str = ""
    default_threshold: str = DEFAULT_THRESHOLD
    registered_at: float = 0.0
    last_seen: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "AgentRecord":
        return cls(
            name=data["name"],
            session_id=data["session_id"],
            description=data.get("description", ""),
            capabilities=data.get("capabilities", ""),
            working_dir=data.get("working_dir", ""),
            default_threshold=data.get("default_threshold", DEFAULT_THRESHOLD),
            registered_at=data.get("registered_at", 0.0),
            last_seen=data.get("last_seen", 0.0),
        )


def _validate_name(name: str) -> None:
    if not (_NAME_MIN <= len(name) <= _NAME_MAX) or _NAME_RE.match(name) is None:
        raise InvalidName(
            f"invalid agent name {name!r}: must be {_NAME_MIN}-{_NAME_MAX} "
            "chars, lowercase kebab-case (a-z, 0-9, hyphens; no leading/trailing hyphen)"
        )


def _validate_threshold(threshold: str) -> None:
    if threshold not in _THRESHOLDS:
        raise InvalidThreshold(
            f"invalid threshold {threshold!r}: must be one of "
            f"{', '.join(sorted(_THRESHOLDS))}"
        )


class AgentRegistry:
    def __init__(self, data_dir: Path) -> None:
        self._dir: Path = Path(data_dir) / "agents"
        self._by_name: dict[str, AgentRecord] = {}
        for data in storage.load_json_dir(self._dir):
            try:
                record = AgentRecord.from_dict(data)
            except (KeyError, TypeError):
                continue
            self._by_name[record.name] = record

    def _path(self, name: str) -> Path:
        return self._dir / f"{storage.safe_name(name)}.json"

    def _persist(self, record: AgentRecord) -> None:
        self._by_name[record.name] = record
        storage.atomic_write(self._path(record.name), json.dumps(record.to_dict()))

    def _remove(self, record: AgentRecord) -> None:
        self._by_name.pop(record.name, None)
        self._path(record.name).unlink(missing_ok=True)

    def get_by_session(self, session_id: str) -> AgentRecord | None:
        for record in self._by_name.values():
            if record.session_id == session_id:
                return record
        return None

    def list(self) -> list[AgentRecord]:
        return list(self._by_name.values())

    def register(
        self,
        session_id: str,
        name: str,
        *,
        now: float,
        is_session_live: Callable[[str], bool],
        ttl: float,
        description: str = "",
        capabilities: str = "",
        working_dir: str = "",
        default_threshold: str | None = None,
    ) -> AgentRecord:
        _validate_name(name)
        if default_threshold is not None:
            _validate_threshold(default_threshold)

        # Collision: the desired name is held by a *different* session. Reject
        # while that owner is live or still within the reclaim-grace window;
        # otherwise the stale holder is reclaimed (same slug, overwritten below).
        holder = self._by_name.get(name)
        if holder is not None and holder.session_id != session_id:
            within_grace = (now - holder.last_seen) < ttl
            if is_session_live(holder.session_id) or within_grace:
                raise NameTaken(f"name {name!r} is already taken")
            self._remove(holder)

        # One identity per session: if this session already owns a *different*
        # name, release that record (its slug differs, so delete the old file).
        owned = self.get_by_session(session_id)
        if owned is not None and owned.name != name:
            self._remove(owned)

        # Whatever remains under ``name`` is now owned by this session, if
        # anything: an idempotent self-update that preserves registered_at.
        prior = self._by_name.get(name)
        if prior is not None and prior.session_id == session_id:
            registered_at = prior.registered_at
            threshold = (
                default_threshold
                if default_threshold is not None
                else prior.default_threshold
            )
        else:
            registered_at = now
            threshold = (
                default_threshold
                if default_threshold is not None
                else DEFAULT_THRESHOLD
            )

        record = AgentRecord(
            name=name,
            session_id=session_id,
            description=description,
            capabilities=capabilities,
            working_dir=working_dir,
            default_threshold=threshold,
            registered_at=registered_at,
            last_seen=now,
        )
        self._persist(record)
        return record

    def unregister(self, session_id: str) -> AgentRecord | None:
        record = self.get_by_session(session_id)
        if record is None:
            return None
        self._remove(record)
        return record

    def set_availability(self, session_id: str, default_threshold: str) -> AgentRecord:
        _validate_threshold(default_threshold)
        record = self.get_by_session(session_id)
        if record is None:
            raise NotRegistered("this session is not registered as an agent")
        record.default_threshold = default_threshold
        self._persist(record)
        return record

    def touch(self, session_id: str, now: float) -> None:
        record = self.get_by_session(session_id)
        if record is None:
            return
        record.last_seen = now
        self._persist(record)
