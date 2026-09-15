"""Per-session isolation, session lifecycle, ALB health routes and usage
metrics — the controls introduced for the SHARED-PROCESS deployment
(MCP client → CloudFront+WAF → ALB → ECS Fargate → Neo4j on EC2).

Under AgentCore each user session ran in its own microVM, so module globals
were effectively per-user. On Fargate one process serves everyone, so the
server keys all per-user state on an application-level session id that the
agent obtains from `create_session` and passes on every call. These tests
guard:

  - create_session mints unguessable, distinct ids; end_session discards.
  - Output directory and plot registry are invisible across sessions
    (tools AND the plot:// resource).
  - Unknown / expired / malformed / missing session ids yield actionable
    errors that tell the agent to call create_session (strict policy).
  - Idle-TTL expiry and the per-session / process-wide plot bounds.
  - Every tool except create_session exposes a `session_id` parameter.
  - `GET /healthz` and `GET /readyz` behave as ALB health checks need.
  - Tool calls are counted (usage log line + in-process counters) and the
    session id never appears raw in the usage log.
"""
from __future__ import annotations

import asyncio
import json
import re

import pytest

from conftest import call_tool_sync, _content_of

FAKE_PNG = b"\x89PNG\r\n\x1a\n" + b"abc" * 40


def _sid(text: str) -> str:
    m = re.search(r"session_id:\s*([A-Za-z0-9_\-]+)", text)
    assert m, f"create_session must return 'session_id: <token>'; got {text!r}"
    return m.group(1)


def _new_session(mcp_server) -> str:
    return _sid(call_tool_sync(mcp_server, "create_session", {}))


# ---------------------------------------------------------------------------
# Session lifecycle
# ---------------------------------------------------------------------------

def test_create_session_returns_unguessable_distinct_ids(mcp_server, server_module):
    a = _new_session(mcp_server)
    b = _new_session(mcp_server)
    assert a != b
    # token_urlsafe(32) → 43 URL-safe chars; must satisfy the validation regex.
    assert server_module._sessions.SESSION_ID_RE.match(a)
    assert len(a) >= 40
    assert server_module.SESSION_STORE.stats()["live_sessions"] >= 2


def test_end_session_discards_state(strict_mcp_server):
    mcp_server = strict_mcp_server
    sid = _new_session(mcp_server)
    call_tool_sync(mcp_server, "set_output_directory", {"path": "/tmp/x", "session_id": sid})
    assert "Session ended" in call_tool_sync(mcp_server, "end_session", {"session_id": sid})
    text = call_tool_sync(mcp_server, "get_output_directory", {"session_id": sid})
    assert "Error" in text and "expired" in text.lower()
    assert "create_session" in text


# ---------------------------------------------------------------------------
# Cross-tenant isolation
# ---------------------------------------------------------------------------

def test_output_directory_is_isolated_between_sessions(strict_mcp_server):
    mcp_server = strict_mcp_server
    a = _new_session(mcp_server)
    b = _new_session(mcp_server)
    call_tool_sync(mcp_server, "set_output_directory", {"path": "/Users/alice/out", "session_id": a})
    call_tool_sync(mcp_server, "set_output_directory", {"path": "/Users/bob/out", "session_id": b})

    ta = call_tool_sync(mcp_server, "get_output_directory", {"session_id": a})
    tb = call_tool_sync(mcp_server, "get_output_directory", {"session_id": b})
    assert "/Users/alice/out" in ta and "bob" not in ta
    assert "/Users/bob/out" in tb and "alice" not in tb


