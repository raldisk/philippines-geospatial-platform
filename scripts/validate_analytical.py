"""
Day 1 Go/No-Go Gate — .dbf Inspection Validator (ADR-006).

Asserts that at least one dataset is operation_mode='analytical'.
Exit code 0 = GO (pipeline may proceed to Bronze).
Exit code 1 = HALT (zero analytical datasets — do not proceed).

Usage:
  python validate_analytical.py                          # reads day1_profile_report.json
  python validate_analytical.py path/to/report.json     # explicit path
  python -m pytest validate_analytical.py -v            # as pytest unit test
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


# ── CLI entry point ───────────────────────────────────────────────────────────

def main(report_path: str = "day1_profile_report.json") -> bool:
    """
    Parse day1_profile_report.json and enforce Go/No-Go gate.
    Returns True on GO, False on HALT.
    Prints structured summary to stdout.
    """
    path = Path(report_path)
    if not path.exists():
        print(f"\n[ERROR] Report not found: {report_path}")
        print("Run inspector.run_inspection() first.")
        return False

    with open(path, encoding="utf-8") as f:
        report = json.load(f)

    datasets         = report.get("datasets", [])
    analytical_count = report.get("analytical_dataset_count", 0)
    total            = len(datasets)
    halt             = report.get("halt_condition", True)
    run_id           = report.get("inspection_run_id", "unknown")
    inspected_at     = report.get("inspected_at", "unknown")

    _print_header(run_id, inspected_at, total, analytical_count, halt)
    _print_dataset_table(datasets)

    if halt:
        _print_halt(report.get("halt_reason", ""))
        return False

    _print_go(datasets)
    return True


# ── pytest-compatible test function ──────────────────────────────────────────

def test_at_least_one_analytical_dataset(report_path: str = "day1_profile_report.json") -> None:
    """
    pytest hook: asserts ≥1 analytical dataset.
    Fails the test suite (and therefore CI) if inspection yields zero analytical datasets.

    Integration: add to GitHub Actions matrix as a Day 1 smoke test before any pipeline code.
    """
    path = Path(report_path)
    assert path.exists(), (
        f"day1_profile_report.json not found at {report_path}. "
        "Run inspector.run_inspection() first."
    )

    with open(path, encoding="utf-8") as f:
        report = json.load(f)

    analytical_count = report.get("analytical_dataset_count", 0)
    halt             = report.get("halt_condition", True)
    datasets         = report.get("datasets", [])

    # Assert on analytical_dataset_count — not just halt_condition (belt-and-suspenders).
    assert not halt, (
        f"Day 1 HALT: zero analytical datasets out of {len(datasets)} inspected. "
        f"halt_reason={report.get('halt_reason', '')} "
        "Cannot proceed to Bronze pipeline. Pivot to geometry-only + external CSV join."
    )

    assert analytical_count >= 1, (
        f"Expected ≥1 analytical dataset, got {analytical_count}. "
        f"Run profile_shapefile() against all .7z archives and re-inspect."
    )

    # Secondary: recommended_indicator must be set for every analytical dataset
    analytical_datasets = [d for d in datasets if d["operation_mode"] == "analytical"]
    for ds in analytical_datasets:
        assert ds.get("recommended_indicator") is not None, (
            f"Dataset '{ds['dataset_name']}' is analytical but has no recommended_indicator. "
            "Inspect numeric columns manually — all may be binary flags."
        )


# ── private display helpers ───────────────────────────────────────────────────

def _print_header(
    run_id: str, inspected_at: str,
    total: int, analytical: int, halt: bool,
) -> None:
    gate_str = "⛔ HALT" if halt else "✅ GO"
    print(f"\n{'═'*70}")
    print(f"  PH Geospatial Platform — Day 1 Go/No-Go Gate")
    print(f"{'═'*70}")
    print(f"  Run ID         : {run_id}")
    print(f"  Inspected at   : {inspected_at}")
    print(f"  Total datasets : {total}")
    print(f"  Analytical     : {analytical}")
    print(f"  Decision       : {gate_str}")
    print(f"{'─'*70}")


def _print_dataset_table(datasets: list[dict]) -> None:
    MODE_ICONS = {
        "analytical":       "✓ ANALYTICAL   ",
        "geometry_only":    "  GEOMETRY_ONLY",
        "boundary_catalog": "  BDRY_CATALOG ",
    }
    print(f"  {'MODE':<16}  {'DATASET':<36}  {'FEATURES':>8}  {'INDICATOR'}")
    print(f"  {'─'*16}  {'─'*36}  {'─'*8}  {'─'*20}")
    for ds in datasets:
        icon      = MODE_ICONS.get(ds.get("operation_mode", ""), "  UNKNOWN       ")
        name      = ds.get("dataset_name", "")[:36]
        features  = ds.get("feature_count", 0)
        indicator = ds.get("recommended_indicator") or "—"
        error     = "  [ERROR]" if ds.get("inspection_error") else ""
        print(f"  {icon}  {name:<36}  {features:>8}  {indicator}{error}")
    print(f"{'─'*70}")


def _print_halt(reason: str) -> None:
    print()
    print("  HALT CONDITION MET — Pipeline must not proceed.")
    print(f"  Reason : {reason}")
    print()
    print("  Required action (Day 1 Go/No-Go Gate):")
    print("    1. Verify all .7z archives were extracted and all .shp files passed to inspector.")
    print("    2. If shapefiles are genuinely geometry-only: pivot to geometry-only mode +")
    print("       external CSV join (attach socioeconomic CSV to admin boundaries manually).")
    print("    3. Document the pivot decision in day1_profile_report.json comments.")
    print()


def _print_go(datasets: list[dict]) -> None:
    print()
    print("  GO — Proceed to Bronze pipeline.")
    print()
    analytical = [d for d in datasets if d.get("operation_mode") == "analytical"]
    analytical.sort(key=lambda d: d.get("feature_count", 0), reverse=True)
    print("  Recommended Bronze priority order (highest feature count first):")
    for i, ds in enumerate(analytical, 1):
        print(
            f"    {i}. {ds['dataset_name']:<40}"
            f"  features={ds.get('feature_count', 0):>6}"
            f"  indicator={ds.get('recommended_indicator', '—')}"
        )
    print()


# ── entrypoint ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    report_arg = sys.argv[1] if len(sys.argv) > 1 else "day1_profile_report.json"
    ok = main(report_arg)
    sys.exit(0 if ok else 1)
