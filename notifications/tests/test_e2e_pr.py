# vim: filetype=python
"""End-to-end test for GitHub PR monitoring: real daemon + relay + a fake GraphQL
endpoint (no network). Exercises subscribe, not-found, live updates delivered as
channel events, ack-driven high-water marks, no-replay for a new subscriber, and
merged auto-unsubscribe."""

import anyio
import pytest

import _harness as h

pytestmark = pytest.mark.slow

NUMBER = 7
KEY = f"octo/demo#{NUMBER}"


def base_pr() -> dict:
    return {
        "title": "Add feature",
        "url": f"https://gh/{KEY}",
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
