# vim: filetype=python
"""End-to-end test for GitHub PR monitoring: real daemon + relay + a fake GraphQL
endpoint (no network). Exercises subscribe, not-found, live updates delivered as
channel events, ack-driven high-water marks, no-replay for a new subscriber, and
merged auto-unsubscribe."""

import anyio
import pytest

import _harness as h
import pr_monitor

pytestmark = pytest.mark.slow

NUMBER = 7
KEY = f"octo/demo#{NUMBER}"


def base_pr(number: int = NUMBER) -> dict:
    key = f"octo/demo#{number}"
    return {
        "title": "Add feature",
        "url": f"https://gh/{key}",
        "state": "OPEN",
        "merged": False,
        "isDraft": False,
        "headRefOid": "a" * 40,
        "mergedBy": None,
        "mergeable": "MERGEABLE",
        "labels": {"nodes": []},
        "reviewRequests": {"nodes": []},
        "reviews": {"nodes": []},
        "reviewThreads": {"nodes": []},
        "comments": {"nodes": []},
        "commits": {
            "nodes": [{"commit": {"oid": "a" * 40, "statusCheckRollup": None}}]
        },
        "timelineItems": {"nodes": []},
    }


def test_pr_monitoring_full_chain(tmp_path):
    store, xdg = tmp_path / "store", tmp_path / "xdg"
    store.mkdir()
    xdg.mkdir()
    ws = h.free_port()

    with (
        h.FakeGitHub(NUMBER, base_pr()) as gh,
        h.daemon_process(h.daemon_env(ws, store, graphql_url=gh.graphql_url)),
    ):

        async def scenario():
            async with h.stdio_client(
                h.relay_params(h.push_relay_env(tmp_path, ws, store, xdg, "sid-A"))
            ) as (read_a, write_a):
                await h.mcp_handshake(read_a, write_a)

                text, _ = await h.mcp_call(
                    read_a, write_a, 2, "subscribe_github_pr", {"pr": KEY}
                )
                assert f"Subscribed to {KEY}" in text and "open" in text

                text, _ = await h.mcp_call(
                    read_a, write_a, 3, "subscribe_github_pr", {"pr": "octo/demo#999"}
                )
                assert "Could not subscribe" in text and "not found" in text

                # mutate GitHub -> failing check + changes-requested review + conflict
                gh.pr["mergeable"] = "CONFLICTING"
                gh.pr["reviews"] = {
                    "nodes": [
                        {
                            "id": "R1",
                            "state": "CHANGES_REQUESTED",
                            "author": {"login": "alice"},
                            "body": "needs work on error handling",
                            "url": "https://gh/r/11",
                        }
                    ]
                }
                gh.pr["commits"] = {
                    "nodes": [
                        {
                            "commit": {
                                "oid": "a" * 40,
                                "statusCheckRollup": {
                                    "state": "FAILURE",
                                    "contexts": {
                                        "nodes": [
                                            {
                                                "__typename": "CheckRun",
                                                "id": "C1",
                                                "name": "build",
                                                "status": "COMPLETED",
                                                "conclusion": "FAILURE",
                                                "detailsUrl": "https://gh/ch/1",
                                                "title": "3 failing",
                                                "summary": "test_a test_b",
                                            }
                                        ]
                                    },
                                },
                            }
                        }
                    ]
                }
                # check + review + conflict land in one poll -> ONE coalesced event
                event = await h.mcp_await_channel(read_a, timeout=25)
                assert event is not None
                content = event.params["content"]
                assert "test_a" in content  # the failing check
                assert "alice" in content  # the changes-requested review
                assert "merge conflicts" in content  # the conflict
                meta = event.params["meta"]
                assert meta["kind"] == "batch"  # coalesced, not a single kind
                assert meta["severity"] == "high"  # highest among the batch
                assert meta["count"] == "3"  # all three carried by one event

                await anyio.sleep(3)  # let acks land
                text, _ = await h.mcp_call(
                    read_a, write_a, 4, "list_github_pr_subscriptions"
                )
                assert KEY in text and "pending=0" in text

                # a second subscriber must NOT get the earlier events replayed
                async with h.stdio_client(
                    h.relay_params(h.push_relay_env(tmp_path, ws, store, xdg, "sid-B"))
                ) as (read_b, write_b):
                    await h.mcp_handshake(read_b, write_b)
                    text, _ = await h.mcp_call(
                        read_b, write_b, 2, "subscribe_github_pr", {"pr": KEY}
                    )
                    assert f"Subscribed to {KEY}" in text
                    gh.pr["comments"] = {
                        "nodes": [
                            {
                                "id": "IC1",
                                "author": {"login": "dave"},
                                "body": "LGTM after fixes",
                                "url": "https://gh/ic/99",
                            }
                        ]
                    }
                    # one coalesced event with only the new comment (no replay)
                    event_b = await h.mcp_await_channel(read_b, timeout=25)
                    assert event_b is not None
                    content_b = event_b.params["content"]
                    assert "dave" in content_b  # the new comment
                    assert "merge conflicts" not in content_b  # earlier conflict
                    assert "test_a" not in content_b  # earlier check
                    assert "alice" not in content_b  # earlier review

                # merge -> terminal event + auto-unsubscribe
                gh.pr.update(
                    {"state": "MERGED", "merged": True, "mergedBy": {"login": "carol"}}
                )
                # subscriber A also receives the earlier 'dave' comment, so read
                # past it to the merge (terminal) event + auto-unsubscribe.
                merged = await h.mcp_await_channel_with(read_a, "carol", 25)
                assert merged is not None
                assert (
                    "carol" in merged.params["content"]
                    and "unsubscrib" in merged.params["content"]
                )
                await anyio.sleep(3)
                text, _ = await h.mcp_call(
                    read_a, write_a, 5, "list_github_pr_subscriptions"
                )
                assert "No active" in text

        anyio.run(scenario)