def test_plot_registry_is_isolated_between_sessions(server_module, strict_mcp_server):
    mcp_server = strict_mcp_server
    a = _new_session(mcp_server)
    b = _new_session(mcp_server)
    # Seed a plot into session A the way the plot tools do (via the ContextVar).
    state_a = server_module.SESSION_STORE.get(a)
    tok = server_module._sessions.bind(state_a)
    try:
        assert server_module._register_plot("alice_volcano.png", FAKE_PNG, "/Users/alice/out/alice_volcano.png")
    finally:
        server_module._sessions.unbind(tok)

    # A can fetch it.
    res = asyncio.run(mcp_server.call_tool("fetch_plot", {"filename": "alice_volcano.png", "session_id": a}))
    content = _content_of(res)
    from mcp import types as mcp_types
    assert any(isinstance(c, mcp_types.EmbeddedResource) for c in content)
    assert str(next(c for c in content if isinstance(c, mcp_types.EmbeddedResource)).resource.uri) == f"plot://{a}/alice_volcano.png"

    # B cannot — not by tool…
    tb = call_tool_sync(mcp_server, "fetch_plot", {"filename": "alice_volcano.png", "session_id": b})
    assert "No plot named `alice_volcano.png`" in tb
    assert "No plots are currently in this session's registry" in tb
    # …nor by listing…
    assert "No plots are currently available" in call_tool_sync(mcp_server, "get_save_script", {"session_id": b})
    # …nor by resource URI under its own session…
    with pytest.raises(Exception):
        asyncio.run(mcp_server.read_resource(f"plot://{b}/alice_volcano.png"))
    # …while A's resource URI works.
    got = asyncio.run(mcp_server.read_resource(f"plot://{a}/alice_volcano.png"))
    assert any(getattr(c, "content", None) == FAKE_PNG for c in got)


def test_plot_resource_rejects_unknown_session(strict_mcp_server):
    mcp_server = strict_mcp_server
    with pytest.raises(Exception) as exc:
        asyncio.run(mcp_server.read_resource("plot://not-a-real-session-id-0000000000/x.png"))
    assert "create_session" in str(exc.value)


def test_local_session_id_not_honoured_under_strict_mcp_server(strict_mcp_server):
    mcp_server = strict_mcp_server
    """The implicit stdio session id must not be usable as a shared bucket on
    the public endpoint."""
    text = call_tool_sync(mcp_server, "get_output_directory", {"session_id": "local"})
    assert "Error" in text


# ---------------------------------------------------------------------------
# Session-required policy and error messages
# ---------------------------------------------------------------------------

STRICT_CASES = [
    ("get_output_directory", {}),
    ("set_output_directory", {"path": "/tmp/x"}),
    ("get_save_script", {}),
    ("fetch_plot", {"filename": "a.png"}),
    ("get_neo4j_schema", {}),
    ("query", {"query": "MATCH (n) RETURN n LIMIT 1"}),
    ("get_study_info", {"study_id": "OSD-1"}),
    ("create_volcano_plot", {"assay_id": "OSD-1-x"}),
    ("clean_mermaid_diagram", {"mermaid_content": "classDiagram"}),
]


@pytest.mark.parametrize("tool,args", STRICT_CASES)
def test_strict_policy_marks_session_id_required_in_schema(strict_mcp_server, tool, args):
    """Under strict policy the schema itself says session_id is required, so
    LLM clients supply it proactively; omitting it is a validation error."""
    t = next(tt for tt in asyncio.run(strict_mcp_server.list_tools()) if tt.name == tool)
    assert "session_id" in t.input_schema.get("required", []), tool
    with pytest.raises(Exception) as exc:
        asyncio.run(strict_mcp_server.call_tool(tool, args))
    assert "session_id" in str(exc.value)


@pytest.mark.parametrize("tool,args", STRICT_CASES)
def test_strict_policy_rejects_empty_session_id_with_guidance(strict_mcp_server, tool, args):
    text = call_tool_sync(strict_mcp_server, tool, {**args, "session_id": ""})
    assert text.startswith("Error (missing session)"), f"{tool}: {text!r}"
    assert "create_session" in text


def test_create_session_needs_no_session_id(strict_mcp_server):
    t = next(tt for tt in asyncio.run(strict_mcp_server.list_tools()) if tt.name == "create_session")
    assert "session_id" not in (t.input_schema.get("properties") or {})


def test_unknown_session_id_error_names_recovery(strict_mcp_server):
    mcp_server = strict_mcp_server
    text = call_tool_sync(mcp_server, "get_output_directory", {"session_id": "A" * 43})
    assert "Error (unknown session)" in text and "create_session" in text


def test_malformed_session_id_is_rejected_before_lookup(strict_mcp_server):
    mcp_server = strict_mcp_server
    text = call_tool_sync(mcp_server, "get_output_directory", {"session_id": "bad id\nwith newline!"})
    assert "Error (malformed session)" in text and "create_session" in text


