"""Per-session state for a SHARED-PROCESS deployment of mcp-genelab.

Why this module exists
----------------------
The server is deployed as a single long-lived process serving many users
(ECS Fargate task behind an ALB). Nothing in that topology isolates one
user's request from another's, so any per-user state — the output directory
chosen with ``set_output_directory`` and the in-memory plot registry consumed
by ``fetch_plot`` / ``get_save_script`` / the ``plot://`` resource — MUST be
scoped to a session, never held in a module global.

How a session is identified
---------------------------
The MCP transport runs in *stateless* Streamable HTTP mode
(``stateless_http=True``). In that mode the MCP SDK issues **no**
``Mcp-Session-Id`` header at all (verified empirically against mcp 1.20, 1.29
and 2.2), and in *stateful* mode the SDK binds each session to the process that
created it, which an ALB spreading requests across tasks would break (ALB
stickiness is cookie-based only; it cannot key on a header). Session identity
is therefore an **application-level** concept:

* the agent calls the ``create_session`` tool once and receives an opaque,
  unguessable ``session_id``;
* it passes that ``session_id`` as an argument on every subsequent tool call;
* the server looks the id up in a bounded, TTL-evicting registry and binds the
  matching :class:`SessionState` to a :class:`contextvars.ContextVar` for the
  duration of the call, so helpers deeper in the call stack
  (``_resolve_output_paths``, ``_register_plot`` …) read per-session state
  without threading it through every signature;
* an unknown or expired id yields a clear error telling the agent to call
  ``create_session`` again.

The transport's ``Mcp-Session-Id`` request header is deliberately **ignored**:
in stateless mode the SDK does not validate it, so a client could send any
value and two clients sending the same value would share state. Only ids
minted by ``create_session`` are accepted on the public endpoint.

Bounds
------
Everything here is bounded so one process can serve many sessions safely:
idle TTL, absolute max age, a hard cap on the number of live sessions (LRU
eviction), a per-session plot cap, and a process-wide cap on retained plot
bytes. All bounds are env-tunable; see the ``MCP_SESSION_*`` variables.

Scaling note
------------
The default backend is in-memory, which is correct for a service running as a
single Fargate task (``desiredCount = 1``, scale vertically). To run several
tasks behind one ALB you must either (a) accept that a session is only valid on
the task that created it (the error path is explicit and recoverable — the
agent just creates a new session) or (b) plug in a shared backend by
subclassing :class:`SessionStore` (Redis/ElastiCache is the natural fit: the
interface is deliberately small).
"""
from __future__ import annotations

import contextvars
import hashlib
import os
import re
import secrets
import threading
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from typing import Optional


# ---------------------------------------------------------------------------
# Env-driven bounds
# ---------------------------------------------------------------------------

def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


# A session is dropped after this many seconds WITHOUT any tool call.
SESSION_IDLE_TTL_SECONDS: float = _env_float("MCP_SESSION_IDLE_TTL_SECONDS", 3600.0)

# A session is dropped this many seconds after creation regardless of use.
SESSION_MAX_AGE_SECONDS: float = _env_float("MCP_SESSION_MAX_AGE_SECONDS", 8 * 3600.0)

# Hard cap on concurrently-live sessions per process. At the cap, sessions
# that have been idle for at least SESSION_EVICTION_MIN_IDLE_SECONDS are
# evicted least-recently-used first; if none qualify, create() REFUSES the new
# session rather than wiping active users' state (an unauthenticated
# create_session flood must not be able to log everyone else out). A
# SessionState without plots is tiny, so the cap can be generous; retained
# plot bytes are bounded separately by MAX_TOTAL_PLOT_BYTES.
MAX_SESSIONS: int = _env_int("MCP_MAX_SESSIONS", 10000)
SESSION_EVICTION_MIN_IDLE_SECONDS: float = _env_float("MCP_SESSION_EVICTION_MIN_IDLE_SECONDS", 300.0)

# Plots retained per session (FIFO eviction)
MAX_PLOTS_PER_SESSION: int = _env_int("MCP_MAX_PLOTS_PER_SESSION", 8)

