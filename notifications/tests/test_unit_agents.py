# vim: filetype=python
"""Unit tests for the agent registry (Phase A): registration, the one-identity
-per-session and name-reclaim rules, availability, validation, and persistence
round-trips. The clock is injected via explicit ``now`` values; nothing here
touches wall-clock time."""

from pathlib import Path

import pytest

import agent_registry as ar
import storage


def _never_live(_session_id: str) -> bool:
    return False


def _always_live(_session_id: str) -> bool:
    return True


def _agent_path(data_dir: Path, name: str) -> Path:
    return data_dir / "agents" / f"{storage.safe_name(name)}.json"


# --- registration happy path -------------------------------------------------


def test_register_happy_path_and_fields(tmp_path: Path) -> None:
    reg = ar.AgentRegistry(tmp_path)
    rec = reg.register(
        "sess-1",
        "frontend",
        now=1000.0,
        is_session_live=_never_live,
        ttl=900.0,
        description="UI work",
        capabilities="react, css",
        working_dir="/repo/ui",
        default_threshold="urgent",
    )
    assert rec.name == "frontend"
    assert rec.session_id == "sess-1"
    assert rec.description == "UI work"
    assert rec.capabilities == "react, css"
    assert rec.working_dir == "/repo/ui"
    assert rec.default_threshold == "urgent"
    assert rec.registered_at == 1000.0
    assert rec.last_seen == 1000.0

    assert reg.get_by_session("sess-1") == rec
    assert [r.name for r in reg.list()] == ["frontend"]
    assert _agent_path(tmp_path, "frontend").is_file()


def test_register_defaults(tmp_path: Path) -> None:
    reg = ar.AgentRegistry(tmp_path)
    rec = reg.register("s1", "backend", now=1.0, is_session_live=_never_live, ttl=900.0)
    assert rec.default_threshold == "direct"
    assert rec.description == ""
    assert rec.capabilities == ""
    assert rec.working_dir == ""


# --- idempotent self-update --------------------------------------------------


def test_idempotent_self_update_preserves_registered_at(tmp_path: Path) -> None:
    reg = ar.AgentRegistry(tmp_path)
    reg.register(
        "s1",
        "api",
        now=100.0,
        is_session_live=_never_live,
        ttl=900.0,
        description="old",
        default_threshold="direct",
    )
    # Re-registering its own name is never blocked by the collision check, even
    # if liveness reports the session as live.
    rec = reg.register(
        "s1",
        "api",
        now=200.0,
        is_session_live=_always_live,
        ttl=900.0,
        description="new",
        default_threshold="urgent",
    )
    assert rec.description == "new"
    assert rec.default_threshold == "urgent"
    assert rec.registered_at == 100.0  # preserved across the update
    assert rec.last_seen == 200.0  # bumped
    assert len(reg.list()) == 1


def test_self_update_preserves_threshold_when_unspecified(tmp_path: Path) -> None:
    reg = ar.AgentRegistry(tmp_path)
    reg.register(
        "s1",
        "api",
        now=1.0,
        is_session_live=_never_live,
        ttl=900.0,
        default_threshold="urgent",
    )
    rec = reg.register(
        "s1", "api", now=2.0, is_session_live=_never_live, ttl=900.0, description="d"
    )
    assert rec.default_threshold == "urgent"
    assert rec.description == "d"


# --- rename / one identity per session --------------------------------------


def test_rename_releases_old_name_and_file(tmp_path: Path) -> None:
    reg = ar.AgentRegistry(tmp_path)
    reg.register("s1", "first", now=1.0, is_session_live=_never_live, ttl=900.0)
    old_path = _agent_path(tmp_path, "first")
    assert old_path.is_file()

    reg.register("s1", "second", now=2.0, is_session_live=_never_live, ttl=900.0)
    assert reg.get_by_session("s1").name == "second"
    assert [r.name for r in reg.list()] == ["second"]
    assert not old_path.exists()
    assert _agent_path(tmp_path, "second").is_file()


