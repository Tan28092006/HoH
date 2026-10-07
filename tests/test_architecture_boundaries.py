"""Import direction between the layers described in docs/ARCHITECTURE.md.

These rules record the dependency direction the code already has, so a change
that bends it fails here instead of surfacing later as an import cycle or as a
transport concern leaking into the service. An entry in ALLOWED_EXCEPTIONS is a
known, reviewed deviation; remove it when the code stops needing it.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

PACKAGE = "hoh_control_plane"
SOURCE = Path(__file__).resolve().parents[1] / "src" / PACKAGE

# Pure value types and helpers with no internal imports.
DOMAIN = {"domain", "scopes", "contracts"}
STORAGE = {"store", "store_schema", "store_migrations", "postgres_store", "store_factory"}
ADAPTERS = {
    "git_worktrees", "qa_runner", "managed_process", "resource_lock",
    "resource_probe", "capability_probe", "pytest_probe", "observability",
}
ORCHESTRATION = {
    "event_wait", "supervisor", "scheduler", "agent_wakeup", "planner_wakeup",
    "managed_worker", "integration_worker", "qa_worker", "worktree_worker",
    "candidate_submission_worker", "seed", "operations",
}
TRANSPORTS = {
    "mcp_server", "api", "__main__", "auth",
    "mcp", "mcp.errors", "mcp.invoke", "mcp.workers", "mcp.server", "mcp.http_app",
    "mcp.tools", "mcp.tools.common", "mcp.tools.planner", "mcp.tools.developer",
    "mcp.tools.qa", "mcp.tools.integrator",
}
CLIENT_SETUP = {
    "agent_kit", "local_agent", "local_credentials", "desktop_control",
    "profile_migration", "agent_assets.install",
}

# Layer -> modules it must never import.
FORBIDDEN = {
    "domain": STORAGE | ADAPTERS | ORCHESTRATION | TRANSPORTS | CLIENT_SETUP | {"service"},
    "storage": ADAPTERS | ORCHESTRATION | TRANSPORTS | CLIENT_SETUP | {"service"},
    "adapters": STORAGE | ORCHESTRATION | TRANSPORTS | CLIENT_SETUP | {"service"},
    "service": ORCHESTRATION | TRANSPORTS | CLIENT_SETUP,
}

ALLOWED_EXCEPTIONS = {
    # The store wakes local waiters after a commit that touched audit or job
    # tables. The UDP hint belongs in a commit hook owned by event_wait.
    ("store", "event_wait"),
    # Agent discovery reads the shared wake-up vocabulary.
    ("service.discovery", "agent_wakeup"),
    # Barrier age is computed in the wait loop; it belongs with the barrier in
    # the service, which would remove this lazy import.
    ("service.repository_reconciliation", "event_wait"),
}


def _layer(module: str) -> str | None:
    if module in DOMAIN:
        return "domain"
    if module in STORAGE:
        return "storage"
    if module in ADAPTERS:
        return "adapters"
    if module == "service" or module.startswith("service."):
        return "service"
    return None


def _module_name(path: Path) -> str:
    relative = path.relative_to(SOURCE).with_suffix("")
    parts = list(relative.parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _internal_imports(path: Path) -> set[str]:
    module = _module_name(path)
    package_parts = module.split(".") if path.name == "__init__.py" else module.split(".")[:-1]
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package_parts[: len(package_parts) - node.level + 1]
                target = ".".join(base + ([node.module] if node.module else []))
                names = [f"{PACKAGE}.{target}" if target else PACKAGE]
            else:
                names = [node.module or ""]
            # `from hoh_control_plane import scopes` imports a submodule.
            if names == [PACKAGE]:
                names = [f"{PACKAGE}.{alias.name}" for alias in node.names]
        else:
            continue
        for name in names:
            if name.startswith(PACKAGE + "."):
                found.add(name[len(PACKAGE) + 1:])
    return found


def _top(module: str) -> str:
    return "service" if module.startswith("service") else module


MODULES = sorted(SOURCE.rglob("*.py"))


@pytest.mark.parametrize("path", MODULES, ids=lambda p: _module_name(p) or "__init__")
def test_imports_follow_layer_direction(path: Path) -> None:
    module = _module_name(path)
    layer = _layer(module)
    if layer is None:
        return
    violations = sorted(
        imported for imported in _internal_imports(path)
        if (_top(imported) in FORBIDDEN[layer] or imported in FORBIDDEN[layer])
        and (module, imported) not in ALLOWED_EXCEPTIONS
    )
    assert not violations, (
        f"{module} ({layer}) imports {violations}; see the dependency direction in "
        "docs/ARCHITECTURE.md"
    )


def test_every_module_has_a_layer() -> None:
    known = DOMAIN | STORAGE | ADAPTERS | ORCHESTRATION | TRANSPORTS | CLIENT_SETUP
    unplaced = sorted(
        module for module in (_module_name(path) for path in MODULES)
        if module and module not in known and _layer(module) is None
    )
    assert not unplaced, (
        f"Place {unplaced} in a layer here and in docs/ARCHITECTURE.md"
    )


def test_service_mixins_import_only_the_shared_kernel() -> None:
    # Mixins already reach each other through `self`; a direct import between
    # two of them would add a second, hidden path. integration -> qa predates
    # this rule.
    allowed = {("service.integration", "service.qa")}
    violations = []
    for path in sorted((SOURCE / "service").glob("*.py")):
        module = _module_name(path)
        if module == "service":
            continue
        for imported in _internal_imports(path):
            if imported.startswith("service.") and imported != "service._common" \
                    and (module, imported) not in allowed:
                violations.append((module, imported))
    assert not violations, violations


def test_every_write_takes_the_write_lock() -> None:
    # Guards are Python checks followed by unguarded UPDATEs; they hold only
    # because every writer is serialized by connection(immediate=True). A write
    # through a plain connection escapes the reconciliation boundary and, on
    # PostgreSQL, can commit an audit sequence below one a cursor already read.
    import re
    violations = []
    for path in sorted((SOURCE / "service").glob("*.py")) + [SOURCE / "event_wait.py"]:
        lines = path.read_text(encoding="utf-8").splitlines()
        for index, line in enumerate(lines):
            match = re.match(r"(\s*)with (self|service)\.store\.connection\(\) as \w+:", line)
            if not match:
                continue
            indent = len(match.group(1))
            cursor = index + 1
            while cursor < len(lines) and (
                not lines[cursor].strip()
                or len(lines[cursor]) - len(lines[cursor].lstrip()) > indent
            ):
                if re.search(r"\b(INSERT|UPDATE|DELETE)\b", lines[cursor]) \
                        or "self._event(" in lines[cursor]:
                    violations.append(f"{_module_name(path)}:{index + 1}")
                    break
                cursor += 1
    assert not violations, f"write inside a non-immediate connection: {violations}"
