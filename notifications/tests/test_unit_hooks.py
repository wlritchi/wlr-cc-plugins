# vim: filetype=python
"""Unit tests for the plugin's hook scripts (the PostCompact compaction-audit hook)."""

import importlib.util
import json
from pathlib import Path


def _load_hook():
    path = Path(__file__).resolve().parent.parent / "hooks" / "compaction-audit.py"
    spec = importlib.util.spec_from_file_location("compaction_audit", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


hook = _load_hook()


def test_build_record_full_payload():
    payload = {
        "hook_event_name": "PostCompact",
        "session_id": "sid-1",
        "trigger": "auto",
        "transcript_path": "/somewhere/t.jsonl",  # present but not read (post-hook race)
    }
    assert hook.build_record(payload, now=1234.5) == {
        "event": "PostCompact",
        "ts": 1234.5,
        "session_id": "sid-1",
        "trigger": "auto",
    }


def test_build_record_matcher_fallback_and_defaults():
    # No hook_event_name -> defaults to PostCompact; "matcher" read when "trigger" absent.
    record = hook.build_record({"matcher": "manual", "session_id": "s"}, now=1.0)
    assert record["event"] == "PostCompact"
    assert record["trigger"] == "manual"
    assert record["session_id"] == "s"


def test_build_record_missing_trigger_is_none():
    record = hook.build_record({"session_id": "s"}, now=1.0)
    assert record["trigger"] is None  # neither trigger nor matcher present


def test_append_record_creates_dir_and_appends(tmp_path):
    audit = tmp_path / "nested" / "audit.jsonl"  # parent does not exist yet
    hook.append_record(audit, {"event": "PostCompact", "n": 1})
    hook.append_record(audit, {"event": "PostCompact", "n": 2})
    lines = audit.read_text().splitlines()
    assert [json.loads(line)["n"] for line in lines] == [1, 2]  # ordered append


def test_audit_path_env_override(monkeypatch, tmp_path):
    override = tmp_path / "custom-audit.jsonl"
    monkeypatch.setenv("NOTIFICATIONS_COMPACTION_AUDIT_FILE", str(override))
    assert hook._audit_path() == override


def test_audit_path_default_under_home(monkeypatch):
    monkeypatch.delenv("NOTIFICATIONS_COMPACTION_AUDIT_FILE", raising=False)
    assert hook._audit_path().name == "notifications-compaction-audit.jsonl"
    assert ".claude" in hook._audit_path().parts
