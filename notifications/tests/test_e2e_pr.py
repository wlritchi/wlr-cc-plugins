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
                got = await h.mcp_collect_channels(
                    read_a, {"pr_check", "pr_review", "pr_conflict"}, 25
                )
                assert {"pr_check", "pr_review", "pr_conflict"} <= set(got)
                assert (
                    "test_a" in got["pr_check"]["content"]
                    and got["pr_check"]["meta"]["severity"] == "high"
                )
                assert "alice" in got["pr_review"]["content"]

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
                    got_b = await h.mcp_collect_channels(read_b, {"pr_comment"}, 25)
                    assert (
                        "pr_comment" in got_b
                        and "dave" in got_b["pr_comment"]["content"]
                    )
                    assert (
                        "pr_conflict" not in got_b and "pr_check" not in got_b
                    )  # no replay

                # merge -> terminal event + auto-unsubscribe
                gh.pr.update(
                    {"state": "MERGED", "merged": True, "mergedBy": {"login": "carol"}}
                )
                merged = await h.mcp_collect_channels(read_a, {"pr_merged"}, 25)
                assert (
                    "carol" in merged["pr_merged"]["content"]
                    and "unsubscrib" in merged["pr_merged"]["content"]
                )
                await anyio.sleep(3)
                text, _ = await h.mcp_call(
                    read_a, write_a, 5, "list_github_pr_subscriptions"
                )
                assert "No active" in text

        anyio.run(scenario)
