#!/usr/bin/env python3
"""
run_day1.py

Day 1 execution runner — PH Geospatial Intelligence Platform v2.2.

Ties together two Day 1 tasks:
  0–4h  Task A : Archive inspection (ADR-006 two-phase, Section 17)
  4–8h  Task B : Bronze write + Silver simplify

Usage:
    # Full Day 1 run (real .7z archive):
    python scripts/run_day1.py --archive /path/to/namria.7z

    # Synthetic mode (no archive required):
    python scripts/run_day1.py --synthetic

    # Inspection only (no Bronze write):
    python scripts/run_day1.py --archive /path/to/namria.7z --inspect-only
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import structlog

log = structlog.get_logger()


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Day 1 runner")
    p.add_argument("--archive", type=Path, default=None)
    p.add_argument("--synthetic", action="store_true")
    p.add_argument("--inspect-only", action="store_true")
    p.add_argument("--output-dir", type=Path, default=Path("data/bronze"))
    return p.parse_args()


def main() -> int:
    args = _parse_args()

    if not args.synthetic and args.archive is None:
        log.error("run_day1.missing_input", hint="Pass --archive or --synthetic")
        return 1

    from geo_service.pipeline.extract.inspector import InspectionOrchestrator
    from geo_service.pipeline.bronze.writer import write_bronze
    from geo_service.pipeline.silver.simplify import run_simplify_batch

    if args.synthetic:
        log.info("run_day1.synthetic_mode")
        profile = {
            "inspection_run_id": "synthetic-day1-local",
            "datasets": [{"dataset_name": "synthetic_boundaries", "operation_mode": "analytical"}],
            "operation_mode": "synthetic",
        }
        report_path = Path("data/day1_profile_report.json")
        report_path.parent.mkdir(parents=True, exist_ok=True)
        with open(report_path, "w") as f:
            json.dump(profile, f, indent=2)
        log.info("run_day1.synthetic_profile_written", path=str(report_path))
        return 0

    # Real archive path
    orchestrator = InspectionOrchestrator(str(args.archive))
    profile = orchestrator.run()

    report_path = Path("data/day1_profile_report.json")
    report_path.parent.mkdir(parents=True, exist_ok=True)
    with open(report_path, "w") as f:
        json.dump(profile, f, indent=2, default=str)
    log.info("run_day1.profile_written", path=str(report_path))

    if profile.get("halt_condition"):
        log.error("run_day1.halt", reason=profile.get("halt_reason"))
        return 2

    if args.inspect_only:
        log.info("run_day1.inspect_only_complete")
        return 0

    for ds in profile.get("datasets", []):
        write_bronze(ds, run_id=profile["inspection_run_id"], mode=ds["operation_mode"])

    log.info("run_day1.complete")
    return 0


if __name__ == "__main__":
    sys.exit(main())