def test_expired_session_reports_expired_and_drops_plots(server_module, strict_mcp_server, monkeypatch):
    mcp_server = strict_mcp_server
    sid = _new_session(mcp_server)
    state = server_module.SESSION_STORE.get(sid)
    tok = server_module._sessions.bind(state)
    try:
        server_module._register_plot("p.png", FAKE_PNG, "/tmp/p.png")
    finally:
        server_module._sessions.unbind(tok)
    # Age the session past the idle TTL.
    monkeypatch.setattr(server_module._sessions, "SESSION_IDLE_TTL_SECONDS", 0.01)
    import time as _t
    _t.sleep(0.05)
    text = call_tool_sync(mcp_server, "fetch_plot", {"filename": "p.png", "session_id": sid})
    assert "Error (expired session)" in text and "create_session" in text
    stats = server_module.SESSION_STORE.stats()
    assert stats["retained_plots"] == 0 and stats["expired_total"] >= 1


def test_lenient_policy_allows_stateless_tools_but_not_stateful(driver, monkeypatch, server_module):
    monkeypatch.setattr(server_module, "SESSION_POLICY", "lenient")
    mcp_server = server_module.create_mcp_server(driver, database="testdb", instructions="")
    # A pure-KG tool runs anonymously (fake driver returns nothing → benign text).
    text = call_tool_sync(mcp_server, "get_node_metadata", {})
    assert not text.startswith("Error (missing session)")
    # A per-user-state tool still needs a session.
    text = call_tool_sync(mcp_server, "set_output_directory", {"path": "/tmp/x"})
    assert text.startswith("Error (missing session)")


def test_implicit_policy_needs_no_session_id(mcp_server, server_module):
    """Local stdio mode (the test default): tools work with no session_id and
    share the fixed local session."""
    assert server_module.SESSION_POLICY == "implicit"
    call_tool_sync(mcp_server, "set_output_directory", {"path": "/Users/jane/Downloads"})
    assert "/Users/jane/Downloads" in call_tool_sync(mcp_server, "get_output_directory", {})


# ---------------------------------------------------------------------------
# Bounds
# ---------------------------------------------------------------------------

def test_per_session_plot_cap_is_fifo(server_module):
    st = server_module._sessions.SessionState(session_id="s")
    cap = server_module._sessions.MAX_PLOTS_PER_SESSION
    for i in range(cap + 3):
        st.register_plot(f"p{i}.png", FAKE_PNG, f"/tmp/p{i}.png")
    assert len(st.plots) == cap
    assert st.list_plots()[0] == "p3.png"
    assert st.plot_bytes == cap * len(FAKE_PNG)


def test_global_plot_byte_cap_evicts_lru_sessions_first(server_module, monkeypatch):
    sessions = server_module._sessions
    store = server_module.SESSION_STORE
    monkeypatch.setattr(sessions, "MAX_TOTAL_PLOT_BYTES", len(FAKE_PNG) * 3)
    old = store.create()
    new = store.create()
    for i in range(3):
        old.register_plot(f"o{i}.png", FAKE_PNG, "/tmp/o")
    store.get(new.session_id)  # touch → `new` is most recently used
    new.register_plot("n0.png", FAKE_PNG, "/tmp/n")
    store.after_mutation()
    assert len(new.plots) == 1, "most-recently-used session keeps its plot"
    assert len(old.plots) == 2, "oldest plot of the LRU session was evicted"


def test_max_sessions_cap_refuses_rather_than_evicting_active_users(server_module, monkeypatch):
    """A create_session flood must not log active users out: at the cap,
    only sessions idle >= SESSION_EVICTION_MIN_IDLE_SECONDS are evicted (LRU
    first); otherwise creation is refused with an actionable error."""
    sessions = server_module._sessions
    store = server_module.SESSION_STORE
    store.clear()  # drop the autouse local session so the cap is exact
    monkeypatch.setattr(sessions, "MAX_SESSIONS", 3)
    ids = [store.create().session_id for _ in range(3)]
    with pytest.raises(sessions.SessionError) as exc:
        store.create()
    assert exc.value.reason == "capacity" and "Retry" in str(exc.value)
    assert all(store.get(i, touch=False) for i in ids), "existing sessions untouched"
    # Once one session has been idle long enough it may be recycled.
    monkeypatch.setattr(sessions, "SESSION_EVICTION_MIN_IDLE_SECONDS", 0.0)
    new = store.create()
    assert len(store) == 3
    with pytest.raises(sessions.SessionError) as exc2:
        store.get(ids[0])
    assert exc2.value.reason == "expired"  # evicted ids are tombstoned → actionable


