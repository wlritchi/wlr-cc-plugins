# vim: filetype=python
"""Unit tests for PR monitoring: polling schedule, GitHub error classification,
GraphQL snapshot + diff, the timeline-driven facet diff rules, identity-addressed
ids, and the split on-disk storage."""

import asyncio
import importlib.util
import json
import random
import time
from datetime import datetime, timezone
from pathlib import Path

import anyio
import httpx
import pytest

import github_client as gc
import pr_monitor as pm
import pr_schedule as ps

UTC = timezone.utc


def _utc(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=UTC)


def gql(**override) -> dict:
    """A GraphQL pullRequest node -> snapshot, with sensible defaults."""
    pr = {
        "title": "T",
        "url": "https://gh/o/r/1",
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
    pr.update(override)
    return pm.snapshot_from_graphql(pr)


def _types(old, new):
    return [e["type"] for e in pm.diff(old, new, "o/r#1")]


def _only(old, new, kind):
    events = [e for e in pm.diff(old, new, "o/r#1") if e["type"] == kind]
    assert events, (kind, _types(old, new))
    return events


# --------------------------------------------------------------------------- #
# schedule
# --------------------------------------------------------------------------- #


class TestSchedule:
    def test_business_hours_dst_aware(self):
        assert ps.in_business_hours(_utc(2026, 6, 25, 14))  # Thu 10:00 EDT
        assert not ps.in_business_hours(_utc(2026, 6, 25, 6))  # Thu 02:00 EDT
        assert not ps.in_business_hours(_utc(2026, 6, 27, 14))  # Saturday
        assert not ps.in_business_hours(_utc(2026, 6, 26, 3, 30))  # Thu 23:30 EDT
        assert ps.in_business_hours(_utc(2026, 1, 15, 15))  # Thu 10:00 EST (winter)

    def test_backoff_doubling(self):
        assert ps.base_interval_seconds(0) == 300
        assert ps.base_interval_seconds(1) == 300
        assert ps.base_interval_seconds(2) == 600
        assert ps.base_interval_seconds(4) == 1200
        assert ps.base_interval_seconds(100) == 8 * 3600

    def test_business_hours_cap(self):
        rng = random.Random(1)
        now = _utc(2026, 6, 25, 14)  # in business hours
        for _ in range(200):
            gap = (ps.compute_next_poll(now, 100, rng) - now).total_seconds()
            assert gap <= 3600 * 1.15 + 1

    def test_pre_open_pull_in(self):
        rng = random.Random(2)
        now = _utc(2026, 6, 25, 11, 30)  # Thu 07:30 EDT; opens 12:00 UTC
        opens = _utc(2026, 6, 25, 12)
        for _ in range(200):
            nxt = ps.compute_next_poll(now, 100, rng)
            assert (nxt - opens).total_seconds() <= 3600 * 1.15 + 1
            assert nxt > now

    def test_weekend_full_backoff(self):
        rng = random.Random(3)
        now = _utc(2026, 6, 27, 6)  # Sat 02:00 EDT
        gaps = [
            (ps.compute_next_poll(now, 100, rng) - now).total_seconds()
            for _ in range(200)
        ]
        assert min(gaps) >= 8 * 3600 * 0.85 - 1
        assert max(gaps) <= 8 * 3600 * 1.15 + 1


# --------------------------------------------------------------------------- #
# github client error classification
# --------------------------------------------------------------------------- #


class TestErrorClassification:
    def _resp(self, code, headers=None):
        return httpx.Response(code, headers=headers or {}, json={})

    def test_http_status_classification(self):
        client = gc.GitHubClient(token="x")
        future = str(int(time.time()) + 3600)
        with pytest.raises(gc.GitHubAuthError):
            client._classify_http(self._resp(401))
        with pytest.raises(gc.GitHubAuthError):
            client._classify_http(self._resp(403, {"X-RateLimit-Remaining": "7"}))
        with pytest.raises(gc.GitHubRateLimited):
            client._classify_http(
                self._resp(
                    403, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": future}
                )
            )
        with pytest.raises(gc.GitHubRateLimited):
            client._classify_http(self._resp(429, {"Retry-After": "30"}))
        with pytest.raises(gc.GitHubNotFound):
            client._classify_http(self._resp(404))
        with pytest.raises(gc.GitHubTransient):
            client._classify_http(self._resp(502))
        client._classify_http(self._resp(200))  # no raise

    def test_graphql_error_classification(self):
        with pytest.raises(gc.GitHubNotFound):
            gc.GitHubClient._classify_graphql({"errors": [{"type": "NOT_FOUND"}]})
        with pytest.raises(gc.GitHubAuthError):
            gc.GitHubClient._classify_graphql({"errors": [{"type": "FORBIDDEN"}]})
        with pytest.raises(gc.GitHubRateLimited):
            gc.GitHubClient._classify_graphql({"errors": [{"type": "RATE_LIMITED"}]})
        with pytest.raises(gc.GitHubTransient):
            gc.GitHubClient._classify_graphql({"errors": [{"message": "boom"}]})
        gc.GitHubClient._classify_graphql({"data": {}})  # no raise

    def test_rate_limit_throttle(self):
        client = gc.GitHubClient(token="x")
        future = str(int(time.time()) + 3600)
        client._update_rate_limit(
            httpx.Headers({"X-RateLimit-Remaining": "10", "X-RateLimit-Reset": future})
        )
        assert client.should_throttle(threshold=50) == float(future)
        client._update_rate_limit(
            httpx.Headers(
                {"X-RateLimit-Remaining": "4000", "X-RateLimit-Reset": future}
            )
        )
        assert client.should_throttle(threshold=50) is None


class TestFetchRetry:
    """The in-poll retry wrapper (gc._retry_transient) in isolation from httpx: a
    fake fetch coroutine we control and an injected no-op sleep keep it instant."""

    def test_retries_transient_then_succeeds(self):
        calls = {"n": 0}
        sleeps: list[float] = []

        async def fetch():
            calls["n"] += 1
            if calls["n"] < 3:
                raise gc.GitHubTransient("blip")
            return {"ok": calls["n"]}

        async def fake_sleep(delay):
            sleeps.append(delay)

        async def scenario():
            return await gc._retry_transient(fetch, sleep=fake_sleep)

        assert anyio.run(scenario) == {"ok": 3}
        assert calls["n"] == 3  # two transient failures, then success
        assert sleeps == [1.0, 2.0]  # exponential backoff between attempts

    def test_persistent_transient_exhausts_and_raises(self):
        calls = {"n": 0}

        async def fetch():
            calls["n"] += 1
            raise gc.GitHubTransient(f"blip {calls['n']}")

        async def fake_sleep(delay):
            pass

        async def scenario():
            return await gc._retry_transient(fetch, sleep=fake_sleep)

        with pytest.raises(gc.GitHubTransient):
            anyio.run(scenario)
        assert calls["n"] == gc._FETCH_ATTEMPTS  # gave up after the configured attempts

    def test_non_transient_is_not_retried(self):
        calls = {"n": 0}

        async def fetch():
            calls["n"] += 1
            raise gc.GitHubNotFound("gone")

        async def fake_sleep(delay):
            pass

        async def scenario():
            return await gc._retry_transient(fetch, sleep=fake_sleep)

        with pytest.raises(gc.GitHubNotFound):
            anyio.run(scenario)
        assert calls["n"] == 1  # auth/not-found/rate-limited propagate immediately


# --------------------------------------------------------------------------- #
# snapshot + diff
# --------------------------------------------------------------------------- #


class TestSnapshotAndDiff:
    def test_snapshot_shape_and_baseline(self):
        snap = gql(labels={"nodes": [{"name": "bug"}]})
        assert snap["state"] == "open" and snap["mergeable_state"] == "clean"
        assert snap["head_sha"] == "a" * 40 and snap["labels"] == ["bug"]
        assert pm.diff(None, snap, "o/r#1") == []  # baseline never replays

    def test_review_changes_requested_is_high(self):
        new = gql(
            reviews={
                "nodes": [
                    {
                        "id": "R1",
                        "state": "CHANGES_REQUESTED",
                        "author": {"login": "alice"},
                        "body": "fix the null check",
                        "url": "u",
                    }
                ]
            }
        )
        event = _only(gql(), new, "pr_review")[0]
        assert event["meta"]["severity"] == "high"
        assert "alice" in event["content"] and "null check" in event["content"]

    def test_inline_comment_short_includes_code(self):
        hunk = "@@ -10,3 +10,3 @@\n     a = 1\n-    b = 2\n+    b = 3"
        new = gql(
            reviewThreads={
                "nodes": [
                    {
                        "id": "T1",
                        "isResolved": False,
                        "path": "x.py",
                        "line": 12,
                        "startLine": None,
                        "comments": {
                            "nodes": [
                                {
                                    "id": "C1",
                                    "author": {"login": "bob"},
                                    "body": "why 3?",
                                    "url": "u",
                                    "diffHunk": hunk,
                                    "path": "x.py",
                                    "line": 12,
                                    "startLine": None,
                                    "originalLine": 12,
                                    "originalStartLine": None,
                                }
                            ]
                        },
                    }
                ]
            }
        )
        event = _only(gql(), new, "pr_inline_comment")[0]
        assert "x.py:12" in event["content"] and "b = 3" in event["content"]

    def test_inline_comment_long_gives_line_range(self):
        new = gql(
            reviewThreads={
                "nodes": [
                    {
                        "id": "T2",
                        "isResolved": False,
                        "path": "x.py",
                        "line": 80,
                        "startLine": 20,
                        "comments": {
                            "nodes": [
                                {
                                    "id": "C2",
                                    "author": {"login": "bob"},
                                    "body": "big",
                                    "url": "u",
                                    "diffHunk": "@@\n x",
                                    "path": "x.py",
                                    "line": 80,
                                    "startLine": 20,
                                    "originalLine": 80,
                                    "originalStartLine": 20,
                                }
                            ]
                        },
                    }
                ]
            }
        )
        event = _only(gql(), new, "pr_inline_comment")[0]
        assert (
            "x.py:20-80" in event["content"] and "spans lines 20–80" in event["content"]
        )

    def test_failed_check_includes_summary(self):
        new = gql(
            commits={
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
                                            "title": "2 failing",
                                            "summary": "test_foo test_bar",
                                        }
                                    ]
                                },
                            },
                        }
                    }
                ]
            }
        )
        event = _only(gql(), new, "pr_check")[0]
        assert event["meta"]["severity"] == "high" and "test_foo" in event["content"]
        assert event["meta"]["url"] == "https://gh/ch/1"

    def test_merge_conflict_high(self):
        event = _only(gql(), gql(mergeable="CONFLICTING"), "pr_conflict")[0]
        assert event["meta"]["severity"] == "high"

    def test_conflict_resolved_on_dirty_to_clean(self):
        dirty = gql(mergeable="CONFLICTING")
        clean = gql(mergeable="MERGEABLE")
        # dirty -> clean fires the (info) inverse of pr_conflict ...
        event = _only(dirty, clean, "pr_conflict_resolved")[0]
        assert event["meta"]["severity"] == "info"
        assert "resolved" in event["content"]
        # ... while clean -> clean and dirty -> dirty stay silent. (Like pr_conflict,
        # a re-resolve on the same head can collapse — documented residual case.)
        assert "pr_conflict_resolved" not in _types(clean, clean)
        assert "pr_conflict_resolved" not in _types(dirty, dirty)

    def test_merged_is_terminal(self):
        merged = gql(
            state="MERGED",
            merged=True,
            mergedBy={"login": "carol"},
            reviews={
                "nodes": [
                    {
                        "id": "R1",
                        "state": "APPROVED",
                        "author": {"login": "x"},
                        "body": "",
                        "url": "u",
                    }
                ]
            },
        )
        events = pm.diff(gql(), merged, "o/r#1")
        assert (
            len(events) == 1
            and events[0]["type"] == "pr_merged"
            and "carol" in events[0]["content"]
        )

    def test_rest_snapshot_diff_parity(self):
        # snapshot_from_api still produces a diffable snapshot (transport-agnostic diff)
        def api(**kw):
            base = {
                "pr": {
                    "state": "open",
                    "merged": False,
                    "mergeable_state": "clean",
                    "head": {"sha": "a" * 40},
                    "title": "T",
                    "html_url": "u",
                },
                "reviews": [],
                "review_comments": [],
                "issue_comments": [],
                "check_runs": [],
                "status": {},
            }
            base.update(kw)
            return pm.snapshot_from_api(base)

        base = api()
        new = api(
            reviews=[
                {
                    "id": 5,
                    "state": "APPROVED",
                    "user": {"login": "a"},
                    "body": "lgtm",
                    "html_url": "u",
                }
            ]
        )
        assert [e["type"] for e in pm.diff(base, new, "o/r#1")] == ["pr_review"]


