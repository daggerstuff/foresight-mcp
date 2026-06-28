"""
Bootstrap the foresight_mcp package from .pyc bytecode.

The foresight_mcp source tree has been compiled to .pyc bytecode in
__pycache__ directories but the corresponding .py source files were removed.
This module pre-loads all necessary foresight_mcp modules from bytecode
so CLI imports resolve correctly.

Usage:
    from foresight_cli.bootstrap import ensure_loaded
    ensure_loaded()
"""

from __future__ import annotations

import importlib._bootstrap_external
import importlib.abc
import importlib.machinery
import importlib.util
import sys
import types
import warnings
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_MCP_DIR = _HERE.parent / "foresight_mcp"
_PYCACHE = _MCP_DIR / "__pycache__"
_BACKEND_PYCACHE = _MCP_DIR / "backend" / "__pycache__"
_LLM_PYCACHE = _MCP_DIR / "llm_providers" / "__pycache__"

EXCLUDED_MODULES = frozenset(
    {
        "__main__",
        "eval_harness",
        "eval",
    }
)

_loaded = False


def _discover_pyc_files() -> dict[str, Path]:
    """Map qualified module names to their .pyc file paths."""
    result: dict[str, Path] = {}

    def scan(pyc_dir: Path, prefix: str) -> None:
        if pyc_dir.exists():
            for p in sorted(pyc_dir.glob("*.cpython-313.pyc")):
                name = p.stem.split(".")[0]
                if name not in EXCLUDED_MODULES:
                    result[f"{prefix}.{name}"] = p

    scan(_PYCACHE, "foresight_mcp")
    scan(_BACKEND_PYCACHE, "foresight_mcp.backend")
    scan(_LLM_PYCACHE, "foresight_mcp.llm_providers")
    scan(_MCP_DIR / "websocket" / "__pycache__", "foresight_mcp.websocket")
    scan(_MCP_DIR / "migrations" / "__pycache__", "foresight_mcp.migrations")

    return result


class _PycLoader(importlib.abc.Loader):
    """Sourceless loader that loads from the correct .pyc path."""

    def __init__(self, fullname: str, pyc_path: Path) -> None:
        self.fullname = fullname
        self.pyc_path = pyc_path
        self._inner = importlib._bootstrap_external.SourcelessFileLoader(fullname, str(pyc_path))

    def create_module(self, spec: importlib.machinery.ModuleSpec) -> types.ModuleType | None:
        return self._inner.create_module(spec)

    def exec_module(self, module: types.ModuleType) -> None:
        self._inner.exec_module(module)


class _PycFinder(importlib.abc.MetaPathFinder):
    """Meta path finder that resolves foresight_mcp modules from .pyc files."""

    def __init__(self) -> None:
        self._pyc_map: dict[str, Path] = {}

    def update_map(self, pyc_map: dict[str, Path]) -> None:
        self._pyc_map = pyc_map

    def find_spec(
        self, fullname: str, path: object = None, target: object = None
    ) -> importlib.machinery.ModuleSpec | None:
        if fullname not in self._pyc_map:
            return None
        pyc_path = self._pyc_map[fullname]
        loader = _PycLoader(fullname, pyc_path)  # type: ignore[arg-type]
        spec = importlib.util.spec_from_loader(fullname, loader, origin=str(pyc_path))
        if spec is None:
            return None
        return spec


_finder = _PycFinder()

# Also monkeypatch sys.meta_path to allow server.py imports to resolve
_original_import = __builtins__["__import__"] if isinstance(__builtins__, dict) else __builtins__.__import__


def _bootstrap_import(name, *args, **kwargs):
    """Custom __import__ that loads .pyc files when .py is missing."""
    try:
        return _original_import(name, *args, **kwargs)
    except ModuleNotFoundError:
        pass
    # Check if we have the .pyc for this module
    mod_name = str(name)
    if mod_name in _finder._pyc_map and mod_name not in sys.modules:
        _finder.find_spec(mod_name)
        # Import again now that we've set up the spec
        return _original_import(name, *args, **kwargs)
    raise


