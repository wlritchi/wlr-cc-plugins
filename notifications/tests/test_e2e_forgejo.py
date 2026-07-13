# vim: filetype=python
"""End-to-end test for Forgejo/Gitea PR monitoring: real daemon + relay + a fake Gitea
REST endpoint (no network). The REST parallel to test_e2e_pr.py — kept SMALL (two flows):

  1. subscribe -> live mutation delivered as a channel event + acked (pending=0),
     then merge -> terminal pr_merged event + auto-unsubscribe.
  2. a PR that vanishes (pulls 404) -> terminal pr_gone + auto-unsubscribe.

The daemon points its ForgejoClient at the fake via FORGEJO_API_URL/FORGEJO_TOKEN
(daemon_env(forgejo_url=...)); the fast NOTIFICATIONS_PR_POLL_SECONDS knob keeps it off
real timers."""

import anyio
import pytest

import _harness as h

pytestmark = pytest.mark.slow

NUMBER = 5
KEY = f"owner/repo#{NUMBER}"


def test_forgejo_subscribe_update_merge(tmp_path):
    store, xdg = tmp_path / "store", tmp_path / "xdg"
    store.mkdir()
    xdg.mkdir()
    ws = h.free_port()

    with (
        h.FakeForgejo(NUMBER, h.forgejo_pr(NUMBER)) as fj,
        h.daemon_process(h.daemon_env(ws, store, forgejo_url=fj.api_url)),
    ):

        async def scenario():
            async with h.stdio_client(
                h.relay_params(h.push_relay_env(tmp_path, ws, store, xdg, "sid-A"))
            ) as (read_a, write_a):
                await h.mcp_handshake(read_a, write_a)

                text, _ = await h.mcp_call(
                    read_a, write_a, 2, "subscribe_forgejo_pr", {"pr": KEY}
                )
                assert f"Subscribed to {KEY}" in text and "Forgejo" in text
                assert "open" in text  # baseline summary

                # Mutate the fake: a changes-requested review + a failing commit status.
                fj.set_reviews(
                    [
                        {
                            "id": 1,
                            "state": "REQUEST_CHANGES",
                            "user": {"login": "alice"},
                            "body": "needs work on error handling",
                            "html_url": "https://fj/r/1",
                        }
                    ]
                )
                fj.set_statuses(
                    [
                        {
                            "context": "ci/build",
                            "status": "failure",
                            "target_url": "https://fj/ci/1",
                            "description": "build failed",
                        }
                    ]
                )
                event = await h.mcp_await_channel(read_a, timeout=25)
                assert event is not None
                content = event.params["content"]
                assert "alice" in content  # the changes-requested review
                assert "ci/build" in content  # the failing status
                assert event.params["meta"]["severity"] == "high"

                await anyio.sleep(3)  # let the push relay's auto-ack land
                text, _ = await h.mcp_call(
                    read_a, write_a, 3, "list_forgejo_pr_subscriptions"
                )
                assert KEY in text and "pending=0" in text

                # Merge -> terminal pr_merged event + auto-unsubscribe.
                fj.set_pr(state="closed", merged=True, merged_by={"login": "carol"})
                merged = await h.mcp_await_channel_with(read_a, "carol", 25)
                assert merged is not None
                assert "unsubscrib" in merged.params["content"]
                assert merged.params["meta"]["kind"] == "pr_merged"

                await anyio.sleep(3)  # let the terminal ack finalize the tracker
                text, _ = await h.mcp_call(
                    read_a, write_a, 4, "list_forgejo_pr_subscriptions"
                )
                assert "No active" in text

        anyio.run(scenario)


