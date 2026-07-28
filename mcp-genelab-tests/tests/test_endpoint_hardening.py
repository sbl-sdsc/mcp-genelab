"""Tests for the public-endpoint hardening added to server.py.

These cover the security/operational controls introduced for the CloudFront →
AgentCore Gateway (CUSTOM_JWT) → AgentCore Runtime deployment:

  - Forbidden-procedure blocking in the `query` tool (LOAD CSV, apoc.load.*,
    apoc.export.*, dbms.*) — the read-only-but-dangerous operations that Bolt
    READ_ACCESS does NOT stop.
  - Row-capping / truncation notice on the general-purpose `query` tool.
  - Query timeout plumbing (tx.run receives a timeout, and a Cypher-timeout
    surfaces an actionable message).
  - set_output_directory input hygiene (length, NUL/newline, '..' segments).
  - Fail-fast credential policy in remote mode (_require_env).

All offline — no Neo4j, no network — using the same FakeDriver harness as the
rest of the suite.
"""
from __future__ import annotations

import asyncio
import importlib

import pytest

from conftest import call_tool_sync, text_from


# --- Forbidden-procedure blocking ------------------------------------------

@pytest.mark.parametrize("bad_query", [
    "LOAD CSV FROM 'http://evil/x.csv' AS row RETURN row",
    "CALL apoc.load.json('http://evil/x') YIELD value RETURN value",
    "CALL apoc.export.csv.all('/tmp/out.csv', {}) YIELD file RETURN file",
    "MATCH (n) CALL apoc.export.json.all('x.json',{}) RETURN n",
    "CALL dbms.components() YIELD name RETURN name",
    "CALL dbms.security.listUsers()",
    "CALL apoc.periodic.iterate('MATCH (n) RETURN n','RETURN 1',{})",
])
def test_query_tool_blocks_forbidden_procedures(bad_query, mcp_server):
    """Read-only-but-dangerous procedures (network I/O, export, DBMS admin)
    must be rejected before reaching Neo4j, because READ_ACCESS does not block
    them — they aren't graph writes."""
    text = call_tool_sync(mcp_server, "query", {"query": bad_query})
    assert "rejected" in text.lower()


@pytest.mark.parametrize("ok_query", [
    "MATCH (n:Assay) RETURN n LIMIT 5",
    "CALL apoc.meta.schema() YIELD value RETURN value",
    "CALL apoc.help('apoc') YIELD name RETURN name LIMIT 1",
])
def test_query_tool_allows_readonly_and_meta(ok_query, driver, mcp_server):
    """Legitimate read queries and read-only apoc.meta/help introspection
    (used by the schema tools) must NOT be blocked."""
    driver.set_route(lambda q, p: [{"value": "ok"}])
    text = call_tool_sync(mcp_server, "query", {"query": ok_query})
    assert "rejected" not in text.lower()


# --- Row capping / truncation ----------------------------------------------

def test_query_tool_truncates_large_results(driver, mcp_server, server_module):
    """The general-purpose query tool caps rows at MAX_QUERY_ROWS and appends a
    truncation notice so the model can narrow the query."""
    cap = server_module.MAX_QUERY_ROWS
    # Route returns more rows than the cap.
    driver.set_route(lambda q, p: [{"i": i} for i in range(cap + 500)])
    text = call_tool_sync(mcp_server, "query", {"query": "MATCH (n) RETURN n"})
    assert "truncated" in text.lower()
    # The count header should report the capped number, not the full set.
    assert f"total_rows: {cap}" in text


def test_query_tool_no_truncation_when_under_cap(driver, mcp_server):
    driver.set_route(lambda q, p: [{"i": i} for i in range(3)])
    text = call_tool_sync(mcp_server, "query", {"query": "MATCH (n) RETURN n"})
    assert "truncated" not in text.lower()
    assert "total_rows: 3" in text


# --- Query timeout plumbing -------------------------------------------------

