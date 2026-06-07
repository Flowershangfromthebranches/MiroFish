#!/usr/bin/env python3
"""Fail if business code imports direct model or Zep SDKs."""

from __future__ import annotations

import ast
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCAN_ROOTS = [ROOT / "backend" / "app", ROOT / "backend" / "scripts"]
ALLOWED = {
    ROOT / "backend" / "app" / "adapters" / "llm" / "openai_compatible.py",
    ROOT / "backend" / "app" / "adapters" / "llm" / "camel_adapter.py",
    ROOT / "backend" / "app" / "adapters" / "graph" / "zep.py",
}
ALLOWED_LEGACY_ADAPTER_IMPORTS = {
    ROOT / "backend" / "app" / "adapters" / "llm" / "factory.py",
    ROOT / "backend" / "app" / "adapters" / "graph" / "factory.py",
}
ALLOWED_GRAPHITI_SCHEMA = {
    ROOT / "backend" / "app" / "adapters" / "graph" / "graphiti.py",
}
FORBIDDEN = {
    "openai",
    "anthropic",
    "dashscope",
    "qwen",
    "zep_cloud",
    "camel.messages",
    "camel.models",
}
FORBIDDEN_STRING_PATTERNS = tuple(
    pattern
    for module in FORBIDDEN
    for pattern in (f"import {module}", f"from {module}")
)
FORBIDDEN_LEGACY_ADAPTER_IMPORTS = {
    "app.adapters.llm.openai_compatible",
    "app.adapters.graph.zep",
    "backend.app.adapters.llm.openai_compatible",
    "backend.app.adapters.graph.zep",
}
FORBIDDEN_GRAPHITI_SCHEMA_PATTERNS = {
    "MiroFishEntity",
    "MiroFishEpisode",
    "MiroFishAgentMemory",
    "MIROFISH_FACT",
    "CREATE CONSTRAINT",
    "MERGE (",
    "MATCH (",
}


def module_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Import):
        return None
    if isinstance(node, ast.ImportFrom):
        return node.module
    return None


def imported_names(node: ast.AST) -> list[str]:
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    if isinstance(node, ast.ImportFrom):
        return [node.module or ""]
    return []


def is_forbidden(name: str) -> bool:
    return any(name == forbidden or name.startswith(f"{forbidden}.") for forbidden in FORBIDDEN)


def is_forbidden_legacy_adapter_import(name: str) -> bool:
    return any(
        name == forbidden or name.startswith(f"{forbidden}.")
        for forbidden in FORBIDDEN_LEGACY_ADAPTER_IMPORTS
    )


def display_path(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def collect_violations(
    scan_roots: list[Path],
    *,
    root: Path = ROOT,
    allowed: set[Path] = ALLOWED,
    allowed_legacy_adapter_imports: set[Path] = ALLOWED_LEGACY_ADAPTER_IMPORTS,
    allowed_graphiti_schema: set[Path] = ALLOWED_GRAPHITI_SCHEMA,
) -> list[str]:
    violations: list[str] = []
    for scan_root in scan_roots:
        for path in scan_root.rglob("*.py"):
            if "__pycache__" in path.parts or path in allowed:
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except SyntaxError as exc:
                violations.append(f"{path}: syntax error: {exc}")
                continue
            for node in ast.walk(tree):
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    for name in imported_names(node):
                        if is_forbidden(name):
                            violations.append(f"{display_path(path, root)} imports forbidden SDK module {name}")
                        if path not in allowed_legacy_adapter_imports and is_forbidden_legacy_adapter_import(name):
                            violations.append(
                                f"{display_path(path, root)} imports legacy provider adapter directly: {name}"
                            )
                elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                    for pattern in FORBIDDEN_STRING_PATTERNS:
                        if pattern in node.value:
                            violations.append(
                                f"{display_path(path, root)} contains forbidden SDK import string {pattern!r}"
                            )
                    if path not in allowed_graphiti_schema:
                        for pattern in FORBIDDEN_GRAPHITI_SCHEMA_PATTERNS:
                            if pattern in node.value:
                                violations.append(
                                    f"{display_path(path, root)} contains Graphiti/Neo4j schema assumption {pattern!r}"
                                )
    return violations


def main() -> int:
    violations = collect_violations(SCAN_ROOTS)
    if violations:
        print("Provider boundary violations found:")
        for violation in violations:
            print(f"- {violation}")
        return 1
    print("Provider boundary check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