# --------------------------------------------------------------------------- #
# facets (Phase 2)
# --------------------------------------------------------------------------- #


def _tl(*nodes) -> dict:
    """A timelineItems connection from positional node dicts."""
    return {"nodes": list(nodes)}


def _labeled(node_id: str, name: str, *, added: bool = True) -> dict:
    return {
        "__typename": "LabeledEvent" if added else "UnlabeledEvent",
        "id": node_id,
        "label": {"name": name},
    }


def _review_req(node_id: str, login: str, *, requested: bool = True) -> dict:
    typename = "ReviewRequestedEvent" if requested else "ReviewRequestRemovedEvent"
    return {
        "__typename": typename,
        "id": node_id,
        "requestedReviewer": {"__typename": "User", "login": login},
    }


def _force_push(node_id: str, before: str, after: str) -> dict:
    return {
        "__typename": "HeadRefForcePushedEvent",
        "id": node_id,
        "beforeCommit": {"oid": before},
        "afterCommit": {"oid": after},
    }


class TestFacets:
    def test_labels_added_and_removed(self):
        base = gql()
        added = gql(timelineItems=_tl(_labeled("L1", "bug"), _labeled("L2", "urgent")))
        events = _only(base, added, "pr_label")
        assert len(events) == 2 and all("added" in e["content"] for e in events)
        # the timeline accumulates: removals are new nodes appended after the adds
        removed = gql(
            timelineItems=_tl(
                _labeled("L1", "bug"),
                _labeled("L2", "urgent"),
                _labeled("U1", "bug", added=False),
                _labeled("U2", "urgent", added=False),
            )
        )
        events = _only(added, removed, "pr_label")
        assert len(events) == 2 and all("removed" in e["content"] for e in events)

    def test_label_re_add_is_three_distinct_events(self):
        """Regression: a label added -> removed -> added renders the 1st and 3rd
        identically, but distinct timeline node ids keep all three as separate,
        non-deduped events (the bug the identity scheme fixes)."""
        base = gql()
        final = gql(
            timelineItems=_tl(
                _labeled("L1", "bug"),
                _labeled("U1", "bug", added=False),
                _labeled("L2", "bug"),
            )
        )
        events = _only(base, final, "pr_label")
        assert len(events) == 3
        assert len({e["id"] for e in events}) == 3  # third add not collapsed onto first
        assert events[0]["content"] == events[2]["content"]  # identity != content
        # and record() keeps all three rather than deduping the repeat away
        added = pm.PRTracker("o", "r", 1, None).record(events)
        assert len(added) == 3

    def test_reviewers_requested_and_removed(self):
        base = gql()
        rr = gql(timelineItems=_tl(_review_req("RR1", "alice")))
        assert (
            "requested from alice"
            in _only(base, rr, "pr_review_request")[0]["content"].lower()
        )
        rrr = gql(
            timelineItems=_tl(
                _review_req("RR1", "alice"),
                _review_req("RRR1", "alice", requested=False),
            )
        )
        assert "removed" in _only(rr, rrr, "pr_review_request")[0]["content"].lower()

    def test_draft_toggle_both_ways(self):
        base = gql()
        drafted = gql(
            timelineItems=_tl(
                {"__typename": "ConvertToDraftEvent", "id": "D1", "actor": None}
            )
        )
        assert "draft" in _only(base, drafted, "pr_draft")[0]["content"]
        readied = gql(
            timelineItems=_tl(
                {"__typename": "ConvertToDraftEvent", "id": "D1", "actor": None},
                {
                    "__typename": "ReadyForReviewEvent",
                    "id": "RDY1",
                    "actor": {"login": "alice"},
                },
            )
        )
        assert "ready for review" in _only(drafted, readied, "pr_draft")[0]["content"]

    def test_thread_resolved_and_reopened(self):
        def thread(resolved):
            return gql(
                reviewThreads={
                    "nodes": [
                        {
                            "id": "T1",
                            "isResolved": resolved,
                            "path": "x.py",
                            "line": 12,
                            "startLine": None,
                            "comments": {"nodes": []},
                        }
                    ]
                }
            )

        resolved = _only(thread(False), thread(True), "pr_thread")[0]
        assert "resolved" in resolved["content"] and "x.py:12" in resolved["content"]
        assert (
            "reopened" in _only(thread(True), thread(False), "pr_thread")[0]["content"]
        )

    def test_all_green_recovery_and_no_false_positive(self):
        def checks(conclusion):
            return gql(
                commits={
                    "nodes": [
                        {
                            "commit": {
                                "oid": "a" * 40,
                                "statusCheckRollup": {
                                    "state": conclusion,
                                    "contexts": {
                                        "nodes": [
                                            {
                                                "__typename": "CheckRun",
                                                "id": "C1",
                                                "name": "build",
                                                "status": "COMPLETED",
                                                "conclusion": conclusion,
                                                "detailsUrl": "u",
                                            }
                                        ]
                                    },
                                },
                            }
                        }
                    ]
                }
            )

        failing, green = checks("FAILURE"), checks("SUCCESS")
        assert (
            "all checks are now passing"
            in _only(failing, green, "pr_checks_green")[0]["content"].lower()
        )
        assert "pr_checks_green" not in _types(green, green)

    def test_force_push_vs_plain_push(self):
        base = gql()
        forced = gql(
            headRefOid="b" * 40,
            timelineItems=_tl(_force_push("FP1", "a" * 40, "b" * 40)),
        )
        forced_types = _types(base, forced)
        assert "force-pushed" in _only(base, forced, "pr_force_push")[0]["content"]
        assert (
            "pr_commits" not in forced_types
        )  # the force-push subsumes the head change
        pushed = gql(headRefOid="b" * 40)
        assert "New commits pushed" in _only(base, pushed, "pr_commits")[0]["content"]
        assert "pr_force_push" not in _types(base, pushed)