def test_pr_warm_retention_then_reaped(tmp_path):
    store, xdg = tmp_path / "store", tmp_path / "xdg"
    store.mkdir()
    xdg.mkdir()
    ws = h.free_port()
    tracker_dir = store / "pr" / pr_monitor._safe(KEY)

    with (
        h.FakeGitHub(NUMBER, base_pr()) as gh,
        h.daemon_process(
            h.daemon_env(ws, store, graphql_url=gh.graphql_url, warm_ttl="3")
        ),
    ):

        async def scenario():
            async with h.stdio_client(
                h.relay_params(h.push_relay_env(tmp_path, ws, store, xdg, "sid-A"))
            ) as (read_a, write_a):
                await h.mcp_handshake(read_a, write_a)

                text, _ = await h.mcp_call(
                    read_a, write_a, 2, "subscribe_github_pr", {"pr": KEY}
                )
                assert f"Subscribed to {KEY}" in text
                assert (tracker_dir / "state.json").exists()

                text, _ = await h.mcp_call(
                    read_a, write_a, 3, "unsubscribe_github_pr", {"pr": KEY}
                )
                assert "Unsubscribed" in text
                # Warm-retained: the tracker dir survives the unsubscribe-to-zero.
                assert (tracker_dir / "state.json").exists()

                # Past the 3s TTL the reaper (interval ~ttl/2 ≈ 1.5s) deletes it.
                await anyio.sleep(7)
                assert not (tracker_dir / "state.json").exists()

        anyio.run(scenario)


# --------------------------------------------------------------------------- #
# mutation builders shared by the failure-injection / concurrency tests below
# --------------------------------------------------------------------------- #


def _failing_check() -> dict:
    """A commits connection whose head rollup carries one FAILURE CheckRun (summary
    'test_a test_b'), so the diff emits a high-severity pr_check mentioning 'test_a'."""
    return {
        "nodes": [
            {
                "commit": {
                    "oid": "a" * 40,
                    "statusCheckRollup": {
                        "state": "FAILURE",
                        "contexts": {
                            "nodes": [
                                {
                                    "__typename": "CheckRun",
                                    "id": "C1",
                                    "name": "build",
                                    "status": "COMPLETED",
                                    "conclusion": "FAILURE",
                                    "detailsUrl": "https://gh/ch/1",
                                    "title": "3 failing",
                                    "summary": "test_a test_b",
                                }
                            ]
                        },
                    },
                }
            }
        ]
    }


def _changes_requested_review() -> dict:
    return {
        "nodes": [
            {
                "id": "R1",
                "state": "CHANGES_REQUESTED",
                "author": {"login": "alice"},
                "body": "needs work on error handling",
                "url": "https://gh/r/11",
            }
        ]
    }


def _comment(comment_id: str, login: str, body: str) -> dict:
    return {
        "nodes": [
            {
                "id": comment_id,
                "author": {"login": login},
                "body": body,
                "url": f"https://gh/ic/{comment_id}",
            }
        ]
    }