def test_create_session_tool_reports_capacity(strict_mcp_server, server_module, monkeypatch):
    monkeypatch.setattr(server_module._sessions, "MAX_SESSIONS", 2)  # local + one
    _new_session(strict_mcp_server)
    text = call_tool_sync(strict_mcp_server, "create_session", {})
    assert text.startswith("Error (capacity session)") and "Retry" in text


def test_forged_mcp_session_id_header_is_ignored(server_module, driver, monkeypatch):
    """In stateless mode a client-supplied Mcp-Session-Id header reaches the
    request unvalidated; it must never select a session (two clients sending
    the same header would otherwise share state)."""
    monkeypatch.setattr(server_module, "SESSION_POLICY", "lenient")
    srv = server_module.create_mcp_server(driver, database="testdb", instructions="")
    from starlette.testclient import TestClient
    H = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json",
         "mcp-session-id": "shared-forged-session-000000"}
    with TestClient(srv.streamable_http_app(**server_module.STREAMABLE_HTTP_OPTIONS), base_url="http://127.0.0.1:8000") as c:
        for i, body in enumerate([
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "x", "version": "1"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "set_output_directory", "arguments": {"path": "/home/victim"}}},
        ]):
            r = c.post("/mcp", headers=H, json=body)
        assert "Error (missing session)" in r.text
    with pytest.raises(server_module._sessions.SessionError):
        server_module.SESSION_STORE.get("shared-forged-session-000000")


# ---------------------------------------------------------------------------
# Tool surface
# ---------------------------------------------------------------------------

def test_every_tool_except_create_session_accepts_session_id(tools_list):
    missing = [
        t.name for t in tools_list
        if t.name != "create_session" and "session_id" not in (t.input_schema.get("properties") or {})
    ]
    assert not missing, f"Tools without a session_id parameter: {missing}"


def test_session_id_optional_in_schema_under_implicit_policy(tools_list):
    """Local stdio mode: no tool forces the LLM to invent a session id."""
    for t in tools_list:
        assert "session_id" not in t.input_schema.get("required", []), t.name


def test_default_instructions_carry_session_protocol(server_module):
    import inspect
    src = inspect.getsource(server_module.async_main)
    assert "SESSION PROTOCOL" in src and "create_session" in src


# ---------------------------------------------------------------------------
# HTTP routes for the ALB
# ---------------------------------------------------------------------------

def _client(mcp_server):
    """Starlette TestClient over the SAME app the container runs: the
    transport options (stateless_http=True, body cap) come from
    server.STREAMABLE_HTTP_OPTIONS, exactly as run_streamable_http_async gets
    them. base_url matters: when the SDK binds to a loopback host it enables
    DNS-rebinding protection (Host header must be local). In production the
    bind host is 0.0.0.0, so ALB/CloudFront Host headers are accepted."""
    from starlette.testclient import TestClient
    from conftest import _SERVER_MODULE
    return TestClient(
        mcp_server.streamable_http_app(**_SERVER_MODULE.STREAMABLE_HTTP_OPTIONS),
        base_url="http://127.0.0.1:8000",
    )


def test_healthz_is_get_200_without_db(mcp_server, driver):
    with _client(mcp_server) as c:
        r = c.get("/healthz")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    assert driver.calls == [], "liveness must not touch Neo4j"


def test_readyz_200_when_neo4j_answers(mcp_server, driver):
    driver.route = lambda q, p: [{"ok": 1}]
    with _client(mcp_server) as c:
        r = c.get("/readyz")
    assert r.status_code == 200 and r.json()["status"] == "ready"
    assert any("RETURN 1" in q for q, _ in driver.calls)


def test_readyz_503_when_neo4j_fails(mcp_server, driver):
    def boom(q, p):
        raise RuntimeError("ServiceUnavailable: bolt down")
    driver.route = boom
    with _client(mcp_server) as c:
        r = c.get("/readyz")
    assert r.status_code == 503 and r.json()["status"] == "degraded"


def test_readyz_503_on_timeout(mcp_server, driver, server_module, monkeypatch):
    monkeypatch.setattr(server_module, "READYZ_TIMEOUT_SECONDS", 0.05)

    async def slow_read(tx, query, params):
        await asyncio.sleep(0.5)  # a Neo4j that hangs
        return "[]"
    monkeypatch.setattr(server_module, "_read", slow_read)
    with _client(mcp_server) as c:
        r = c.get("/readyz")
    assert r.status_code == 503