def test_tx_run_receives_timeout(driver, mcp_server, server_module):
    """_read/_read_with_count must pass a timeout to tx.run so Neo4j cancels
    runaway queries. We capture the kwarg via a custom fake tx.run."""
    captured = {}

    # Monkeypatch the FakeDriver's session to record the timeout kwarg.
    orig_session = driver.session

    class _RecordingResult:
        def __init__(self, rows):
            self._rows = rows
        async def to_eager_result(self):
            class E:
                records = []
            return E()
        def __aiter__(self):
            self._it = iter(self._rows)
            return self
        async def __anext__(self):
            try:
                return next(self._it)
            except StopIteration:
                raise StopAsyncIteration

    class _RecordingTx:
        async def run(self, query, params=None, timeout=None, **kw):
            captured["timeout"] = timeout
            return _RecordingResult([])

    class _RecordingSession:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def execute_read(self, fn, *args, **kwargs):
            return await fn(_RecordingTx(), *args, **kwargs)

    driver.session = lambda **kw: _RecordingSession()
    try:
        call_tool_sync(mcp_server, "query", {"query": "MATCH (n) RETURN n"})
    finally:
        driver.session = orig_session

    assert captured.get("timeout") == server_module.QUERY_TIMEOUT_SECONDS


def test_query_tool_reports_cypher_timeout(driver, mcp_server):
    """A Neo4j-side timeout (raised as an exception containing 'timeout') must
    surface an actionable message, not a raw stack trace."""
    def _raise_timeout(q, p):
        raise RuntimeError("The transaction has been terminated. timeout expired")
    driver.set_route(_raise_timeout)
    text = call_tool_sync(mcp_server, "query", {"query": "MATCH (a)-[*]-(b) RETURN a"})
    assert "time limit" in text.lower() or "timeout" in text.lower()


def test_query_tool_error_does_not_echo_query(driver, mcp_server):
    """On a generic DB error the response must NOT echo the raw query back
    (info-leak hygiene for a public endpoint)."""
    secret_label = "SuperSecretInternalLabel12345"
    def _boom(q, p):
        raise RuntimeError("boom")
    driver.set_route(_boom)
    text = call_tool_sync(
        mcp_server, "query",
        {"query": f"MATCH (n:{secret_label}) RETURN n"},
    )
    assert secret_label not in text


# --- set_output_directory hygiene ------------------------------------------

@pytest.mark.parametrize("bad_path", [
    "/tmp/../../etc/passwd",
    "..\\..\\Windows\\System32",
    "/tmp/ok/../../../root",
    "/tmp/with\nnewline",
    "/tmp/with\x00nul",
    "x" * 5000,
])
def test_set_output_directory_rejects_bad_paths(bad_path, mcp_server):
    text = call_tool_sync(mcp_server, "set_output_directory", {"path": bad_path})
    assert "error" in text.lower()


@pytest.mark.parametrize("good_path", [
    "/Users/jane/Downloads",
    "/home/jane/out",
    "C:/Users/Jane/Downloads",
])
def test_set_output_directory_accepts_good_paths(good_path, mcp_server):
    text = call_tool_sync(mcp_server, "set_output_directory", {"path": good_path})
    assert "error" not in text.lower()
    assert "Output directory set to" in text


# --- Fail-fast credential policy -------------------------------------------

def test_require_env_raises_when_missing(server_module, monkeypatch):
    monkeypatch.delenv("NEO4J_URI", raising=False)
    with pytest.raises(SystemExit):
        server_module._require_env("NEO4J_URI")


def test_require_env_returns_when_present(server_module, monkeypatch):
    monkeypatch.setenv("NEO4J_URI", "bolt://x:7687")
    assert server_module._require_env("NEO4J_URI") == "bolt://x:7687"


# --- Forbidden/write helper unit tests -------------------------------------

@pytest.mark.parametrize("q,expected", [
    ("MATCH (n) RETURN n", False),
    ("CREATE (n)", True),
    ("match (n) set n.x = 1", True),
    ("LOAD CSV FROM 'x' AS r RETURN r", True),
    ("CALL apoc.load.json('x')", True),
    ("CALL apoc.meta.schema()", False),
    ("CALL dbms.components()", True),
])
def test_is_forbidden_query(server_module, q, expected):
    assert server_module._is_forbidden_query(q) is expected


# --- Scrub helper -----------------------------------------------------------

def test_scrub_for_log_collapses_and_bounds(server_module):
    s = server_module._scrub_for_log("line1\nline2\n" + "x" * 500, limit=50)
    assert "\n" not in s
    assert len(s) < 120  # bounded + suffix
    assert "chars)" in s
