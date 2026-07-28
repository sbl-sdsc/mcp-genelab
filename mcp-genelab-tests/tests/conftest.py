"""Shared pytest fixtures and fake-Neo4j infrastructure for the mcp-genelab
server test suite.

The whole suite is offline — no real Neo4j instance, no network calls, no MCP
transport. We instantiate the FastMCP server in-process, swap in a controllable
fake driver, and exercise tools via mcp.list_tools() / mcp.call_tool().

Fixtures:
  driver       — module-scoped FakeDriver; tests can override its route
                 function and inspect the Cypher queries that were actually
                 issued via `driver.calls`.
  mcp_server   — module-scoped FastMCP instance built with the fake driver.
  tools_list   — module-scoped result of mcp_server.list_tools(); used by
                 the metadata-only tests so we don't pay the construction
                 cost on every test.

Helper:
  text_from(result) — flatten an MCP tool result (TextContent + ImageContent
                      blocks) into a single string for substring assertions.
"""
from __future__ import annotations

import asyncio
import importlib
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any, Callable, List

import pytest


# --- Make the server module importable -------------------------------------
# server.py does `from . import __version__`, so it must be loaded as part of
# a package. We pin the package directory at the repo root by walking up from
# this conftest.py. The expected layout is:
#
#     <repo-root>/
#       <PACKAGE_DIR>/__init__.py    (provides __version__)
#       <PACKAGE_DIR>/server.py
#       tests/conftest.py
#       tests/test_*.py
#
#   OR the equivalent src/ layout:
#
#     <repo-root>/
#       src/<PACKAGE_DIR>/__init__.py
#       src/<PACKAGE_DIR>/server.py
#       tests/conftest.py
#       tests/test_*.py
#
# The packaging name is detected automatically — we look for any directory
# containing both server.py and __init__.py, first at the repo root and
# then under repo_root/src/.

def _find_package_dir() -> Path:
    here = Path(__file__).resolve()
    repo_root = here.parent.parent

    def _scan(parent: Path) -> Path | None:
        if not parent.is_dir():
            return None
        for candidate in parent.iterdir():
            if candidate.is_dir() \
                    and (candidate / "server.py").exists() \
                    and (candidate / "__init__.py").exists():
                return candidate
        return None

    # Layout 1/2: package adjacent to the tests dir (flat or src/).
    pkg = _scan(repo_root) or _scan(repo_root / "src")
    if pkg is not None:
        return pkg

    # Layout 3: the tests live in their own subdirectory (mcp-genelab-tests/)
    # while the package is at the ACTUAL repo root one level up, in src/. This
    # is the real repository layout. Walk up a couple of levels looking for
    # <ancestor>/src/<pkg>/server.py or <ancestor>/<pkg>/server.py.
    for ancestor in (repo_root.parent, repo_root.parent.parent):
        pkg = _scan(ancestor) or _scan(ancestor / "src")
        if pkg is not None:
            return pkg

    # Layout 4 (fallback): the package is pip-installed (e.g. CI does
    # `pip install -r requirements-test.txt` which pulls in mcp-genelab, or a
    # developer ran `pip install -e .`). Resolve it via import machinery.
    try:
        spec = importlib.util.find_spec("mcp_genelab")
        if spec and spec.origin:
            return Path(spec.origin).resolve().parent
    except Exception:
        pass

    raise RuntimeError(
        f"Could not locate the package containing server.py near {repo_root} "
        f"(checked flat, src/, and up to two parent levels), nor as an "
        f"installed 'mcp_genelab' package. Either run the suite from within "
        f"the repo checkout, or `pip install -e .` the package first."
    )


_PKG_DIR = _find_package_dir()
# `_REPO_ROOT` here is the directory we add to sys.path so `import <pkg>`
# works. For flat layouts that's the repo root; for src layouts that's
# repo_root/src. In either case it's the parent of the package directory.
_REPO_ROOT = _PKG_DIR.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# Import the server module dynamically so the package path resolution above
# is applied. We cache it in `_SERVER_MODULE` for the fixtures.
import importlib
_SERVER_MODULE = importlib.import_module(f"{_PKG_DIR.name}.server")


# --- Fake Neo4j driver ------------------------------------------------------
# The real driver is async; we replicate the minimum surface area the server
# actually uses: driver.session(database=..., default_access_mode=...) returns
# an async context manager exposing execute_read(fn, query, params).

class _FakeRecord:
    """Mimics neo4j.Record's .data() method."""
    def __init__(self, d: dict[str, Any]) -> None:
        self._d = d
    def data(self) -> dict[str, Any]:
        return self._d


class _FakeEager:
    def __init__(self, records: List[_FakeRecord]) -> None:
        self.records = records