def test_readyz_caches_verdict_briefly(mcp_server, driver, server_module, monkeypatch):
    monkeypatch.setattr(server_module, "READYZ_CACHE_SECONDS", 60.0)
    driver.route = lambda q, p: [{"ok": 1}]
    with _client(mcp_server) as c:
        for _ in range(5):
            assert c.get("/readyz").status_code == 200
    assert len(driver.calls) == 1, "probe bursts must not amplify into Neo4j queries"


def test_metrics_endpoint_off_by_default(mcp_server, server_module):
    assert server_module._metrics.METRICS_ENDPOINT_ENABLED is False
    with _client(mcp_server) as c:
        r = c.get("/metrics")
    assert r.status_code == 404


def test_metrics_endpoint_when_enabled(server_module, driver, monkeypatch):
    monkeypatch.setattr(server_module._metrics, "METRICS_ENDPOINT_ENABLED", True)
    server_module._metrics.reset()
    mcp_server = server_module.create_mcp_server(driver, database="testdb", instructions="")
    call_tool_sync(mcp_server, "get_output_directory", {})
    with _client(mcp_server) as c:
        r = c.get("/metrics")
        assert r.status_code == 200
        assert 'mcp_genelab_tool_calls_total{service="mcp-genelab",tool="get_output_directory"} 1' in r.text
        rj = c.get("/metrics", headers={"Accept": "application/json"})
        assert rj.json()["tools"]["get_output_directory"]["calls"] == 1


# ---------------------------------------------------------------------------
# Usage metrics
# ---------------------------------------------------------------------------

def test_tool_calls_are_counted_and_logged(server_module, caplog, strict_mcp_server, monkeypatch):
    mcp_server = strict_mcp_server
    monkeypatch.setattr(server_module._metrics, "USAGE_LOG_ENABLED", True)
    server_module._metrics.reset()
    sid = _new_session(mcp_server)
    with caplog.at_level("INFO", logger="mcp-genelab.usage"):
        call_tool_sync(mcp_server, "get_output_directory", {"session_id": sid})
        call_tool_sync(mcp_server, "fetch_plot", {"filename": "nope.png", "session_id": sid})  # "No plot named…" (not an Error)
        call_tool_sync(mcp_server, "get_output_directory", {"session_id": ""})  # missing session → error
    snap = server_module._metrics.REGISTRY.snapshot()
    assert snap["sessions_created_total"] == 1
    assert snap["tools"]["get_output_directory"]["calls"] == 2
    assert snap["tools"]["get_output_directory"]["errors"] == 1
    assert snap["tools"]["fetch_plot"]["calls"] == 1
    # A "not in registry" reply is guidance, not an `Error…` block: not counted as an error.
    assert snap["tools"]["fetch_plot"]["errors"] == 0
    assert snap["sessions_rejected"].get("missing") == 1

    events = [json.loads(r.getMessage()) for r in caplog.records if r.name == "mcp-genelab.usage"]
    kinds = [e["event"] for e in events]
    assert "session_created" in kinds and kinds.count("tool_call") == 4  # incl. create_session
    tc = [e for e in events if e["event"] == "tool_call"]
    assert all("duration_ms" in e and "status" in e for e in tc)
    # The raw session id is a bearer secret and must never be logged.
    assert not any(sid in r.getMessage() for r in caplog.records)
    assert any(e["session"] == server_module._sessions.session_id_digest(sid) for e in tc)


def test_emf_document_shape_when_enabled(server_module, monkeypatch, caplog):
    m = server_module._metrics
    monkeypatch.setattr(m, "EMF_ENABLED", True)
    monkeypatch.setattr(m, "USAGE_LOG_ENABLED", True)
    with caplog.at_level("INFO", logger="mcp-genelab.usage"):
        m.REGISTRY.record_tool_call("query", 12.5, "ok", session_digest="abc")
    docs = [json.loads(r.getMessage()) for r in caplog.records if r.name == "mcp-genelab.usage"]
    emf = [d for d in docs if "_aws" in d]
    assert len(emf) == 1
    d = emf[0]
    directive = d["_aws"]["CloudWatchMetrics"][0]
    assert directive["Namespace"] == m.METRICS_NAMESPACE
    assert ["Service", "Tool"] in directive["Dimensions"]
    names = {x["Name"] for x in directive["Metrics"]}
    assert names == {"ToolCalls", "ToolErrors", "ToolLatencyMs"}
    assert d["Tool"] == "query" and d["ToolCalls"] == 1 and d["ToolLatencyMs"] == 12.5
    assert isinstance(d["_aws"]["Timestamp"], int)