# --- collisions & reclaim ----------------------------------------------------


def test_collision_rejected_while_owner_live(tmp_path: Path) -> None:
    reg = ar.AgentRegistry(tmp_path)
    reg.register("owner", "shared", now=1.0, is_session_live=_never_live, ttl=900.0)

    def live(session_id: str) -> bool:
        return session_id == "owner"

    with pytest.raises(ar.NameTaken):
        reg.register("intruder", "shared", now=2.0, is_session_live=live, ttl=900.0)

    assert reg.get_by_session("owner").name == "shared"
    assert reg.get_by_session("intruder") is None


def test_collision_rejected_within_grace(tmp_path: Path) -> None:
    reg = ar.AgentRegistry(tmp_path)
    reg.register("owner", "shared", now=1000.0, is_session_live=_never_live, ttl=900.0)
    # Owner is offline, but now - last_seen = 500 < ttl 900 -> still protected.
    with pytest.raises(ar.NameTaken):
        reg.register(
            "intruder", "shared", now=1500.0, is_session_live=_never_live, ttl=900.0
        )
    assert reg.get_by_session("owner").name == "shared"


def test_reclaim_after_grace(tmp_path: Path) -> None:
    reg = ar.AgentRegistry(tmp_path)
    reg.register("owner", "shared", now=1000.0, is_session_live=_never_live, ttl=900.0)
    # now - last_seen = 1000 >= ttl 900 and owner offline -> reclaim.
    rec = reg.register(
        "intruder", "shared", now=2000.0, is_session_live=_never_live, ttl=900.0
    )
    assert rec.session_id == "intruder"
    assert rec.registered_at == 2000.0  # fresh identity, not the old timestamp
    assert reg.get_by_session("owner") is None
    assert [r.name for r in reg.list()] == ["shared"]
    assert _agent_path(tmp_path, "shared").is_file()


def test_zero_ttl_reclaims_immediately_when_offline(tmp_path: Path) -> None:
    reg = ar.AgentRegistry(tmp_path)
    reg.register("owner", "shared", now=1000.0, is_session_live=_never_live, ttl=0.0)
    rec = reg.register(
        "intruder", "shared", now=1000.0, is_session_live=_never_live, ttl=0.0
    )
    assert rec.session_id == "intruder"


# --- unregister --------------------------------------------------------------


def test_unregister_removes_record_and_file(tmp_path: Path) -> None:
    reg = ar.AgentRegistry(tmp_path)
    reg.register("s1", "gone", now=1.0, is_session_live=_never_live, ttl=900.0)
    path = _agent_path(tmp_path, "gone")
    assert path.is_file()

    removed = reg.unregister("s1")
    assert removed is not None and removed.name == "gone"
    assert not path.exists()
    assert reg.get_by_session("s1") is None
    assert reg.unregister("s1") is None  # idempotent


# --- availability ------------------------------------------------------------


def test_set_availability_updates_and_persists(tmp_path: Path) -> None:
    reg = ar.AgentRegistry(tmp_path)
    reg.register(
        "s1",
        "aa",
        now=1.0,
        is_session_live=_never_live,
        ttl=900.0,
        default_threshold="direct",
    )
    rec = reg.set_availability("s1", "all")
    assert rec.default_threshold == "all"

    reloaded = ar.AgentRegistry(tmp_path)
    assert reloaded.get_by_session("s1").default_threshold == "all"


def test_set_availability_not_registered(tmp_path: Path) -> None:
    reg = ar.AgentRegistry(tmp_path)
    with pytest.raises(ar.NotRegistered):
        reg.set_availability("ghost", "all")


# --- validation --------------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    ["", "a", "A", "ab_c", "-ab", "ab-", "has space", "UPPER", "x" * 65, "naïve"],
)
def test_invalid_name(tmp_path: Path, bad: str) -> None:
    reg = ar.AgentRegistry(tmp_path)
    with pytest.raises(ar.InvalidName):
        reg.register("s1", bad, now=1.0, is_session_live=_never_live, ttl=900.0)