class _FakeRawResult:
    def __init__(self, records: List[_FakeRecord]) -> None:
        self._records = records
    async def to_eager_result(self) -> _FakeEager:
        return _FakeEager(self._records)
    def __aiter__(self):
        # Support `async for record in result` — used by _read_with_count's
        # streaming/row-capping path in the real server.
        self._iter = iter(self._records)
        return self
    async def __anext__(self) -> _FakeRecord:
        try:
            return next(self._iter)
        except StopIteration:
            raise StopAsyncIteration


class _FakeTx:
    """A fake transaction. Routes every query through the driver's `route`
    callable and records the call so tests can inspect what Cypher ran."""
    def __init__(self, route: Callable[[str, dict], List[dict]],
                 call_log: list) -> None:
        self.route = route
        self.call_log = call_log

    async def run(self, query: str, params: dict[str, Any] = None,
                  timeout: float = None, **kwargs) -> _FakeRawResult:
        # Accept (and ignore) timeout / any future kwargs the real driver's
        # tx.run supports, so the server can pass timeout=QUERY_TIMEOUT_SECONDS.
        self.call_log.append((query, params or {}))
        rows = self.route(query, params or {})
        return _FakeRawResult([_FakeRecord(r) for r in rows])


class _FakeSession:
    def __init__(self, route: Callable[[str, dict], List[dict]],
                 call_log: list) -> None:
        self.route = route
        self.call_log = call_log

    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False

    async def execute_read(self, fn, *args, **kwargs) -> Any:
        tx = _FakeTx(self.route, self.call_log)
        return await fn(tx, *args, **kwargs)


class FakeDriver:
    """A controllable async Neo4j driver substitute.

    Tests set `driver.route` to a callable mapping (query_str, params_dict) ->
    list[dict] of row data. Every Cypher invocation is recorded in
    `driver.calls` so tests can inspect what queries ran.

    Conventions used in route functions across the test suite:
      - Match queries by substring (e.g. 'log2fc > 0' for the DEG up branch).
      - Return [] for unmatched queries; the tools handle empty results
        gracefully.
      - For count queries, return [{"total": N}].
    """
    def __init__(self) -> None:
        self.route: Callable[[str, dict], List[dict]] = lambda q, p: []
        self.calls: list[tuple[str, dict]] = []

    def reset(self) -> None:
        """Clear the call log. Tests should call this at the start of each
        scenario so assertions on driver.calls aren't polluted by earlier
        fixture setup."""
        self.calls.clear()

    def set_route(self, fn: Callable[[str, dict], List[dict]]) -> None:
        self.route = fn

    def session(self, database=None, default_access_mode=None) -> _FakeSession:
        return _FakeSession(self.route, self.calls)


# --- Fixtures ---------------------------------------------------------------

@pytest.fixture(scope="session")
def server_module():
    """The imported server module (`<package>.server`). Use this when a test
    needs to inspect source code (e.g. checking DEFAULT_INSTRUCTIONS in
    async_main) rather than tool behavior."""
    return _SERVER_MODULE


@pytest.fixture
def driver() -> FakeDriver:
    """A fresh FakeDriver per test. Per-test scope (rather than session)
    because tests routinely set `route` and inspect `calls`."""
    return FakeDriver()


@pytest.fixture
def mcp_server(driver: FakeDriver):
    """A FastMCP server wired to the per-test fake driver. We pass an empty
    instructions string here so individual tests can read the real
    DEFAULT_INSTRUCTIONS from the module source separately if they need to."""
    return _SERVER_MODULE.create_mcp_server(
        driver, database="testdb", instructions=""
    )


@pytest.fixture
def tools_list(mcp_server) -> list:
    """The list of registered Tool objects as an MCP client would see them
    via tools/list. Synchronously materializes the async list_tools() call."""
    return asyncio.run(mcp_server.list_tools())


# --- Helpers ---------------------------------------------------------------

def text_from(result) -> str:
    """Flatten an MCP tool-call result into a single text string for
    substring assertions. Handles both the modern (content, structured) tuple
    return and the legacy plain-list return."""
    content = result[0] if isinstance(result, tuple) else result
    parts = []
    for c in content:
        if hasattr(c, "text") and c.text is not None:
            parts.append(c.text)
    return "\n".join(parts)


def call_tool_sync(mcp_server, name: str, args: dict) -> str:
    """Convenience: invoke a tool and return its flattened text output.

    Centralizing this here means tests don't repeat the asyncio.run + text_from
    boilerplate, and any future change to the MCP result shape only touches
    one place."""
    result = asyncio.run(mcp_server.call_tool(name, args))
    return text_from(result)
