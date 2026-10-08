"""Usage metrics for mcp-genelab on a shared-process (ECS Fargate) deployment.

Behind an ALB nothing observes tool calls except this process, so
the server records them itself and exposes them three ways — pick whichever
fits the account's tooling; all can be on at once:

1. **Structured usage log** (``MCP_USAGE_LOG``; default ON for remote
   transports, OFF for stdio): one JSON line on **stderr** per tool call, e.g.::

       {"event":"tool_call","tool":"create_volcano_plot","session":"3f1a…",
        "status":"ok","duration_ms":412.7,"response_bytes":5120,"client":"claude-ai/1.0","ts":"…"}

   With the ECS ``awslogs`` log driver these land in CloudWatch Logs where
   Logs Insights can aggregate them (``stats count() by tool``) and metric
   filters can turn them into alarms. Session ids are logged only as a short
   SHA-256 digest — they are bearer secrets. ``session`` identifies one
   conversation (one ``create_session``); ``client_fp`` is a salted digest of
   viewer IP + User-Agent that groups the conversations of one client, so
   "distinct chats per user" is ``filter event="session_created" | stats
   count() by client_fp``.

2. **CloudWatch Embedded Metric Format** (``MCP_METRICS_EMF=1``): the same
   event is additionally emitted as an EMF document, which CloudWatch Logs
   turns into real CloudWatch metrics automatically (no agent, no PutMetricData
   IAM permission). Metrics: ``ToolCalls``, ``ToolErrors``, ``ToolLatencyMs``
   with dimensions ``[Service]`` and ``[Service, Tool]``, plus
   ``SessionsCreated``. Namespace via ``MCP_METRICS_NAMESPACE`` (default
   ``mcp-genelab``). Format per the CloudWatch EMF specification.

3. **Pull endpoint** (``MCP_METRICS_ENDPOINT=1``): ``GET /metrics`` returns
   Prometheus text exposition (or JSON with ``Accept: application/json``) of
   in-process counters — call/error counts and latency sums per tool, live
   sessions, retained plot bytes. Intended for a CloudWatch agent / ADOT
   sidecar or an internal scrape; counters reset when the task restarts. OFF by
   default because the ALB would otherwise expose it publicly — if you enable
   it, add an ALB listener rule that only allows ``/metrics`` from trusted
   sources (or leave it internal and have the sidecar hit ``localhost:8000``).

Nothing here talks to AWS APIs directly, so the image needs no boto3 and the
task role needs no extra permissions.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import sys
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional


def _env_flag(name: str, default: bool) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


_REMOTE_TRANSPORT = os.getenv("MCP_TRANSPORT", "stdio").lower() in ("streamable-http", "http", "sse")
# Off by default in stdio mode: stdout is the JSON-RPC wire there and stderr
# is the user's terminal — usage lines would only be noise.
USAGE_LOG_ENABLED: bool = _env_flag("MCP_USAGE_LOG", _REMOTE_TRANSPORT)
EMF_ENABLED: bool = _env_flag("MCP_METRICS_EMF", False)
METRICS_ENDPOINT_ENABLED: bool = _env_flag("MCP_METRICS_ENDPOINT", False)
METRICS_NAMESPACE: str = os.getenv("MCP_METRICS_NAMESPACE", "mcp-genelab")
SERVICE_NAME: str = os.getenv("MCP_SERVICE_NAME", "mcp-genelab")

# Salt for the client fingerprint (viewer IP + User-Agent → 16 hex chars).
# Set MCP_USAGE_FP_SALT (e.g. from Secrets Manager) so fingerprints are
# comparable across task restarts; otherwise a random per-process salt is
# used and they are only comparable within one task's lifetime.
_FP_SALT: str = os.getenv("MCP_USAGE_FP_SALT") or secrets.token_hex(16)
FP_SALT_IS_PERSISTENT: bool = bool(os.getenv("MCP_USAGE_FP_SALT"))


def fingerprint(value: str) -> str:
    """Salted, non-reversible 16-hex-char digest used for client_fp."""
    return hashlib.sha256((_FP_SALT + "|" + value).encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Dedicated stderr logger for machine-readable lines
# ---------------------------------------------------------------------------
# The application logger (the MCP SDK configures a Rich handler on stderr) is for
# humans. Usage/EMF lines must be exactly one JSON document per line with no
# prefix, so they get their own logger, their own plain handler, and
# propagate=False so the root handler never decorates them. The handler
# writes to STDERR, never stdout: in stdio transport stdout IS the JSON-RPC
# channel, and the ECS awslogs driver captures stderr just as it does stdout
# (CloudWatch EMF extraction works on either stream).

_usage_logger = logging.getLogger("mcp-genelab.usage")
_usage_logger.propagate = False
_usage_logger.setLevel(logging.INFO)
if not _usage_logger.handlers:
    _h = logging.StreamHandler(sys.stderr)
    _h.setFormatter(logging.Formatter("%(message)s"))
    _usage_logger.addHandler(_h)


def _emit_line(doc: dict[str, Any]) -> None:
    try:
        _usage_logger.info(json.dumps(doc, separators=(",", ":"), default=str))
    except Exception:  # never let metrics break a tool call
        pass


# ---------------------------------------------------------------------------
# In-process counters
# ---------------------------------------------------------------------------

@dataclass
class _ToolStats:
    calls: int = 0
    errors: int = 0
    latency_ms_sum: float = 0.0
    latency_ms_max: float = 0.0
    response_bytes_sum: int = 0
    response_bytes_max: int = 0
    last_call_ts: float = 0.0


@dataclass
class MetricsRegistry:
    started_at: float = field(default_factory=time.time)
    tools: "dict[str, _ToolStats]" = field(default_factory=lambda: defaultdict(_ToolStats))
    sessions_created: int = 0
    sessions_rejected: "dict[str, int]" = field(default_factory=lambda: defaultdict(int))
    clients: "dict[str, int]" = field(default_factory=lambda: defaultdict(int))  # "name/version" -> initializations
    plots_registered: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    # -- recording -----------------------------------------------------------
    def record_tool_call(
        self,
        tool: str,
        duration_ms: float,
        status: str,
        session_digest: str = "-",
        client: str = "",
        error_type: Optional[str] = None,
        client_fp: str = "",
        response_bytes: int = 0,
    ) -> None:
        with self._lock:
            s = self.tools[tool]
            s.calls += 1
            if status != "ok":
                s.errors += 1
            s.latency_ms_sum += duration_ms
            s.latency_ms_max = max(s.latency_ms_max, duration_ms)
            s.response_bytes_sum += int(response_bytes)
            s.response_bytes_max = max(s.response_bytes_max, int(response_bytes))
            s.last_call_ts = time.time()

        if USAGE_LOG_ENABLED:
            _emit_line({
                "event": "tool_call",
                "service": SERVICE_NAME,
                "tool": tool,
                "session": session_digest,
                "client_fp": client_fp,
                "status": status,
                "error_type": error_type,
                "duration_ms": round(duration_ms, 1),
                "response_bytes": int(response_bytes),
                "client": client[:120] if client else "",
                "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            })
        if EMF_ENABLED:
            _emit_line(self._emf_tool_call(tool, duration_ms, status, response_bytes))

    def record_client_initialized(
        self,
        client_name: str = "",
        client_version: str = "",
        protocol_version: str = "",
        client: str = "",
        client_fp: str = "",
    ) -> None:
        """One event per MCP `initialize` handshake: which client software
        connected (clientInfo from the handshake) plus the client fingerprint
        that its later session_created / tool_call events carry."""
        with self._lock:
            key = f"{client_name or 'unknown'}/{client_version or '-'}"
            self.clients[key] += 1
        if USAGE_LOG_ENABLED:
            _emit_line({
                "event": "client_initialized",
                "service": SERVICE_NAME,
                "client_name": client_name,
                "client_version": client_version,
                "protocol_version": protocol_version,
                "client_fp": client_fp,
                "client": client[:120] if client else "",
                "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            })
        if EMF_ENABLED:
            _emit_line(self._emf_simple("ClientInitializations", 1, extra={"ClientName": client_name or "unknown"}))

    def record_session_created(self, client: str = "", session_digest: str = "-", client_fp: str = "") -> None:
        with self._lock:
            self.sessions_created += 1
        if USAGE_LOG_ENABLED:
            _emit_line({
                "event": "session_created",
                "service": SERVICE_NAME,
                "session": session_digest,
                "client_fp": client_fp,
                "client": client[:120] if client else "",
                "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            })
        if EMF_ENABLED:
            _emit_line(self._emf_simple("SessionsCreated", 1))

    def record_session_rejected(self, reason: str, tool: str = "") -> None:
        with self._lock:
            self.sessions_rejected[reason] += 1
        if USAGE_LOG_ENABLED:
            _emit_line({
                "event": "session_rejected",
                "service": SERVICE_NAME,
                "reason": reason,
                "tool": tool,
                "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            })
        if EMF_ENABLED:
            _emit_line(self._emf_simple("SessionsRejected", 1, extra={"Reason": reason}))

    def record_plot_registered(self) -> None:
        with self._lock:
            self.plots_registered += 1

    # -- EMF documents ---------------------------------------------------------
    @staticmethod
    def _now_ms() -> int:
        return int(time.time() * 1000)

    def _emf_tool_call(self, tool: str, duration_ms: float, status: str, response_bytes: int = 0) -> dict[str, Any]:
        return {
            "_aws": {
                "Timestamp": self._now_ms(),
                "CloudWatchMetrics": [{
                    "Namespace": METRICS_NAMESPACE,
                    "Dimensions": [["Service"], ["Service", "Tool"]],
                    "Metrics": [
                        {"Name": "ToolCalls", "Unit": "Count"},
                        {"Name": "ToolErrors", "Unit": "Count"},
                        {"Name": "ToolLatencyMs", "Unit": "Milliseconds"},
                        {"Name": "ToolResponseBytes", "Unit": "Bytes"},
                    ],
                }],
            },
            "Service": SERVICE_NAME,
            "Tool": tool,
            "ToolCalls": 1,
            "ToolErrors": 0 if status == "ok" else 1,
            "ToolLatencyMs": round(duration_ms, 1),
            "ToolResponseBytes": int(response_bytes),
        }

    def _emf_simple(self, name: str, value: float, extra: Optional[dict[str, str]] = None) -> dict[str, Any]:
        dims = [["Service"]]
        doc: dict[str, Any] = {
            "_aws": {
                "Timestamp": self._now_ms(),
                "CloudWatchMetrics": [{
                    "Namespace": METRICS_NAMESPACE,
                    "Dimensions": dims,
                    "Metrics": [{"Name": name, "Unit": "Count"}],
                }],
            },
            "Service": SERVICE_NAME,
            name: value,
        }
        if extra:
            dims.append(["Service", *extra.keys()])
            doc.update(extra)
        return doc

    # -- exposition --------------------------------------------------------------
    def snapshot(self, session_stats: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        with self._lock:
            tools = {
                name: {
                    "calls": s.calls,
                    "errors": s.errors,
                    "latency_ms_sum": round(s.latency_ms_sum, 1),
                    "latency_ms_avg": round(s.latency_ms_sum / s.calls, 1) if s.calls else 0.0,
                    "latency_ms_max": round(s.latency_ms_max, 1),
                    "response_bytes_sum": s.response_bytes_sum,
                    "response_bytes_max": s.response_bytes_max,
                }
                for name, s in sorted(self.tools.items())
            }
            out: dict[str, Any] = {
                "service": SERVICE_NAME,
                "uptime_seconds": round(time.time() - self.started_at, 1),
                "tool_calls_total": sum(s.calls for s in self.tools.values()),
                "tool_errors_total": sum(s.errors for s in self.tools.values()),
                "tool_response_bytes_total": sum(s.response_bytes_sum for s in self.tools.values()),
                "sessions_created_total": self.sessions_created,
                "sessions_rejected": dict(self.sessions_rejected),
                "clients": dict(self.clients),
                "plots_registered_total": self.plots_registered,
                "tools": tools,
            }
        if session_stats:
            out["sessions"] = session_stats
        return out

    def prometheus_text(self, session_stats: Optional[dict[str, Any]] = None) -> str:
        snap = self.snapshot(session_stats)
        svc = SERVICE_NAME
        lines: list[str] = []

        def gauge(name: str, help_: str, value: Any, labels: str = "") -> None:
            lines.append(f"# HELP {name} {help_}")
            lines.append(f"# TYPE {name} gauge")
            lines.append(f'{name}{{service="{svc}"{labels}}} {value}')

        gauge("mcp_genelab_uptime_seconds", "Seconds since process start.", snap["uptime_seconds"])
        gauge("mcp_genelab_sessions_created_total", "Sessions minted by create_session.", snap["sessions_created_total"])
        gauge("mcp_genelab_plots_registered_total", "Plots registered across all sessions.", snap["plots_registered_total"])
        for reason, n in snap["sessions_rejected"].items():
            lines.append(f'mcp_genelab_sessions_rejected_total{{service="{svc}",reason="{reason}"}} {n}')
        for key, n in snap["clients"].items():
            name, _, ver = key.partition("/")
            lines.append(f'mcp_genelab_client_initializations_total{{service="{svc}",client_name="{name}",client_version="{ver}"}} {n}')
        if "sessions" in snap:
            for k, v in snap["sessions"].items():
                gauge(f"mcp_genelab_session_{k}", f"Session store: {k}.", v)

        lines.append("# HELP mcp_genelab_tool_calls_total Tool invocations.")
        lines.append("# TYPE mcp_genelab_tool_calls_total counter")
        lines.append("# HELP mcp_genelab_tool_errors_total Tool invocations that raised or returned an error.")
        lines.append("# TYPE mcp_genelab_tool_errors_total counter")
        lines.append("# HELP mcp_genelab_tool_latency_ms_sum Sum of tool latencies (ms).")
        lines.append("# TYPE mcp_genelab_tool_latency_ms_sum counter")
        lines.append("# HELP mcp_genelab_tool_latency_ms_max Max tool latency (ms) since start.")
        lines.append("# TYPE mcp_genelab_tool_latency_ms_max gauge")
        lines.append("# HELP mcp_genelab_tool_response_bytes_sum Sum of tool result payload sizes (bytes).")
        lines.append("# TYPE mcp_genelab_tool_response_bytes_sum counter")
        for name, s in snap["tools"].items():
            lab = f'{{service="{svc}",tool="{name}"}}'
            lines.append(f"mcp_genelab_tool_calls_total{lab} {s['calls']}")
            lines.append(f"mcp_genelab_tool_errors_total{lab} {s['errors']}")
            lines.append(f"mcp_genelab_tool_latency_ms_sum{lab} {s['latency_ms_sum']}")
            lines.append(f"mcp_genelab_tool_latency_ms_max{lab} {s['latency_ms_max']}")
            lines.append(f"mcp_genelab_tool_response_bytes_sum{lab} {s['response_bytes_sum']}")
        return "\n".join(lines) + "\n"


# Process-wide singleton. Tests may replace it or call `.reset()`.
REGISTRY = MetricsRegistry()


def reset() -> None:
    """Test hook."""
    global REGISTRY
    REGISTRY = MetricsRegistry()