def test_pr_auth_error_emits_one_time_event(tmp_path):
    """After a good subscribe, a 401 on every poll surfaces ONE pr_auth_error event."""
    store, xdg = tmp_path / "store", tmp_path / "xdg"
    store.mkdir()
    xdg.mkdir()
    ws = h.free_port()

    with (
        h.FakeGitHub(NUMBER, base_pr()) as gh,
        h.daemon_process(h.daemon_env(ws, store, graphql_url=gh.graphql_url)),
    ):

        async def scenario():
            async with h.stdio_client(
                h.relay_params(h.push_relay_env(tmp_path, ws, store, xdg, "sid-A"))
            ) as (read_a, write_a):
                await h.mcp_handshake(read_a, write_a)
                text, _ = await h.mcp_call(
                    read_a, write_a, 2, "subscribe_github_pr", {"pr": KEY}
                )
                assert f"Subscribed to {KEY}" in text

                # The initial poll already succeeded; now every poll 401s -> auth error.
                gh.set_fault(401)
                event = await h.mcp_await_channel_with(read_a, "GITHUB_TOKEN", 25)
                assert event is not None
                content = event.params["content"]
                assert "GitHub access" in content and "failed" in content
                meta = event.params["meta"]
                assert meta["kind"] == "pr_auth_error"
                assert meta["severity"] == "high"

        anyio.run(scenario)


def test_pr_not_found_emits_pr_gone_and_unsubscribes(tmp_path):
    """A PR that vanishes (polls return pullRequest: null) yields a terminal pr_gone
    event and auto-unsubscribes the session — no fault machinery needed."""
    store, xdg = tmp_path / "store", tmp_path / "xdg"
    store.mkdir()
    xdg.mkdir()
    ws = h.free_port()

    with (
        h.FakeGitHub(NUMBER, base_pr()) as gh,
        h.daemon_process(h.daemon_env(ws, store, graphql_url=gh.graphql_url)),
    ):

        async def scenario():
            async with h.stdio_client(
                h.relay_params(h.push_relay_env(tmp_path, ws, store, xdg, "sid-A"))
            ) as (read_a, write_a):
                await h.mcp_handshake(read_a, write_a)
                text, _ = await h.mcp_call(
                    read_a, write_a, 2, "subscribe_github_pr", {"pr": KEY}
                )
                assert f"Subscribed to {KEY}" in text

                # Point the fake away from this PR so later polls see pullRequest: null,
                # which the client classifies as GitHubNotFound.
                gh.number = NUMBER + 1
                event = await h.mcp_await_channel_with(
                    read_a, "could not be fetched", 25
                )
                assert event is not None
                assert "unsubscrib" in event.params["content"]
                assert event.params["meta"]["kind"] == "pr_gone"

                # The push relay auto-acks the terminal event, driving the daemon's
                # finalize-and-unsubscribe; give it a moment to land.
                await anyio.sleep(3)
                text, _ = await h.mcp_call(
                    read_a, write_a, 3, "list_github_pr_subscriptions"
                )
                assert "No active" in text

        anyio.run(scenario)


def test_pr_transient_blip_recovers(tmp_path):
    """Two 503s burn the in-poll retry budget; the poll recovers on the 3rd attempt,
    keeps polling, and a later mutation is still delivered (no terminal event)."""
    store, xdg = tmp_path / "store", tmp_path / "xdg"
    store.mkdir()
    xdg.mkdir()
    ws = h.free_port()

    with (
        h.FakeGitHub(NUMBER, base_pr()) as gh,
        h.daemon_process(h.daemon_env(ws, store, graphql_url=gh.graphql_url)),
    ):

        async def scenario():
            async with h.stdio_client(
                h.relay_params(h.push_relay_env(tmp_path, ws, store, xdg, "sid-A"))
            ) as (read_a, write_a):
                await h.mcp_handshake(read_a, write_a)
                text, _ = await h.mcp_call(
                    read_a, write_a, 2, "subscribe_github_pr", {"pr": KEY}
                )
                assert f"Subscribed to {KEY}" in text

                # _retry_transient does 3 attempts; the 2 faults clear before the 3rd,
                # which reads the now-mutated PR and emits the failing-check event.
                gh.set_fault(503, count=2)
                gh.pr["commits"] = _failing_check()

                # Generous timeout: in-poll backoff adds ~1s + ~2s before recovery.
                event, kinds = await h.mcp_await_channel_with_kinds(
                    read_a, "test_a", 40
                )
                assert event is not None  # recovered and delivered the change
                assert "pr_gone" not in kinds and "pr_auth_error" not in kinds

                # Still subscribed: no terminal event tore the tracker down.
                await anyio.sleep(2)
                text, _ = await h.mcp_call(
                    read_a, write_a, 3, "list_github_pr_subscriptions"
                )
                assert KEY in text

        anyio.run(scenario)