# --------------------------------------------------------------------------- #
# identity-addressed ids + storage (Phase 3)
# --------------------------------------------------------------------------- #


class TestContentIdsAndStorage:
    def test_identity_addressed_ids(self):
        # id tracks the explicit identity, NOT the rendered content:
        a = pm._event(
            "pr_review", "info", "alice approved.\nu", "o/r#1", "u", "review:R1"
        )
        b = pm._event(
            "pr_review", "info", "alice approved.\nu", "o/r#1", "u", "review:R1"
        )
        # different content, same identity -> same id
        c = pm._event(
            "pr_review", "info", "bob approved.\nu", "o/r#1", "u", "review:R1"
        )
        # same content, different identity -> different id
        d = pm._event(
            "pr_review", "info", "alice approved.\nu", "o/r#1", "u", "review:R2"
        )
        assert a["id"] == b["id"] == c["id"]
        assert a["id"] != d["id"] and len(a["id"]) == 16

    def test_record_dedups_by_id(self):
        tracker = pm.PRTracker("o", "r", 1, None)
        a = pm._event("pr_review", "info", "same", "o/r#1", "u", "review:R1")
        b = pm._event("pr_review", "info", "same", "o/r#1", "u", "review:R1")
        c = pm._event("pr_review", "info", "other", "o/r#1", "u", "review:R2")
        added = tracker.record([a, b, c])
        assert [e["id"] for e in added] == [a["id"], c["id"]]
        assert len(tracker.events) == 2

    def test_split_storage_roundtrip(self, tmp_path, monkeypatch):
        monkeypatch.setenv("NOTIFICATIONS_DATA_DIR", str(tmp_path))
        tracker = pm.PRTracker("o", "r", 1, None)
        tracker.snapshot = {"timeline": {}, "labels": [], "state": "open"}
        tracker.consecutive_no_update = 3
        events = tracker.record(
            [
                pm._event("pr_review", "info", "one", "o/r#1", "u", "review:R1"),
                pm._event("pr_review", "info", "two", "o/r#1", "u", "review:R2"),
            ]
        )
        pm.save_state(tracker)
        pm.append_events(tracker, events)
        first_id = events[0]["id"]
        tracker.subscribers.add("sidA")
        tracker.acked["sidA"] = {first_id}
        pm.save_subscriber(tracker, "sidA")

        directory = pm._tracker_dir("o/r#1")
        assert (directory / "state.json").exists()
        assert len((directory / "events.jsonl").read_text().strip().splitlines()) == 2
        assert (directory / "sub-sidA.json").exists()

        loaded = {t.key: t for t in pm.load_trackers(None)}["o/r#1"]
        assert loaded.consecutive_no_update == 3
        assert loaded.event_ids == {events[0]["id"], events[1]["id"]}
        assert loaded.subscribers == {"sidA"} and loaded.acked["sidA"] == {first_id}
        assert [e["id"] for e in loaded.unacked_for("sidA")] == [events[1]["id"]]

    def test_event_log_is_append_only(self, tmp_path, monkeypatch):
        monkeypatch.setenv("NOTIFICATIONS_DATA_DIR", str(tmp_path))
        tracker = pm.PRTracker("o", "r", 1, None)
        log = pm._tracker_dir("o/r#1") / "events.jsonl"
        pm.append_events(
            tracker,
            tracker.record(
                [pm._event("pr_review", "info", "one", "o/r#1", "u", "review:R1")]
            ),
        )
        before = len(log.read_text().splitlines())
        pm.append_events(
            tracker,
            tracker.record(
                [pm._event("pr_review", "info", "two", "o/r#1", "u", "review:R2")]
            ),
        )
        assert len(log.read_text().splitlines()) == before + 1

    def test_next_poll_at_persists(self, tmp_path, monkeypatch):
        monkeypatch.setenv("NOTIFICATIONS_DATA_DIR", str(tmp_path))
        tracker = pm.PRTracker("o", "r", 1, None)
        tracker.snapshot = {"timeline": {}, "labels": [], "state": "open"}
        tracker.next_poll_at = 1_900_000_000.0  # far future, so a reload won't poll now
        pm.save_state(tracker)
        loaded = {t.key: t for t in pm.load_trackers(None)}["o/r#1"]
        assert loaded.next_poll_at == 1_900_000_000.0

    def test_delete_tracker_removes_dir(self, tmp_path, monkeypatch):
        monkeypatch.setenv("NOTIFICATIONS_DATA_DIR", str(tmp_path))
        tracker = pm.PRTracker("o", "r", 1, None)
        pm.save_state(tracker)
        directory = pm._tracker_dir("o/r#1")
        assert directory.exists()
        pm.delete_tracker("o/r#1")
        assert not directory.exists()


