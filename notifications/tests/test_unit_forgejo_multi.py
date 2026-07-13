# vim: filetype=python
"""Unit tests for MULTI-INSTANCE Forgejo PR notifications (docs/specs/2026-07-11).

Covers the additive multi-instance seam, keeping the default instance byte-identical:

  - storage_key alias dimension (default unprefixed, named prefixed)
  - relay ref parsing (bare / aliased / full-host-rejected / bad-alias)
  - daemon config parsing of FORGEJO_<ALIAS>_* pairs + the canonicalization config-error
  - load_trackers instance round-trip + the base_url mismatch-skip guard
  - the wire `instance` echo + the version-skew cleanup (old-daemon-no-echo -> warn vs.
    unsubscribe-stray, incl. the mirrored-repo don't-destroy case)
  - the auth-error message naming the right per-instance token env var
  - per-instance client selection (default vs named vs unknown)

The daemon and relay are imported by path (their filenames are hyphenated scripts), the
same idiom as test_unit_pr / test_unit_core."""

import importlib.util
import json
from pathlib import Path

import anyio
import pytest

import forgejo_client as fc
import pr_monitor as pm


# --------------------------------------------------------------------------- #
# module loaders (hyphenated script filenames)
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def daemon():
    path = Path(__file__).resolve().parent.parent / "daemon" / "notifications-daemon.py"
    spec = importlib.util.spec_from_file_location("notifications_daemon", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def relay():
    path = Path(__file__).resolve().parent.parent / "mcp" / "notifications-server.py"
    spec = importlib.util.spec_from_file_location("notifications_relay", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --------------------------------------------------------------------------- #
# storage_key alias dimension
# --------------------------------------------------------------------------- #


class TestStorageKeyAlias:
    def test_default_forgejo_unprefixed_by_alias(self):
        # Default instance (no alias) is byte-identical to v1: forgejo:owner/repo#n.
        assert pm.storage_key("forgejo", "o", "r", 1) == "forgejo:o/r#1"
        assert pm.storage_key("forgejo", "o", "r", 1, "") == "forgejo:o/r#1"
        assert pm.storage_key("forgejo", "o", "r", 1, None) == "forgejo:o/r#1"

    def test_named_instance_folds_alias(self):
        assert (
            pm.storage_key("forgejo", "o", "r", 1, "external")
            == "forgejo:external:o/r#1"
        )

    def test_github_ignores_alias(self):
        # github has no multi-instance dimension in v1; the alias is ignored and the key
        # stays unprefixed (byte-identical).
        assert pm.storage_key("github", "o", "r", 1) == "o/r#1"
        assert pm.storage_key("github", "o", "r", 1, "external") == "o/r#1"

    def test_named_and_default_and_github_all_distinct(self):
        keys = {
            pm.storage_key("github", "o", "r", 1),
            pm.storage_key("forgejo", "o", "r", 1),
            pm.storage_key("forgejo", "o", "r", 1, "external"),
        }
        assert len(keys) == 3  # one owner/repo#n cannot collide across the three


# --------------------------------------------------------------------------- #
# relay ref parsing
# --------------------------------------------------------------------------- #


class TestRefParsing:
    def test_bare_ref_is_default_instance(self, relay):
        assert relay._parse_forgejo_ref("owner/repo#4") == ("", "owner", "repo", 4)

    def test_aliased_ref(self, relay):
        assert relay._parse_forgejo_ref("external:owner/repo#12") == (
            "external",
            "owner",
            "repo",
            12,
        )

    def test_kebab_alias(self, relay):
        assert relay._parse_forgejo_ref("self-hosted:o/r#1") == (
            "self-hosted",
            "o",
            "r",
            1,
        )

    def test_full_host_url_rejected(self, relay):
        err = relay._parse_forgejo_ref("https://fj.example.com/o/r#4")
        assert isinstance(err, str) and "Invalid PR reference" in err

    def test_dotted_host_prefix_rejected(self, relay):
        err = relay._parse_forgejo_ref("fj.example.com:o/r#4")
        assert isinstance(err, str) and "Full-host" in err

    def test_host_port_prefix_rejected(self, relay):
        # host:8080:o/r#4 — the first colon must not peel a host-like alias and leave a
        # colon-bearing owner; the extra colon in the remainder is what rejects it.
        err = relay._parse_forgejo_ref("host:8080:o/r#4")
        assert isinstance(err, str) and "Full-host" in err

    def test_uppercase_alias_rejected(self, relay):
        err = relay._parse_forgejo_ref("EXTERNAL:o/r#4")
        assert isinstance(err, str) and "kebab-case" in err

    def test_garbage_ref_rejected(self, relay):
        assert isinstance(relay._parse_forgejo_ref("not-a-ref"), str)

    def test_display_helpers(self, relay):
        assert relay._forgejo_ref_display("", "o", "r", 4) == "o/r#4"
        assert relay._forgejo_ref_display("ext", "o", "r", 4) == "ext:o/r#4"


# --------------------------------------------------------------------------- #
# daemon config parsing + canonicalization
# --------------------------------------------------------------------------- #


class TestConfigParsing:
    def test_parses_aliased_pairs(self, daemon):
        env = {
            "FORGEJO_EXTERNAL_API_URL": "https://ext.example",
            "FORGEJO_EXTERNAL_TOKEN": "e",
            "FORGEJO_SELF_HOSTED_API_URL": "https://self.example/",  # underscore -> kebab
            "FORGEJO_SELF_HOSTED_TOKEN": "s",
        }
        inst = daemon._build_forgejo_instances(
            env, default_url="https://fj.example/api/v1"
        )
        assert sorted(inst) == ["external", "self-hosted"]
        assert inst["external"].base_url == "https://ext.example/api/v1"
        assert inst["self-hosted"].base_url == "https://self.example/api/v1"
        # token threaded onto the right client (Gitea `token <PAT>` scheme)
        assert inst["external"]._headers()["Authorization"] == "token e"

    def test_bare_forgejo_api_url_is_not_an_instance(self, daemon):
        # The unaliased FORGEJO_API_URL is the DEFAULT instance, not a named one — it must
        # never be parsed into FJ_INSTANCES (no alias segment between prefix and suffix).
        env = {"FORGEJO_API_URL": "https://fj.example", "FORGEJO_TOKEN": "t"}
        assert daemon._build_forgejo_instances(env, default_url="") == {}

    def test_canonicalization_refuses_alias_equal_to_default(self, daemon, capsys):
        env = {
            "FORGEJO_DUP_API_URL": "https://fj.example/",  # normalizes to the default URL
            "FORGEJO_DUP_TOKEN": "d",
        }
        default_url = fc.ForgejoClient(base_url="https://fj.example").base_url
        inst = daemon._build_forgejo_instances(env, default_url=default_url)
        assert "dup" not in inst  # refused: one PR must not grow two trackers
        err = capsys.readouterr().err
        assert "CONFIG ERROR" in err and "dup" in err

    def test_token_env_naming(self, daemon):
        assert (
            daemon._forgejo_token_env("") == "FORGEJO_TOKEN"
        )  # default (v1-identical)
        assert daemon._forgejo_token_env("external") == "FORGEJO_EXTERNAL_TOKEN"
        assert daemon._forgejo_token_env("self-hosted") == "FORGEJO_SELF_HOSTED_TOKEN"

    def test_missing_url_value_ignored(self, daemon):
        env = {"FORGEJO_EMPTY_API_URL": "", "FORGEJO_EMPTY_TOKEN": "x"}
        assert daemon._build_forgejo_instances(env, default_url="") == {}


# --------------------------------------------------------------------------- #
# per-instance client selection + config-error resolution
# --------------------------------------------------------------------------- #


class TestClientSelection:
    def _wire(self, daemon):
        default = fc.ForgejoClient(base_url="https://fj.example", token="t")
        external = fc.ForgejoClient(base_url="https://ext.example", token="e")
        daemon.FJ = default
        daemon.FJ_INSTANCES = {"external": external}
        return default, external

    def test_default_and_named_selection(self, daemon):
        default, external = self._wire(daemon)
        try:
            assert daemon._pr_client("forgejo", "") is default
            assert daemon._pr_client("forgejo", "external") is external
            assert daemon._pr_client("forgejo", "nope") is None
            assert daemon._forgejo_aliases() == ["external"]
        finally:
            daemon.FJ = None
            daemon.FJ_INSTANCES = {}

    def test_config_error_messages(self, daemon):
        self._wire(daemon)
        try:
            assert daemon._forgejo_config_error("github", "") is None
            assert daemon._forgejo_config_error("forgejo", "") is None
            assert daemon._forgejo_config_error("forgejo", "external") is None
            err = daemon._forgejo_config_error("forgejo", "ghost")
            assert "unknown Forgejo instance 'ghost'" in err
            assert "external" in err  # lists configured aliases (names only)
            assert "https://" not in err  # never leaks URLs
        finally:
            daemon.FJ = None
            daemon.FJ_INSTANCES = {}

    def test_default_unconfigured_message_is_v1(self, daemon):
        daemon.FJ = None
        daemon.FJ_INSTANCES = {}
        err = daemon._forgejo_config_error("forgejo", "")
        assert "set FORGEJO_API_URL / FORGEJO_TOKEN" in err

    def test_pr_clients_map_covers_default_and_named(self, daemon):
        default, external = self._wire(daemon)
        daemon.GH = object()
        try:
            clients = daemon._pr_clients()
            assert clients["github"] is daemon.GH
            assert clients["forgejo"] is default
            assert clients[("forgejo", "external")] is external
        finally:
            daemon.FJ = None
            daemon.FJ_INSTANCES = {}
            daemon.GH = None


# --------------------------------------------------------------------------- #
# load_trackers: instance round-trip + base_url mismatch guard
# --------------------------------------------------------------------------- #


class _Client:
    def __init__(self, base_url):
        self.base_url = base_url


class TestLoadTrackersMultiInstance:
    def _seed(self, tmp_path, monkeypatch, *, instance, base_url):
        monkeypatch.setenv("NOTIFICATIONS_DATA_DIR", str(tmp_path))
        t = pm.PRTracker(
            "o", "r", 1, None, provider="forgejo", base_url=base_url, instance=instance
        )
        t.snapshot = {"timeline": {}, "labels": [], "state": "open"}
        pm.save_state(t)
        return t

    def test_instance_roundtrips_and_selects_named_client(self, tmp_path, monkeypatch):
        url = "https://ext.example/api/v1"
        self._seed(tmp_path, monkeypatch, instance="external", base_url=url)
        # storage dir is alias-tagged
        assert pm._tracker_dir("forgejo:external:o/r#1").exists()
        client = _Client(url)
        loaded = {
            t.storage_key: t
            for t in pm.load_trackers({("forgejo", "external"): client})
        }
        t = loaded["forgejo:external:o/r#1"]
        assert t.instance == "external"
        assert t.base_url == url
        assert t.client is client
        assert t.key == "o/r#1"  # display ref stays bare

    def test_persisted_instance_field_present(self, tmp_path, monkeypatch):
        self._seed(
            tmp_path, monkeypatch, instance="external", base_url="https://ext/api/v1"
        )
        state = json.loads(
            (pm._tracker_dir("forgejo:external:o/r#1") / "state.json").read_text()
        )
        assert state["instance"] == "external"

    def test_default_instance_absent_field_loads_as_default(
        self, tmp_path, monkeypatch
    ):
        # A v1 default forgejo tracker on disk has NO instance field; it must load as the
        # default instance and key on the bare provider string.
        monkeypatch.setenv("NOTIFICATIONS_DATA_DIR", str(tmp_path))
        directory = pm._tracker_dir("forgejo:o/r#1")
        directory.mkdir(parents=True)
        (directory / "state.json").write_text(
            json.dumps(
                {
                    "owner": "o",
                    "repo": "r",
                    "number": 1,
                    "provider": "forgejo",
                    "base_url": "https://fj/api/v1",
                    "snapshot": {"timeline": {}, "state": "open"},
                }
            )
        )
        client = _Client("https://fj/api/v1")
        loaded = {t.storage_key: t for t in pm.load_trackers({"forgejo": client})}
        t = loaded["forgejo:o/r#1"]
        assert t.instance == ""
        assert t.client is client

    def test_base_url_mismatch_skips_tracker(self, tmp_path, monkeypatch, capsys):
        # The alias was repointed at a new URL since the tracker was written: the persisted
        # base_url disagrees with the configured client's -> skip (don't poll the wrong
        # instance).
        self._seed(
            tmp_path, monkeypatch, instance="external", base_url="https://OLD/api/v1"
        )
        client = _Client("https://NEW/api/v1")  # repointed
        loaded = pm.load_trackers({("forgejo", "external"): client})
        assert loaded == []  # skipped
        assert "disagrees" in capsys.readouterr().err

    def test_base_url_match_loads(self, tmp_path, monkeypatch):
        url = "https://same/api/v1"
        self._seed(tmp_path, monkeypatch, instance="external", base_url=url)
        loaded = pm.load_trackers({("forgejo", "external"): _Client(url)})
        assert len(loaded) == 1

    def test_unconfigured_named_instance_skipped(self, tmp_path, monkeypatch, capsys):
        # A forgejo tracker for an alias no longer configured is skipped, not crashed.
        self._seed(
            tmp_path, monkeypatch, instance="external", base_url="https://ext/api/v1"
        )
        loaded = pm.load_trackers({"forgejo": _Client("https://fj/api/v1")})
        assert loaded == []
        assert "no client" in capsys.readouterr().err

    def test_default_forgejo_unaffected_by_named_on_disk(self, tmp_path, monkeypatch):
        # A default and a named tracker for the same owner/repo#n coexist in distinct dirs.
        monkeypatch.setenv("NOTIFICATIONS_DATA_DIR", str(tmp_path))
        default = pm.PRTracker(
            "o", "r", 1, None, provider="forgejo", base_url="https://fj/api/v1"
        )
        named = pm.PRTracker(
            "o",
            "r",
            1,
            None,
            provider="forgejo",
            base_url="https://ext/api/v1",
            instance="external",
        )
        for t in (default, named):
            t.snapshot = {"timeline": {}, "state": "open"}
            pm.save_state(t)
        loaded = {
            t.storage_key: t
            for t in pm.load_trackers(
                {
                    "forgejo": _Client("https://fj/api/v1"),
                    ("forgejo", "external"): _Client("https://ext/api/v1"),
                }
            )
        }
        assert loaded["forgejo:o/r#1"].instance == ""
        assert loaded["forgejo:external:o/r#1"].instance == "external"


# --------------------------------------------------------------------------- #
# daemon handlers: storage-key derivation + instance echo
# --------------------------------------------------------------------------- #


class _FakeWS:
    def __init__(self):
        self.sent = []

    async def send(self, data):
        self.sent.append(json.loads(data))


class _FakeForgejoClient:
    """A ForgejoClient stand-in whose fetch_pr returns a fixed open snapshot, so a
    subscribe handler can baseline without a network/subprocess."""

    def __init__(self, base_url):
        self.base_url = base_url
        self.configured = True

    def should_throttle(self, threshold=50):
        return None

    async def fetch_pr(self, owner, repo, number):
        return {
            "pr": {
                "number": number,
                "title": "T",
                "html_url": "u",
                "state": "open",
                "merged": False,
                "mergeable": True,
                "head": {"sha": "a" * 40},
                "labels": [],
                "requested_reviewers": [],
            },
            "reviews": [],
            "review_comments": [],
            "issue_comments": [],
            "statuses": [],
        }


class TestDaemonHandlersInstance:
    def _reset(self, daemon):
        daemon.TRACKERS.clear()
        daemon.CONNECTIONS.clear()

    def test_subscribe_named_instance_echoes_and_keys_by_alias(
        self, daemon, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("NOTIFICATIONS_DATA_DIR", str(tmp_path))
        self._reset(daemon)
        external = _FakeForgejoClient("https://ext.example/api/v1")
        daemon.FJ = None
        daemon.FJ_INSTANCES = {"external": external}

        async def scenario():
            ws = _FakeWS()
            conn = daemon.Connection(ws)
            conn.session_id = "sidA"
            daemon.CONNECTIONS["sidA"] = conn
            await daemon._handle_subscribe(
                ws,
                conn,
                {
                    "session_id": "sidA",
                    "owner": "o",
                    "repo": "r",
                    "number": 1,
                    "instance": "external",
                    "req_id": 7,
                },
                provider="forgejo",
            )
            return ws

        try:
            ws = anyio.run(scenario)
            tracker_keys = set(daemon.TRACKERS)  # capture before teardown clears it
        finally:
            for t in list(daemon.TRACKERS.values()):
                if t.task is not None:
                    t.task.cancel()
            self._reset(daemon)
            daemon.FJ_INSTANCES = {}

        reply = ws.sent[-1]
        assert reply["type"] == daemon.wsproto.SUBSCRIBED
        assert reply["instance"] == "external"  # echoed back
        assert reply["pr"] == "o/r#1"  # display ref stays bare
        # tracker keyed by the alias-tagged storage key
        assert "forgejo:external:o/r#1" in tracker_keys

    def test_subscribe_unknown_instance_errors_with_aliases(
        self, daemon, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("NOTIFICATIONS_DATA_DIR", str(tmp_path))
        self._reset(daemon)
        daemon.FJ = _FakeForgejoClient("https://fj.example/api/v1")
        daemon.FJ_INSTANCES = {"external": _FakeForgejoClient("https://ext/api/v1")}

        async def scenario():
            ws = _FakeWS()
            conn = daemon.Connection(ws)
            conn.session_id = "sidA"
            await daemon._handle_subscribe(
                ws,
                conn,
                {
                    "session_id": "sidA",
                    "owner": "o",
                    "repo": "r",
                    "number": 1,
                    "instance": "ghost",
                    "req_id": 3,
                },
                provider="forgejo",
            )
            return ws

        try:
            ws = anyio.run(scenario)
        finally:
            daemon.FJ = None
            daemon.FJ_INSTANCES = {}
            self._reset(daemon)

        reply = ws.sent[-1]
        assert reply["type"] == daemon.wsproto.ERROR
        assert "unknown Forgejo instance 'ghost'" in reply["error"]
        assert "external" in reply["error"]
        assert not daemon.TRACKERS  # no tracker created for an unknown instance

    def test_list_carries_instance_alias(self, daemon, tmp_path, monkeypatch):
        monkeypatch.setenv("NOTIFICATIONS_DATA_DIR", str(tmp_path))
        self._reset(daemon)
        # Two forgejo trackers for the same owner/repo#n: one default, one named.
        default = pm.PRTracker(
            "o", "r", 1, None, provider="forgejo", base_url="https://fj/api/v1"
        )
        named = pm.PRTracker(
            "o",
            "r",
            1,
            None,
            provider="forgejo",
            base_url="https://ext/api/v1",
            instance="external",
        )
        for t in (default, named):
            t.snapshot = {"timeline": {}, "state": "open"}
            t.subscribers.add("sidA")
            t.acked["sidA"] = set()
            daemon.TRACKERS[t.storage_key] = t

        async def scenario():
            ws = _FakeWS()
            conn = daemon.Connection(ws)
            conn.session_id = "sidA"
            await daemon._handle_list_pr_subscriptions(
                ws, conn, {"session_id": "sidA", "req_id": 9}, provider="forgejo"
            )
            return ws

        try:
            ws = anyio.run(scenario)
        finally:
            self._reset(daemon)

        items = ws.sent[-1]["items"]
        # Default items are v1-shaped (no "instance" key); named items carry the alias.
        default_item = next(it for it in items if it.get("instance", "") == "")
        named_item = next(it for it in items if it.get("instance") == "external")
        assert default_item["pr"] == "o/r#1" and "instance" not in default_item
        assert named_item["pr"] == "o/r#1" and named_item["instance"] == "external"


# --------------------------------------------------------------------------- #
# auth-error message names the right per-instance token env var
# --------------------------------------------------------------------------- #


class _AuthFailClient:
    def __init__(self, base_url):
        self.base_url = base_url
        self.configured = True

    def should_throttle(self, threshold=50):
        return None

    async def fetch_pr(self, owner, repo, number):
        import pr_errors

        raise pr_errors.PRAuthError("unauthorized (401)")


class TestAuthErrorNaming:
    def _run_auth(self, daemon, tmp_path, monkeypatch, *, instance):
        monkeypatch.setenv("NOTIFICATIONS_DATA_DIR", str(tmp_path))
        monkeypatch.setenv("NOTIFICATIONS_PR_POLL_SECONDS", "0.2")
        daemon.TRACKERS.clear()
        client = _AuthFailClient("https://x/api/v1")
        t = pm.PRTracker(
            "o",
            "r",
            1,
            client,
            provider="forgejo",
            base_url="https://x/api/v1",
            instance=instance,
        )
        t.snapshot = {"timeline": {}, "state": "open", "merged": False}
        t.subscribers.add("sidA")
        t.acked["sidA"] = set()

        async def scenario():
            import asyncio

            conn = daemon.Connection(_FakeWS())
            conn.session_id = "sidA"
            daemon.CONNECTIONS["sidA"] = conn
            loop_task = asyncio.ensure_future(daemon._tracker_loop(t))
            for _ in range(200):
                if t.auth_notified:
                    break
                await asyncio.sleep(0.01)
            loop_task.cancel()
            try:
                await loop_task
            except asyncio.CancelledError:
                pass

        anyio.run(scenario)
        daemon.CONNECTIONS.clear()
        daemon.TRACKERS.clear()
        # The emitted auth event is on the tracker.
        return next(e for e in t.events if e["type"] == "pr_auth_error")

    def test_default_instance_names_forgejo_token(self, daemon, tmp_path, monkeypatch):
        event = self._run_auth(daemon, tmp_path, monkeypatch, instance="")
        assert "FORGEJO_TOKEN" in event["content"]
        assert "FORGEJO_EXTERNAL_TOKEN" not in event["content"]

    def test_named_instance_names_aliased_token(self, daemon, tmp_path, monkeypatch):
        event = self._run_auth(daemon, tmp_path, monkeypatch, instance="external")
        assert "FORGEJO_EXTERNAL_TOKEN" in event["content"]
        assert "external" in event["content"]  # alias named in the label


# --------------------------------------------------------------------------- #
# relay version-skew echo-back cleanup
# --------------------------------------------------------------------------- #


class TestSkewCleanup:
    """The relay's belt: sent-non-default + no-echo == failure.

    We drive subscribe_forgejo_pr with a stubbed _forgejo_daemon_request that models an
    OLD daemon (no `instance` echo) and a session-state stub, then assert the tool treats
    it as failure and either unsubscribes the stray sub (this-session-created) or warns
    without destroying (mirrored-repo / pre-existing default)."""

    def _install_stubs(self, relay, monkeypatch, reply):
        monkeypatch.setattr(
            relay.session_state, "effective_session_id", lambda: ("sidA", "hook")
        )
        calls = []

        async def fake_request(payload):
            calls.append(payload)
            return dict(reply)

        monkeypatch.setattr(relay, "_forgejo_daemon_request", fake_request)
        return calls

    def test_old_daemon_no_echo_this_session_created_unsubscribes_stray(
        self, relay, monkeypatch
    ):
        relay._SESSION_CREATED_SUBS.clear()
        # OLD daemon: subscribes on its default, replies subscribed but WITHOUT an instance.
        calls = self._install_stubs(
            relay,
            monkeypatch,
            {"type": relay.wsproto.SUBSCRIBED, "pr": "o/r#1", "summary": "open"},
        )
        out = anyio.run(lambda: relay.subscribe_forgejo_pr("external:o/r#1"))
        assert "predates multi-instance" in out
        assert "has been undone" in out  # cleaned up the stray default sub
        # the follow-up request is the UNSUBSCRIBE the tool issued to undo it
        assert any(c["type"] == relay.wsproto.UNSUBSCRIBE_FORGEJO_PR for c in calls)
        assert (("external", "o/r#1")) not in relay._SESSION_CREATED_SUBS

    def test_old_daemon_no_echo_not_created_warns_without_destroy(
        self, relay, monkeypatch
    ):
        relay._SESSION_CREATED_SUBS.clear()
        # Simulate a pre-existing default sub the relay did NOT create this session by
        # pre-seeding the reply but making the tool believe it created it, then removing
        # the created-marker before the reply is processed. Simplest: model the
        # mirrored-repo case by clearing the created-set right before the skew check.
        calls = []

        monkeypatch.setattr(
            relay.session_state, "effective_session_id", lambda: ("sidA", "hook")
        )

        async def fake_request(payload):
            calls.append(payload)
            # Between the tool recording (instance,ref) and the reply, drop the marker so
            # the skew path sees "I did NOT create this" (the mirrored-repo case).
            relay._SESSION_CREATED_SUBS.discard(("external", "o/r#1"))
            return {"type": relay.wsproto.SUBSCRIBED, "pr": "o/r#1", "summary": "open"}

        monkeypatch.setattr(relay, "_forgejo_daemon_request", fake_request)
        out = anyio.run(lambda: relay.subscribe_forgejo_pr("external:o/r#1"))
        assert "predates multi-instance" in out
        assert "may have been created" in out  # warn-without-destroy
        assert "check list_forgejo_pr_subscriptions" in out
        # It must NOT have issued an unsubscribe (would destroy a legit default sub).
        assert all(c["type"] != relay.wsproto.UNSUBSCRIBE_FORGEJO_PR for c in calls)

    def test_new_daemon_echoes_instance_success(self, relay, monkeypatch):
        relay._SESSION_CREATED_SUBS.clear()
        self._install_stubs(
            relay,
            monkeypatch,
            {
                "type": relay.wsproto.SUBSCRIBED,
                "pr": "o/r#1",
                "instance": "external",
                "summary": "open, checks 0 pass",
            },
        )
        out = anyio.run(lambda: relay.subscribe_forgejo_pr("external:o/r#1"))
        assert "Subscribed to external:o/r#1 (Forgejo: external)" in out
        assert "predates" not in out

    def test_default_subscribe_renders_exactly_v1(self, relay, monkeypatch):
        relay._SESSION_CREATED_SUBS.clear()
        self._install_stubs(
            relay,
            monkeypatch,
            {
                "type": relay.wsproto.SUBSCRIBED,
                "pr": "o/r#1",
                "instance": "",
                "summary": "open",
            },
        )
        out = anyio.run(lambda: relay.subscribe_forgejo_pr("o/r#1"))
        # v1 wording: "Subscribed to o/r#1 (Forgejo). Current status: ..."
        assert "Subscribed to o/r#1 (Forgejo)." in out
        assert "Forgejo:" not in out  # default carries no alias tag

    def test_default_subscribe_old_daemon_no_skew(self, relay, monkeypatch):
        # A DEFAULT subscribe against an old daemon (no echo) is NOT a skew — we sent no
        # instance, so the missing echo is expected and it renders as a plain success.
        relay._SESSION_CREATED_SUBS.clear()
        self._install_stubs(
            relay,
            monkeypatch,
            {"type": relay.wsproto.SUBSCRIBED, "pr": "o/r#1", "summary": "open"},
        )
        out = anyio.run(lambda: relay.subscribe_forgejo_pr("o/r#1"))
        assert "Subscribed to o/r#1 (Forgejo)." in out
        assert "predates" not in out


# --------------------------------------------------------------------------- #
# relay list rendering with alias tags
# --------------------------------------------------------------------------- #


class TestListRendering:
    def test_named_and_default_list_lines(self, relay, monkeypatch):
        monkeypatch.setattr(
            relay.session_state, "effective_session_id", lambda: ("sidA", "hook")
        )

        async def fake_request(payload):
            return {
                "type": relay.wsproto.SUBSCRIPTIONS_RESULT,
                "items": [
                    {"pr": "o/r#1", "instance": "", "merged": False, "pending": 0},
                    {
                        "pr": "o/r#2",
                        "instance": "external",
                        "merged": False,
                        "pending": 3,
                    },
                ],
            }

        monkeypatch.setattr(relay, "_forgejo_daemon_request", fake_request)
        out = anyio.run(relay.list_forgejo_pr_subscriptions)
        assert "  o/r#1" in out  # default: bare
        assert "external:o/r#2" in out  # named: alias-prefixed
        assert "pending=3" in out