def test_pr_fanout_to_multiple_subscribers(tmp_path):
    """Two sessions subscribe the same PR: one mutation fans out to BOTH; one session
    unsubscribing leaves the tracker alive and still delivering to the other."""
    store, xdg = tmp_path / "store", tmp_path / "xdg"
    store.mkdir()
    xdg.mkdir()
    ws = h.free_port()

    with (
        h.FakeGitHub(NUMBER, base_pr()) as gh,
        h.daemon_process(h.daemon_env(ws, store, graphql_url=gh.graphql_url)),
    ):

        async def scenario():
            async with h.stdio_client(
                h.relay_params(h.push_relay_env(tmp_path, ws, store, xdg, "sid-A"))
            ) as (read_a, write_a):
                await h.mcp_handshake(read_a, write_a)
                text, _ = await h.mcp_call(
                    read_a, write_a, 2, "subscribe_github_pr", {"pr": KEY}
                )
                assert f"Subscribed to {KEY}" in text

                async with h.stdio_client(
                    h.relay_params(h.push_relay_env(tmp_path, ws, store, xdg, "sid-B"))
                ) as (read_b, write_b):
                    await h.mcp_handshake(read_b, write_b)
                    text, _ = await h.mcp_call(
                        read_b, write_b, 2, "subscribe_github_pr", {"pr": KEY}
                    )
                    assert f"Subscribed to {KEY}" in text

                    # One mutation (failing check + changes-requested review) -> BOTH
                    # subscribers receive a coalesced event carrying the change.
                    gh.pr["reviews"] = _changes_requested_review()
                    gh.pr["commits"] = _failing_check()
                    ev_a = await h.mcp_await_channel_with(read_a, "alice", 25)
                    ev_b = await h.mcp_await_channel_with(read_b, "alice", 25)
                    assert ev_a is not None and ev_b is not None
                    assert "test_a" in ev_a.params["content"]
                    assert "test_a" in ev_b.params["content"]

                    await anyio.sleep(2)  # let acks land before A leaves

                    # A unsubscribes; the tracker still has B, so it keeps polling.
                    text, _ = await h.mcp_call(
                        read_a, write_a, 3, "unsubscribe_github_pr", {"pr": KEY}
                    )
                    assert "Unsubscribed" in text

                    # A new mutation reaches B (proving the tracker stayed alive for B).
                    gh.pr["comments"] = _comment("IC9", "dave", "ship it")
                    ev_b2 = await h.mcp_await_channel_with(read_b, "dave", 25)
                    assert ev_b2 is not None
                    assert "dave" in ev_b2.params["content"]
                    text, _ = await h.mcp_call(
                        read_b, write_b, 3, "list_github_pr_subscriptions"
                    )
                    assert KEY in text

        anyio.run(scenario)


def test_pr_one_session_multiple_trackers(tmp_path):
    """One session subscribes to two different PRs served by the same fake (one GraphQL
    URL). Mutating each PR delivers an event tied to the right tracker, independently."""
    store, xdg = tmp_path / "store", tmp_path / "xdg"
    store.mkdir()
    xdg.mkdir()
    ws = h.free_port()
    number2 = NUMBER + 1
    key2 = f"octo/demo#{number2}"

    with (
        h.FakeGitHub(NUMBER, base_pr(), extra={number2: base_pr(number2)}) as gh,
        h.daemon_process(h.daemon_env(ws, store, graphql_url=gh.graphql_url)),
    ):

        async def scenario():
            async with h.stdio_client(
                h.relay_params(h.push_relay_env(tmp_path, ws, store, xdg, "sid-A"))
            ) as (read_a, write_a):
                await h.mcp_handshake(read_a, write_a)
                text, _ = await h.mcp_call(
                    read_a, write_a, 2, "subscribe_github_pr", {"pr": KEY}
                )
                assert f"Subscribed to {KEY}" in text
                text, _ = await h.mcp_call(
                    read_a, write_a, 3, "subscribe_github_pr", {"pr": key2}
                )
                assert f"Subscribed to {key2}" in text

                # Mutate PR #1 -> its tracker delivers a failing-check event for KEY.
                gh.pr["commits"] = _failing_check()
                ev1 = await h.mcp_await_channel_with(read_a, "test_a", 25)
                assert ev1 is not None
                assert KEY in ev1.params["content"]

                # Mutate PR #2 -> the OTHER tracker delivers a distinct event for key2.
                gh.extra[number2]["comments"] = _comment("IC2", "erin", "second PR")
                ev2 = await h.mcp_await_channel_with(read_a, "erin", 25)
                assert ev2 is not None
                assert key2 in ev2.params["content"]

                text, _ = await h.mcp_call(
                    read_a, write_a, 4, "list_github_pr_subscriptions"
                )
                assert KEY in text and key2 in text

        anyio.run(scenario)