# --------------------------------------------------------------------------- #
# truncation accounting: per-subscriber "missed while away" count (Phase 4)
# --------------------------------------------------------------------------- #


class TestMissedOnCompaction:
    def _events(self, n: int) -> list[dict]:
        return [
            pm._event("pr_comment", "info", f"c{i}", "o/r#1", "u", f"comment:{i}")
            for i in range(n)
        ]

    def test_missed_counts_only_unacked_dropped(self, tmp_path, monkeypatch):
        monkeypatch.setenv("NOTIFICATIONS_DATA_DIR", str(tmp_path))
        monkeypatch.setattr(pm, "MAX_CACHED_EVENTS", 3)
        tracker = pm.PRTracker("o", "r", 1, None)
        events = self._events(6)  # cap 3 -> events 0,1,2 are the ones dropped
        added = tracker.record(events)
        # subA acked the two oldest (both dropped); subB acked everything.
        tracker.subscribers.update({"subA", "subB"})
        tracker.acked["subA"] = {events[0]["id"], events[1]["id"]}
        tracker.acked["subB"] = {e["id"] for e in events}
        pm.append_events(tracker, added)  # crosses the cap -> compaction
        assert len(tracker.events) == 3
        # subA missed only event 2 (the one dropped that it had not acked)
        assert tracker.missed["subA"] == 1
        # subB had acked all dropped events -> nothing genuinely missed
        assert tracker.missed["subB"] == 0
        # a subscriber that wasn't around for the drop has no missed count
        assert tracker.missed.get("subLater", 0) == 0

    def test_missed_accumulates_across_compactions(self, tmp_path, monkeypatch):
        monkeypatch.setenv("NOTIFICATIONS_DATA_DIR", str(tmp_path))
        monkeypatch.setattr(pm, "MAX_CACHED_EVENTS", 3)
        tracker = pm.PRTracker("o", "r", 1, None)
        tracker.subscribers.add("subA")
        tracker.acked["subA"] = set()  # never acks anything
        for batch in range(2):
            batch_events = [
                pm._event("pr_comment", "info", "x", "o/r#1", "u", f"b{batch}:{i}")
                for i in range(4)
            ]
            pm.append_events(tracker, tracker.record(batch_events))
        # 8 events total, cap 3 -> 5 dropped, none acked -> missed 5
        assert tracker.missed["subA"] == 5

    def test_subscriber_missed_persist_and_backward_compat(self, tmp_path, monkeypatch):
        monkeypatch.setenv("NOTIFICATIONS_DATA_DIR", str(tmp_path))
        tracker = pm.PRTracker("o", "r", 1, None)
        tracker.snapshot = {"timeline": {}, "labels": [], "state": "open"}
        pm.save_state(tracker)
        event = pm._event("pr_comment", "info", "c", "o/r#1", "u", "comment:1")
        pm.append_events(tracker, tracker.record([event]))
        tracker.subscribers.add("sidA")
        tracker.acked["sidA"] = {event["id"]}
        tracker.missed["sidA"] = 4
        pm.save_subscriber(tracker, "sidA")
        # a subscriber file written before `missed` existed must still load (as 0)
        (pm._tracker_dir("o/r#1") / "sub-sidB.json").write_text(
            json.dumps({"session_id": "sidB", "acked": [event["id"]]})
        )

        loaded = {t.key: t for t in pm.load_trackers(None)}["o/r#1"]
        assert loaded.missed["sidA"] == 4 and loaded.acked["sidA"] == {event["id"]}
        assert loaded.missed["sidB"] == 0  # absent in the legacy file -> default