@pytest.mark.parametrize("good", ["ab", "a1", "front-end", "a-b-c", "x" * 64])
def test_valid_names_accepted(tmp_path: Path, good: str) -> None:
    reg = ar.AgentRegistry(tmp_path)
    rec = reg.register("s1", good, now=1.0, is_session_live=_never_live, ttl=900.0)
    assert rec.name == good


def test_invalid_threshold_on_register(tmp_path: Path) -> None:
    reg = ar.AgentRegistry(tmp_path)
    with pytest.raises(ar.InvalidThreshold):
        reg.register(
            "s1",
            "ok",
            now=1.0,
            is_session_live=_never_live,
            ttl=900.0,
            default_threshold="loud",
        )


def test_invalid_threshold_on_set_availability(tmp_path: Path) -> None:
    reg = ar.AgentRegistry(tmp_path)
    reg.register("s1", "ok", now=1.0, is_session_live=_never_live, ttl=900.0)
    with pytest.raises(ar.InvalidThreshold):
        reg.set_availability("s1", "loud")


def test_exception_hierarchy() -> None:
    for exc in (ar.NameTaken, ar.InvalidName, ar.InvalidThreshold, ar.NotRegistered):
        assert issubclass(exc, ar.AgentRegistryError)
    assert issubclass(ar.AgentRegistryError, ValueError)


# --- touch -------------------------------------------------------------------


def test_touch_updates_last_seen_and_persists(tmp_path: Path) -> None:
    reg = ar.AgentRegistry(tmp_path)
    reg.register("s1", "aa", now=100.0, is_session_live=_never_live, ttl=900.0)
    reg.touch("s1", now=500.0)
    assert reg.get_by_session("s1").last_seen == 500.0

    reloaded = ar.AgentRegistry(tmp_path)
    assert reloaded.get_by_session("s1").last_seen == 500.0


def test_touch_unknown_session_is_noop(tmp_path: Path) -> None:
    reg = ar.AgentRegistry(tmp_path)
    reg.touch("ghost", now=1.0)  # must not raise
    assert reg.list() == []


# --- persistence round-trip --------------------------------------------------


def test_persistence_round_trip(tmp_path: Path) -> None:
    reg = ar.AgentRegistry(tmp_path)
    a = reg.register(
        "s1",
        "alpha",
        now=10.0,
        is_session_live=_never_live,
        ttl=900.0,
        description="d1",
        capabilities="c1",
        working_dir="/w1",
        default_threshold="all",
    )
    b = reg.register(
        "s2",
        "beta",
        now=20.0,
        is_session_live=_never_live,
        ttl=900.0,
        default_threshold="urgent",
    )

    reloaded = ar.AgentRegistry(tmp_path)
    assert reloaded.get_by_session("s1") == a
    assert reloaded.get_by_session("s2") == b
    assert {r.name for r in reloaded.list()} == {"alpha", "beta"}


# --- storage helpers ---------------------------------------------------------


def test_load_json_dir_missing_returns_empty(tmp_path: Path) -> None:
    assert storage.load_json_dir(tmp_path / "nope") == []


def test_load_json_dir_skips_invalid(tmp_path: Path) -> None:
    d = tmp_path / "d"
    d.mkdir()
    storage.atomic_write(d / "good.json", '{"k": 1}')
    (d / "bad.json").write_text("{not json")
    (d / "scalar.json").write_text("42")
    (d / "ignore.txt").write_text("nope")
    assert storage.load_json_dir(d) == [{"k": 1}]


def test_atomic_write_creates_parents_and_leaves_no_tmp(tmp_path: Path) -> None:
    target = tmp_path / "a" / "b" / "c.json"
    storage.atomic_write(target, "hello")
    assert target.read_text() == "hello"
    assert not target.with_name("c.json.tmp").exists()


def test_safe_name_slugs_unsafe_chars(tmp_path: Path) -> None:
    assert storage.safe_name("a b/c") == "a_b_c"
    assert storage.safe_name("ok-name.json") == "ok-name.json"