def ensure_loaded() -> None:
    """Load foresight_mcp modules from .pyc bytecode.

    Safe to call multiple times — only runs once.
    """
    global _loaded
    if _loaded:
        return

    pyc_map = _discover_pyc_files()
    _finder.update_map(pyc_map)

    # Install the meta-path finder at position 1 (before the default path-based finder)
    if _finder not in sys.meta_path:
        sys.meta_path.insert(1, _finder)

    # Register backend package
    backend_pkg_path = _MCP_DIR / "backend"
    if backend_pkg_path.exists() and "foresight_mcp.backend" not in sys.modules:
        backend_pkg = types.ModuleType("foresight_mcp.backend")
        backend_pkg.__path__ = [str(backend_pkg_path)]
        sys.modules["foresight_mcp.backend"] = backend_pkg

    # Register subpackages (needed for relative imports in server.py)
    subpackages = {
        "foresight_mcp.backend": _MCP_DIR / "backend",
        "foresight_mcp.llm_providers": _MCP_DIR / "llm_providers",
        "foresight_mcp.websocket": _MCP_DIR / "websocket",
        "foresight_mcp.migrations": _MCP_DIR / "migrations",
    }
    for pkg_name, pkg_path in subpackages.items():
        if pkg_path.exists() and pkg_name not in sys.modules:
            ns_pkg = types.ModuleType(pkg_name)
            ns_pkg.__path__ = [str(pkg_path)]
            sys.modules[pkg_name] = ns_pkg

    # Register the foresight_mcp package itself (needed for relative imports)
    if "foresight_mcp" not in sys.modules:
        mcp_pkg = types.ModuleType("foresight_mcp")
        mcp_pkg.__path__ = [str(_MCP_DIR)]
        sys.modules["foresight_mcp"] = mcp_pkg

    # Import leaf modules first (fewest dependencies)
    leaf_order = [
        "foresight_mcp.config",
        "foresight_mcp.schema",
        "foresight_mcp.connection_pool",
        "foresight_mcp.tenant_context",
        "foresight_mcp.sql_helpers",
        "foresight_mcp.rate_limiter",
        "foresight_mcp.tenant_middleware",
        "foresight_mcp.event_bus",
        "foresight_mcp.llm_errors",
        "foresight_mcp.auth",
        "foresight_mcp.decay_model",
        "foresight_mcp.graph_store",
        "foresight_mcp.embedding_validation",
        "foresight_mcp.hybrid_retriever",
        "foresight_mcp.injection_budget",
        "foresight_mcp.rrf_tuning",
        "foresight_mcp.circuit_breaker",
        "foresight_mcp.entity_extractor",
        "foresight_mcp.memory_types",
        "foresight_mcp.memory_components",
        "foresight_mcp.enhanced_synthesizer",
        "foresight_mcp.crisis_detection",
        "foresight_mcp.block_registry",
        "foresight_mcp.context_blocks",
        "foresight_mcp.document_layer",
        "foresight_mcp.reflection_engine",
        "foresight_mcp.reflection_narrative",
        "foresight_mcp.semantic_search",
        "foresight_mcp.stream_producer",
        "foresight_mcp.phrase_triggers",
        "foresight_mcp.memory_gc",
        "foresight_mcp.narrative_cache",
        "foresight_mcp.ghost_cleanup",
        "foresight_mcp.graph_edge_decay",
        "foresight_mcp.cluster_service",
        "foresight_mcp.memory_maintenance",
        "foresight_mcp.temporal_schema",
        "foresight_mcp.temporal_service",
        "foresight_mcp.maintenance_eval",
        "foresight_mcp.crdt",
        "foresight_mcp.consumer_group",
        "foresight_mcp.sync",
        "foresight_mcp.capture",
        "foresight_mcp.clustering",
        "foresight_mcp.temporal_queries",
        "foresight_mcp.llm_client",
        "foresight_mcp.profile_synthesizer",
        "foresight_mcp.hooks",
        "foresight_mcp.memory_relationships",
        "foresight_mcp.audit",
        "foresight_mcp.subconscious",
        "foresight_mcp.narrative_cache",
        "foresight_mcp.backend.__init__",
    ]

    for mod_name in leaf_order:
        if mod_name in sys.modules:
            continue
        try:
            __import__(mod_name)
        except ModuleNotFoundError:
            # Circular dep or truly missing — skip
            pass
        except Exception:
            warnings.warn(f"Could not load {mod_name}", stacklevel=2, source=None)

    # Sync __init__ sub-module attributes to parent packages.
    # Python's import system normally does this when loading from .py files,
    # but our SourcelessFileLoader doesn't trigger the standard attribute
    # propagation. We need to copy __init__ exports to the parent package.
    for pkg in [
        "foresight_mcp.backend",
        "foresight_mcp.llm_providers",
        "foresight_mcp.websocket",
        "foresight_mcp.migrations",
        "foresight_mcp",
    ]:
        _sync_init_exports(pkg)

    _loaded = True


def _sync_init_exports(package_name: str) -> None:
    """Copy attributes from ``<package>.__init__`` to ``<package>``.

    When a sub-module ``<package>.__init__`` is loaded from .pyc bytecode,
    its attrs are not automatically propagated to the package namespace
    module (``<package>``). This function manually syncs them.
    """
    pkg = sys.modules.get(package_name)
    init_mod = sys.modules.get(f"{package_name}.__init__")
    if pkg is None or init_mod is None:
        return

    # Ensure __path__ matches
    if hasattr(pkg, "__path__") and hasattr(init_mod, "__path__"):
        if not pkg.__path__:
            pkg.__path__ = init_mod.__path__

    # Copy public symbols from __init__ to the package
    for attr_name in dir(init_mod):
        if attr_name.startswith("_"):
            continue
        if hasattr(pkg, attr_name):
            continue  # Don't overwrite
        setattr(pkg, attr_name, getattr(init_mod, attr_name))