# ---------------------------------------------------------------------------
# Client fingerprint (distinct conversations per client)
# ---------------------------------------------------------------------------

def _init_and_create_session(client, headers):
    H = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json", **headers}
    client.post("/mcp", headers=H, json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "x", "version": "1"}}})
    client.post("/mcp", headers=H, json={"jsonrpc": "2.0", "method": "notifications/initialized"})
    r = client.post("/mcp", headers=H, json={"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                    "params": {"name": "create_session", "arguments": {}}})
    assert r.status_code == 200 and "session_id:" in r.text, r.text


def test_session_created_carries_salted_client_fingerprint(server_module, driver, monkeypatch, caplog):
    """Over HTTP each create_session logs a client_fp derived from the viewer
    IP (first X-Forwarded-For hop) + User-Agent: stable for the same client,
    different for another client, never the raw IP, and tied to the session
    digest so tool_call events can be joined to it."""
    monkeypatch.setattr(server_module, "SESSION_POLICY", "strict")
    monkeypatch.setattr(server_module._metrics, "USAGE_LOG_ENABLED", True)
    srv = server_module.create_mcp_server(driver, database="testdb", instructions="")
    with caplog.at_level("INFO", logger="mcp-genelab.usage"):
        with _client(srv) as c:
            ua = {"User-Agent": "claude-ai/1.0"}
            _init_and_create_session(c, {"X-Forwarded-For": "203.0.113.7, 130.176.1.1", **ua})
            _init_and_create_session(c, {"X-Forwarded-For": "203.0.113.7, 130.176.9.9", **ua})   # same viewer, 2nd chat
            _init_and_create_session(c, {"X-Forwarded-For": "198.51.100.2", **ua})               # another viewer
    events = [json.loads(r.getMessage()) for r in caplog.records if r.name == "mcp-genelab.usage"]
    created = [e for e in events if e["event"] == "session_created"]
    assert len(created) == 3
    fps = [e["client_fp"] for e in created]
    assert fps[0] == fps[1] != fps[2]
    assert all(len(f) == 16 for f in fps)
    assert all(e["session"] != "-" for e in created)
    assert not any("203.0.113.7" in r.getMessage() for r in caplog.records), "raw IP must never be logged"
    # tool_call events for create_session carry the same fingerprint
    tc = [e for e in events if e["event"] == "tool_call" and e["tool"] == "create_session"]
    assert [e["client_fp"] for e in tc] == fps


def test_client_fingerprint_empty_without_headers(server_module, mcp_server):
    """stdio / in-process calls have no headers → no fingerprint (never a
    constant that would lump everyone together)."""
    assert server_module._client_fingerprint(None) == ""
    assert server_module._client_ip({"x-forwarded-for": "1.2.3.4, 5.6.7.8"}) == "1.2.3.4"
    assert server_module._client_ip({"cloudfront-viewer-address": "2001:db8::1:443"}) == "2001:db8::1"


def test_initialize_logs_which_mcp_client_connected(server_module, driver, monkeypatch, caplog):
    """The MCP initialize handshake's clientInfo identifies the client
    software; it is logged once as client_initialized with the same client_fp
    the client's sessions/tool calls will carry."""
    monkeypatch.setattr(server_module._metrics, "USAGE_LOG_ENABLED", True)
    server_module._metrics.reset()
    srv = server_module.create_mcp_server(driver, database="testdb", instructions="")
    with caplog.at_level("INFO", logger="mcp-genelab.usage"):
        with _client(srv) as c:
            _init_and_create_session(c, {"X-Forwarded-For": "203.0.113.9", "User-Agent": "Claude-User/1.0"})
    events = [json.loads(r.getMessage()) for r in caplog.records if r.name == "mcp-genelab.usage"]
    init = [e for e in events if e["event"] == "client_initialized"]
    assert len(init) == 1
    assert init[0]["client_name"] == "x" and init[0]["client_version"] == "1"
    assert init[0]["protocol_version"] == "2025-06-18"
    created = next(e for e in events if e["event"] == "session_created")
    assert init[0]["client_fp"] == created["client_fp"] != ""
    assert server_module._metrics.REGISTRY.snapshot()["clients"] == {"x/1": 1}
