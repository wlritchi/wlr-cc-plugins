#!/usr/bin/env -S uv run -qs
# vim: filetype=python
"""Run the notifications test suite.

    uv run -qs notifications/tests/run.py            # all tests
    uv run -qs notifications/tests/run.py -k diff    # filter
    uv run -qs notifications/tests/run.py -m "not slow"   # skip the e2e tests

The test deps live here (PEP 723) so there is no project to set up. The e2e tests
spawn the real daemon/relay via `uv run`, which resolves their own deps; nothing
hits the network at test time (a local fake GitHub GraphQL server is used).
"""

# /// script
# requires-python = ">=3.12"
# dependencies = ["pytest>=8", "mcp", "anyio", "websockets", "httpx", "tzdata"]
# ///

import sys
from pathlib import Path

import pytest

if __name__ == "__main__":
    here = Path(__file__).resolve().parent
    raise SystemExit(pytest.main([str(here), *sys.argv[1:]]))
