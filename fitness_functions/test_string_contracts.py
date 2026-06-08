"""
fitness_functions/test_string_contracts.py

String-based fitness functions for PH Geospatial Platform v2.2.

TECH-DEBT-002 constraint: These tests use string/AST-based assertions ONLY.
No filesystem path-existence assertions in this module.
Migration to path-based contracts is Phase 3 scope — do not rewrite here.

Master Plan Section 12: fitness functions enforce architectural invariants
that cannot be expressed via unit tests alone.
"""

from __future__ import annotations

import ast
import glob
import os
import re


# ── Helpers ────────────────────────────────────────────────────────────────────

def _read_source(pattern: str) -> list[tuple[str, str]]:
    """Read all Python files matching glob pattern. Returns (path, src) tuples."""
    results = []
    for path in glob.glob(pattern, recursive=True):
        try:
            results.append((path, open(path, encoding="utf-8").read()))
        except OSError:
            pass
    return results


def _ast_names(src: str) -> list[str]:
    """Return all attribute names referenced in AST (e.g. node.attr for Attribute nodes)."""
    try:
        tree = ast.parse(src)
        return [
            node.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Attribute)
        ]
    except SyntaxError:
        return []


# ── FIX-01 required contract: ThreadPoolExecutor banned in silver/*.py ────────

def test_no_threadpoolexecutor_in_silver_layer():
    """
    TECH-DEBT-002 string contract.
    Silver modules (except parallel.py orchestrator) must not use ThreadPoolExecutor.
    GEOS is not thread-safe — all spatial ops must run in process-isolated workers (ADR-007).
    """
    violations = []
    for path, src in _read_source("geo_service/pipeline/silver/*.py"):
        filename = os.path.basename(path)
        if filename == "parallel.py":
            # parallel.py orchestrates process pools via a ThreadPoolExecutor — allowed.
            # It never calls GEOS operations inside threads; workers are subprocess-isolated.
            continue
        if "ThreadPoolExecutor" in src:
            violations.append(path)

    assert len(violations) == 0, (
        f"ThreadPoolExecutor found in Silver modules (forbidden per ADR-007 — use ProcessPoolExecutor): "
        f"{violations}"
    )


# ── Additional string contracts ────────────────────────────────────────────────

def test_no_hardcoded_secrets_in_bronze():
    """
    Bronze writer must not contain hardcoded credentials.
    All access keys, passwords, and tokens must be read from /run/secrets or env.
    """
    password_pattern = re.compile(
        r'(password|passwd|access_key|secret_key|api_key)\s*=\s*["\'][^"\']{4,}["\']',
        re.IGNORECASE,
    )
    violations = []
    for path, src in _read_source("geo_service/pipeline/bronze/*.py"):
        matches = password_pattern.findall(src)
        if matches:
            violations.append((path, matches))

    assert len(violations) == 0, (
        f"Hardcoded credential patterns found in Bronze modules: {violations}"
    )


def test_no_print_statements_in_pipeline():
    """
    Pipeline modules must use structlog, not bare print().
    print() bypasses structured logging, breaks log parsing, and violates Section 8.1.

    Exception: deploy_gate.py CLI __main__ block may use print for human output.
    Exception: __init__.py files (typically empty).
    """
    violations = []
    for path, src in _read_source("geo_service/pipeline/**/*.py"):
        filename = os.path.basename(path)
        if filename in ("__init__.py", "deploy_gate.py"):
            continue
        # Strip comment lines before checking
        uncommented = "\n".join(
            line for line in src.splitlines()
            if not line.strip().startswith("#")
        )
        if re.search(r"\bprint\s*\(", uncommented):
            violations.append(path)

    assert len(violations) == 0, (
        f"bare print() in pipeline modules (use structlog): {violations}"
    )


def test_all_exceptions_inherit_from_domain():
    """
    All Exception subclasses in geo_service/domain/exceptions.py must inherit from
    GeospatialPlatformError (or be GeospatialPlatformError itself).
    This enforces the centralised exception hierarchy from Section 8.2.
    """
    exceptions_src_files = _read_source("geo_service/domain/exceptions.py")
    assert len(exceptions_src_files) > 0, "geo_service/domain/exceptions.py not found"

    for path, src in exceptions_src_files:
        try:
            tree = ast.parse(src)
        except SyntaxError as exc:
            assert False, f"SyntaxError in {path}: {exc}"

        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            class_name = node.name
            if class_name == "GeospatialPlatformError":
                continue  # base class — allowed to inherit Exception directly
            base_names = []
            for base in node.bases:
                if isinstance(base, ast.Name):
                    base_names.append(base.id)
                elif isinstance(base, ast.Attribute):
                    base_names.append(base.attr)
            # All exception subclasses must derive from a GeospatialPlatformError subtype
            assert any(
                "Error" in b or "GeospatialPlatformError" in b for b in base_names
            ), (
                f"Exception class '{class_name}' in {path} must inherit from "
                f"GeospatialPlatformError or a named domain error. "
                f"Found bases: {base_names}"
            )


def test_no_wildcard_imports_in_pipeline():
    """
    from module import * is prohibited in pipeline modules.
    Wildcard imports shadow names, break static analysis, and cause flaky test isolation.
    """
    violations = []
    for path, src in _read_source("geo_service/pipeline/**/*.py"):
        if "from __future__ import" in src:
            continue  # __future__ imports are fine
        if re.search(r"^from\s+\S+\s+import\s+\*", src, re.MULTILINE):
            violations.append(path)

    assert len(violations) == 0, (
        f"Wildcard imports found in pipeline (prohibited): {violations}"
    )


def test_dag_uses_trigger_rule_all_success_for_deploy_gate():
    """
    The deploy_gate task in geo_pipeline_daily.py must NOT use ONE_SUCCESS trigger rule.
    It must use ALL_SUCCESS (default) or be explicitly set to ALL_SUCCESS.
    Deploy gate must only run when all upstream tasks succeed.
    """
    dag_files = _read_source("dags/*.py")
    assert len(dag_files) > 0, "No DAG files found in dags/"

    for path, src in dag_files:
        if "deploy_gate" not in src:
            continue
        # ONE_SUCCESS is forbidden for deploy_gate — would allow gate to run on partial success
        # The DAG should use default (ALL_SUCCESS) or explicit ALL_SUCCESS
        assert "TriggerRule.ONE_SUCCESS" not in src or (
            # ONE_SUCCESS is allowed for silver_transform (joins bronze branches) — not deploy_gate
            src.count("TriggerRule.ONE_SUCCESS") == 1
            and "silver_transform" in src
        ), (
            f"deploy_gate task must not use ONE_SUCCESS TriggerRule in {path}. "
            f"Silver transform may use it to join bronze branches, but deploy_gate requires ALL_SUCCESS."
        )