def test_forgejo_two_instances_isolated(tmp_path):
    """Multi-instance: one daemon serving a DEFAULT and a NAMED ("external") Forgejo
    instance. The same owner/repo#N lives on both (mirrored-repo shape). Subscribing to
    the bare ref and to external:<ref> yields two independent trackers: a mutation on the
    named instance is delivered tagged with its alias and does NOT leak onto the default
    subscription, and the list renders both with the right alias tagging."""
    store, xdg = tmp_path / "store", tmp_path / "xdg"
    store.mkdir()
    xdg.mkdir()
    ws = h.free_port()

    with (
        h.FakeForgejo(NUMBER, h.forgejo_pr(NUMBER)) as fj_default,
        h.FakeForgejo(NUMBER, h.forgejo_pr(NUMBER)) as fj_external,
        h.daemon_process(
            h.daemon_env(
                ws,
                store,
                forgejo_url=fj_default.api_url,
                forgejo_instances={"external": fj_external.api_url},
            )
        ),
    ):

        async def scenario():
            async with h.stdio_client(
                h.relay_params(h.push_relay_env(tmp_path, ws, store, xdg, "sid-A"))
            ) as (read_a, write_a):
                await h.mcp_handshake(read_a, write_a)

                # Subscribe to the SAME owner/repo#N on both instances.
                text, _ = await h.mcp_call(
                    read_a, write_a, 2, "subscribe_forgejo_pr", {"pr": KEY}
                )
                assert f"Subscribed to {KEY} (Forgejo)." in text  # default: v1 wording
                text, _ = await h.mcp_call(
                    read_a,
                    write_a,
                    3,
                    "subscribe_forgejo_pr",
                    {"pr": f"external:{KEY}"},
                )
                assert f"external:{KEY}" in text and "Forgejo: external" in text

                # list shows both, alias-tagged for the named one only.
                text, _ = await h.mcp_call(
                    read_a, write_a, 4, "list_forgejo_pr_subscriptions"
                )
                assert f"  {KEY}" in text  # default: bare
                assert f"external:{KEY}" in text  # named: alias-prefixed

                # Mutate ONLY the external instance: a changes-requested review.
                fj_external.set_reviews(
                    [
                        {
                            "id": 1,
                            "state": "REQUEST_CHANGES",
                            "user": {"login": "erin"},
                            "body": "external-only review",
                            "html_url": "https://ext/r/1",
                        }
                    ]
                )
                event = await h.mcp_await_channel_with(read_a, "erin", 25)
                assert event is not None
                assert "external-only review" in event.params["content"]

                # The default instance never saw that review: its subscription stays clean
                # (pending=0). Give the auto-ack a moment to settle the external event.
                await anyio.sleep(3)
                text, _ = await h.mcp_call(
                    read_a, write_a, 5, "list_forgejo_pr_subscriptions"
                )
                # Both still listed; the default one has no pending events from erin.
                assert f"  {KEY}" in text and f"external:{KEY}" in text
                # Locate the default line and confirm it reports pending=0 (no leak).
                default_line = next(
                    line
                    for line in text.splitlines()
                    if KEY in line and "external:" not in line
                )
                assert "pending=0" in default_line

        anyio.run(scenario)


def test_forgejo_not_found_emits_pr_gone(tmp_path):
    store, xdg = tmp_path / "store", tmp_path / "xdg"
    store.mkdir()
    xdg.mkdir()
    ws = h.free_port()

    with (
        h.FakeForgejo(NUMBER, h.forgejo_pr(NUMBER)) as fj,
        h.daemon_process(h.daemon_env(ws, store, forgejo_url=fj.api_url)),
    ):

        async def scenario():
            async with h.stdio_client(
                h.relay_params(h.push_relay_env(tmp_path, ws, store, xdg, "sid-A"))
            ) as (read_a, write_a):
                await h.mcp_handshake(read_a, write_a)
                text, _ = await h.mcp_call(
                    read_a, write_a, 2, "subscribe_forgejo_pr", {"pr": KEY}
                )
                assert f"Subscribed to {KEY}" in text

                # Point the fake away from this PR so /pulls/{NUMBER} now 404s, which the
                # client classifies as ForgejoNotFound -> terminal pr_gone.
                fj.number = NUMBER + 1
                event = await h.mcp_await_channel_with(
                    read_a, "could not be fetched", 25
                )
                assert event is not None
                assert "unsubscrib" in event.params["content"]
                assert event.params["meta"]["kind"] == "pr_gone"

                await anyio.sleep(3)  # the push relay auto-acks the terminal event
                text, _ = await h.mcp_call(
                    read_a, write_a, 3, "list_forgejo_pr_subscriptions"
                )
                assert "No active" in text

        anyio.run(scenario)