# --------------------------------------------------------------------------- #
# daemon-level: truncation notice delivery + ack routing (Phase 4)
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def daemon():
    path = Path(__file__).resolve().parent.parent / "daemon" / "notifications-daemon.py"
    spec = importlib.util.spec_from_file_location("notifications_daemon", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _FakeWS:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, data: str) -> None:
        self.sent.append(json.loads(data))


def test_truncation_notice_delivered_once_then_ack_resets(
    daemon, tmp_path, monkeypatch
):
    monkeypatch.setenv("NOTIFICATIONS_DATA_DIR", str(tmp_path))
    key, sid = "o/r#1", "sidA"
    trunc_id = f"trunc:{key}:{sid}"
    tracker = pm.PRTracker("o", "r", 1, None)
    tracker.snapshot = {"timeline": {}, "labels": [], "state": "open"}
    tracker.subscribers.add(sid)
    tracker.acked[sid] = set()
    tracker.missed[sid] = 3
    pm.save_state(tracker)
    pm.save_subscriber(tracker, sid)  # on-disk subscriber file for the reload check
    monkeypatch.setitem(daemon.TRACKERS, key, tracker)

    async def scenario():
        ws = _FakeWS()
        conn = daemon.Connection(ws)
        conn.session_id = sid
        task = asyncio.create_task(daemon._dispatch_loop(conn))
        for _ in range(200):  # let one delivery pass run, then it blocks on its wake
            if ws.sent:
                break
            await asyncio.sleep(0.01)
        conn.wake.set()  # nudge a second pass: the inflight guard must dedup it
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return ws, conn

    ws, conn = anyio.run(scenario)
    trunc = [m for m in ws.sent if m["id"] == trunc_id]
    assert len(trunc) == 1  # delivered exactly once despite the second pass
    assert trunc[0]["meta"]["kind"] == "pr_truncated"
    assert trunc[0]["meta"]["severity"] == "high"
    assert "3 earlier update" in trunc[0]["content"]
    assert trunc_id in conn.inflight

    daemon._handle_ack(conn, {"id": trunc_id})  # ack clears counter + inflight
    assert tracker.missed[sid] == 0
    assert trunc_id not in conn.inflight
    # persisted reset survives a reload
    loaded = {t.key: t for t in pm.load_trackers(None)}[key]
    assert loaded.missed[sid] == 0