# Process-wide ceiling on retained PNG bytes across ALL sessions. When exceeded,
# the oldest plots of the least-recently-used sessions are dropped first.
# 256 MiB ≈ 1000+ typical 120-dpi volcano plots.
MAX_TOTAL_PLOT_BYTES: int = _env_int("MCP_MAX_TOTAL_PLOT_BYTES", 256 * 1024 * 1024)

# Session ids are URL-safe base64 tokens (secrets.token_urlsafe(32) → 43 chars).
# Validate shape BEFORE any lookup/logging so an attacker-supplied string can
# neither pollute logs nor probe the registry with arbitrary bytes.
SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{8,128}$")

# Reserved id of the implicit process-local session used in stdio mode (one
# user, one process). Never issued by create_session.
LOCAL_SESSION_ID = "local"


# ---------------------------------------------------------------------------
# Session state
# ---------------------------------------------------------------------------

@dataclass
class SessionState:
    """All per-user state the server keeps between tool calls."""

    session_id: str
    created_at: float = field(default_factory=time.monotonic)
    last_seen: float = field(default_factory=time.monotonic)
    created_wallclock: float = field(default_factory=time.time)
    output_dir: Optional[str] = None
    # suggested_filename -> (png_bytes, user_facing_path); insertion-ordered
    # so FIFO eviction is a simple popitem(last=False).
    plots: "OrderedDict[str, tuple[bytes, str]]" = field(default_factory=OrderedDict)
    tool_calls: int = 0
    plot_bytes: int = 0
    client_label: str = ""
    client_fp: str = ""  # salted client fingerprint (see server._client_fingerprint)

    # -- plot registry -------------------------------------------------------
    def register_plot(self, filename: str, png_bytes: bytes, user_facing_path: str) -> None:
        """Record a plot's bytes for later retrieval. Re-registering the same
        filename replaces it in place (and refreshes recency)."""
        if filename in self.plots:
            old_bytes, _ = self.plots.pop(filename)
            self.plot_bytes -= len(old_bytes)
        self.plots[filename] = (png_bytes, user_facing_path)
        self.plot_bytes += len(png_bytes)
        while len(self.plots) > MAX_PLOTS_PER_SESSION:
            self.evict_oldest_plot()

    def evict_oldest_plot(self) -> int:
        """Drop the oldest plot; return the number of bytes released."""
        if not self.plots:
            return 0
        _, (png_bytes, _) = self.plots.popitem(last=False)
        self.plot_bytes -= len(png_bytes)
        return len(png_bytes)

    def lookup_plot(self, filename: str) -> "Optional[tuple[bytes, str]]":
        return self.plots.get(filename)

    def list_plots(self) -> "list[str]":
        return list(self.plots.keys())

    # -- lifetime ------------------------------------------------------------
    def is_expired(self, now: Optional[float] = None) -> bool:
        now = time.monotonic() if now is None else now
        if SESSION_IDLE_TTL_SECONDS > 0 and (now - self.last_seen) > SESSION_IDLE_TTL_SECONDS:
            return True
        if SESSION_MAX_AGE_SECONDS > 0 and (now - self.created_at) > SESSION_MAX_AGE_SECONDS:
            return True
        return False

    def touch(self) -> None:
        self.last_seen = time.monotonic()
        self.tool_calls += 1

    def idle_seconds_remaining(self) -> float:
        if SESSION_IDLE_TTL_SECONDS <= 0:
            return float("inf")
        return max(0.0, SESSION_IDLE_TTL_SECONDS - (time.monotonic() - self.last_seen))


# ---------------------------------------------------------------------------
# Session store
# ---------------------------------------------------------------------------

class SessionError(Exception):
    """Raised when a session id cannot be resolved. ``reason`` is one of
    'missing', 'malformed', 'unknown', 'expired'. The message is written to be
    returned verbatim to the calling agent."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


def _session_not_found_message(reason: str) -> str:
    if reason == "expired":
        return (
            "Session expired. Sessions end after "
            f"{int(SESSION_IDLE_TTL_SECONDS // 60)} minutes of inactivity or "
            f"{int(SESSION_MAX_AGE_SECONDS // 3600)} hours total. Call "
            "`create_session()` to obtain a NEW session_id, then retry this "
            "call with it. Any plots from the old session must be regenerated."
        )
    if reason == "unknown":
        return (
            "Unknown session_id. It is not held by this server instance (it "
            "may have expired, or the server was restarted). Call "
            "`create_session()` to obtain a new session_id and retry this call "
            "with it."
        )
    if reason == "capacity":
        return (
            "The server is at its session capacity right now and no idle "
            "session could be released. Retry `create_session()` in a minute."
        )
    if reason == "malformed":
        return (
            "Malformed session_id. Pass exactly the value returned by "
            "`create_session()` (an opaque URL-safe token), or call "
            "`create_session()` to obtain one."
        )
    return (
        "A session_id is required. Call `create_session()` once to obtain a "
        "session_id, then pass it as the `session_id` argument on EVERY tool "
        "call (this server serves many users from one process; the id is what "
        "keeps your output directory and plots private to you)."
    )


class SessionStore:
    """Bounded in-memory registry of :class:`SessionState` objects.

    Thread-safe (a plain lock; operations are O(1)/O(evictions)). All public
    methods sweep expired sessions opportunistically so no background task is
    required, which keeps the store usable from the synchronous tests and from
    a stdio process alike.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sessions: "OrderedDict[str, SessionState]" = OrderedDict()
        # Recently expired/evicted ids so an agent gets "expired" (retry with a
        # new session) rather than the vaguer "unknown". Bounded FIFO.
        self._tombstones: "deque[str]" = deque(maxlen=4096)
        self._tombstone_set: set[str] = set()
        # Lifetime counters for the /metrics surface.
        self.created_total = 0
        self.expired_total = 0
        self.evicted_total = 0

    # -- helpers -------------------------------------------------------------
    def _remember_tombstone(self, session_id: str) -> None:
        if session_id in self._tombstone_set:
            return
        if len(self._tombstones) == self._tombstones.maxlen:
            old = self._tombstones[0]
            self._tombstone_set.discard(old)
        self._tombstones.append(session_id)
        self._tombstone_set.add(session_id)

    def _sweep_locked(self, now: float) -> None:
        expired = [sid for sid, s in self._sessions.items() if s.is_expired(now)]
        for sid in expired:
            del self._sessions[sid]
            self._remember_tombstone(sid)
            self.expired_total += 1

    def _make_room_locked(self, now: float) -> bool:
        """Free one slot for a new session if we are at MAX_SESSIONS. Only
        sessions idle for >= SESSION_EVICTION_MIN_IDLE_SECONDS are eligible
        (LRU first). Returns False if no slot could be freed."""
        if len(self._sessions) < MAX_SESSIONS:
            return True
        for sid, st in list(self._sessions.items()):  # LRU order
            if (now - st.last_seen) >= SESSION_EVICTION_MIN_IDLE_SECONDS:
                del self._sessions[sid]
                self._remember_tombstone(sid)
                self.evicted_total += 1
                return True
        return False

    def _enforce_bounds_locked(self) -> None:
        # Global plot-bytes cap: drop oldest plots from LRU sessions first.
        total = sum(s.plot_bytes for s in self._sessions.values())
        if total <= MAX_TOTAL_PLOT_BYTES:
            return
        for s in list(self._sessions.values()):  # LRU first
            while s.plots and total > MAX_TOTAL_PLOT_BYTES:
                total -= s.evict_oldest_plot()
            if total <= MAX_TOTAL_PLOT_BYTES:
                break

    # -- public API ----------------------------------------------------------
    def create(self, client_label: str = "") -> SessionState:
        """Mint a new session with an unguessable id and register it."""
        session_id = secrets.token_urlsafe(32)
        state = SessionState(session_id=session_id, client_label=client_label[:120])
        with self._lock:
            now = time.monotonic()
            self._sweep_locked(now)
            if not self._make_room_locked(now):
                raise SessionError("capacity", _session_not_found_message("capacity"))
            self._sessions[session_id] = state
            self.created_total += 1
            self._enforce_bounds_locked()
        return state

    def get(self, session_id: Optional[str], touch: bool = True) -> SessionState:
        """Resolve ``session_id`` to live state or raise :class:`SessionError`."""
        if not session_id or not str(session_id).strip():
            raise SessionError("missing", _session_not_found_message("missing"))
        session_id = str(session_id).strip()
        if not SESSION_ID_RE.match(session_id):
            raise SessionError("malformed", _session_not_found_message("malformed"))
        now = time.monotonic()
        with self._lock:
            state = self._sessions.get(session_id)
            if state is not None and state.is_expired(now):
                del self._sessions[session_id]
                self._remember_tombstone(session_id)
                self.expired_total += 1
                state = None
            if state is None:
                reason = "expired" if session_id in self._tombstone_set else "unknown"
                raise SessionError(reason, _session_not_found_message(reason))
            if touch:
                state.touch()
                self._sessions.move_to_end(session_id)  # LRU bookkeeping
            return state

    def get_or_create_fixed(self, session_id: str) -> SessionState:
        """Return the session with a FIXED id, creating it if absent. Used ONLY
        for the implicit stdio-mode session (``LOCAL_SESSION_ID``); never for
        a client-supplied value."""
        with self._lock:
            state = self._sessions.get(session_id)
            if state is None or state.is_expired():
                state = SessionState(session_id=session_id)
                self._sessions[session_id] = state
                self.created_total += 1
            state.touch()
            self._sessions.move_to_end(session_id)
            self._enforce_bounds_locked()
            return state

    def after_mutation(self) -> None:
        """Call after a session's plot registry grew, so the global byte cap
        is re-checked."""
        with self._lock:
            self._enforce_bounds_locked()

    def end(self, session_id: str) -> bool:
        with self._lock:
            state = self._sessions.pop(session_id, None)
            if state is not None:
                self._remember_tombstone(session_id)
                return True
            return False

    def sweep(self) -> int:
        """Drop expired sessions now; return how many were dropped."""
        with self._lock:
            before = len(self._sessions)
            self._sweep_locked(time.monotonic())
            return before - len(self._sessions)

    def clear(self) -> None:
        """Test hook: forget everything."""
        with self._lock:
            self._sessions.clear()
            self._tombstones.clear()
            self._tombstone_set.clear()

    def stats(self) -> dict:
        with self._lock:
            live = list(self._sessions.values())
            return {
                "live_sessions": len(live),
                "created_total": self.created_total,
                "expired_total": self.expired_total,
                "evicted_total": self.evicted_total,
                "retained_plots": sum(len(s.plots) for s in live),
                "retained_plot_bytes": sum(s.plot_bytes for s in live),
            }

    def __len__(self) -> int:
        with self._lock:
            return len(self._sessions)


# ---------------------------------------------------------------------------
# Request-scoped binding
# ---------------------------------------------------------------------------
# The server binds the resolved SessionState here at the top of each tool call.
# asyncio copies the context into tasks spawned with asyncio.gather /
# create_task, so the parallel Cypher fan-outs inside the specialist tools
# still see the right session.

CURRENT_SESSION: contextvars.ContextVar[Optional[SessionState]] = contextvars.ContextVar(
    "mcp_genelab_current_session", default=None
)


def bind(state: Optional[SessionState]) -> contextvars.Token:
    return CURRENT_SESSION.set(state)


def unbind(token: contextvars.Token) -> None:
    CURRENT_SESSION.reset(token)


def current() -> Optional[SessionState]:
    return CURRENT_SESSION.get()


def session_id_digest(session_id: Optional[str]) -> str:
    """Short, non-reversible label for logs/metrics. Session ids are bearer
    secrets (whoever holds one can read that session's plots), so they are
    never logged raw."""
    if not session_id:
        return "-"
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:12]
