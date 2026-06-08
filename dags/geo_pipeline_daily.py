# dags/geo_pipeline_daily.py
# Section 25 — authoritative Airflow DAG
#
# Schedule: */15 * * * * (15-min polling, ADR-005)
# max_active_runs: 1 (never two pipeline runs simultaneously)
# BranchPythonOperator: analytical | geometry_only | NO_NEW_ARCHIVES
#
# Pipeline: MinioSensor → inspect → [branch] → bronze → ephemeral silver → gold → tiles → deploy_gate
#
# ADR-011: Silver is ephemeral. Temp path deleted by deploy_gate after Gold verification.
# deploy_gate enforces 46 checks. Failure raises ValueError — do not disable checks.
# HALT RULE: if deploy_gate fails, fix the underlying issue or document a waiver.
#            Do NOT comment out checks to force green.

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta
from typing import Any

from airflow import DAG
from airflow.operators.python import PythonOperator, BranchPythonOperator
from airflow.providers.amazon.aws.sensors.s3 import S3KeySensor
from airflow.utils.trigger_rule import TriggerRule
from airflow.models import Variable


# ── Failure callback ─────────────────────────────────────────────────────────
# Defined BEFORE DEFAULT_ARGS to avoid NameError at import time.

def _on_task_failure(context: dict) -> None:
    """Push pipeline failure metric to Prometheus Pushgateway (fire-and-forget)."""
    import requests
    task_id = context["task_instance"].task_id
    dag_id  = context["dag"].dag_id
    try:
        requests.post(
            f"{os.environ.get('PUSHGATEWAY_URL', 'http://pushgateway:9091')}/metrics/job/airflow",
            data=f'geo_pipeline_task_failure_total{{dag="{dag_id}",task="{task_id}"}} 1\n',
            timeout=5,
        )
    except Exception:
        pass  # Never block pipeline error handling on metric push failure


# ── Silver temp cleanup ───────────────────────────────────────────────────────
# Called by deploy_gate after Gold verification (ADR-011: Silver must not persist).
# Uses boto3 directly — no airflow hook dependency in cleanup path.

def _delete_silver_temp(silver_temp_path: str) -> None:
    """
    Delete ephemeral Silver temp object from MinIO (ADR-011).

    silver_temp_path: s3a://geo-silver-tmp/<run_id>/<dataset>.parquet
    Failure is logged but never raises — Silver temp leak is caught by
    deploy gate check G-SILVER-LEAK in the next pipeline run.
    """
    import re
    import boto3
    from botocore.exceptions import ClientError
    from geo_service.infra.secrets import read_secret

    # Parse s3a:// URI → bucket + key
    match = re.match(r"s3a://([^/]+)/(.+)", silver_temp_path)
    if not match:
        print(f"[lineage] _delete_silver_temp: invalid path {silver_temp_path!r}, skipping")
        return

    bucket, key = match.group(1), match.group(2)

    try:
        endpoint = os.environ.get("MINIO_ENDPOINT", "http://minio:9000")
        s3 = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=read_secret("MINIO_ACCESS_KEY_FILE"),
            aws_secret_access_key=read_secret("MINIO_SECRET_KEY_FILE"),
        )
        s3.delete_object(Bucket=bucket, Key=key)
        print(f"[ADR-011] Deleted ephemeral Silver temp: s3://{bucket}/{key}")
    except ClientError as exc:
        # Log but never raise — leak detection handled by next deploy gate run.
        print(f"[ADR-011] WARNING: failed to delete Silver temp {silver_temp_path}: {exc}")


# ── DAG configuration ─────────────────────────────────────────────────────────

DEFAULT_ARGS = {
    "owner": "geo-pipeline",
    "depends_on_past": False,
    "retries": 1,                        # deterministic transforms: 1 retry only
    "retry_delay": timedelta(minutes=2),
    "on_failure_callback": _on_task_failure,
    "email_on_failure": False,           # Prometheus alert handles notification
}

with DAG(
    dag_id="geo_pipeline_daily",
    default_args=DEFAULT_ARGS,
    description="Shapefile 7z → Bronze → Ephemeral Silver → Gold → PMTiles",
    schedule="*/15 * * * *",   # 15-min polling (ADR-005)
    start_date=datetime(2026, 1, 1),
    catchup=False,
    max_active_runs=1,                  # serialized: never two runs simultaneously
    tags=["geo", "pipeline", "production"],
) as dag:

    # ── Task 0: Sensor ────────────────────────────────────────────────────────
    # MinioSensor polls uploads/ prefix for new .7z archives.
    # reschedule mode: frees worker slot between poke intervals (Section 25).
    # timeout < schedule period: gives up before next trigger to avoid overlap.

    sense_new_archive = S3KeySensor(
        task_id="sense_new_7z_archive",
        bucket_name=Variable.get("GEO_UPLOAD_BUCKET", "geo-uploads"),
        bucket_key="uploads/*.7z",
        wildcard_match=True,
        aws_conn_id="minio_s3a",
        poke_interval=60,               # check every 60 seconds
        timeout=60 * 14,                # give up after 14 min (< 15-min schedule)
        mode="reschedule",
    )

    # ── Task 1: Inspection ────────────────────────────────────────────────────

    def task_inspect(**context: Any) -> dict:
        """
        Runs find_new_archives() + profile_shapefile() against discovered archives.
        XCom-pushes DatasetProfile dict and pipeline_run_id to downstream tasks.
        Returns operation_mode for BranchPythonOperator.

        BYOD: handles .7z with nested folders, multi-shp, non-geo file skipping.
        """
        from geo_service.pipeline.extract.archive import find_new_archives
        from geo_service.pipeline.extract.inspector import profile_shapefile
        from geo_service.infra.secrets import read_secret
        import asyncio

        run_id = str(uuid.uuid4())
        context["ti"].xcom_push(key="pipeline_run_id", value=run_id)

        discovered = asyncio.run(find_new_archives(
            dsn=read_secret("POSTGRES_DSN_FILE"),
            bucket=Variable.get("GEO_UPLOAD_BUCKET", "geo-uploads"),
        ))

        if not discovered:
            # No new archives — sensor false positive or all already ingested.
            return {"operation_mode": "NO_NEW_ARCHIVES"}

        # One archive per run (serialized, max_active_runs=1).
        archive = discovered[0]

        if not archive.shp_paths:
            # Archive extracted but no .shp found — log and skip.
            import structlog
            structlog.get_logger().warning(
                "dag.inspect.no_shp_in_archive",
                archive=archive.archive_path.name,
                hint="Archive may contain GeoJSON/GeoParquet; check staging dir.",
            )
            return {"operation_mode": "NO_NEW_ARCHIVES"}

        # Profile the first (or only) shapefile; archive_path for SHA-256 audit.
        shp_path = archive.shp_paths[0]
        profile = profile_shapefile(
            shp_path=str(shp_path),
            dataset_name=archive.dataset_name,
            archive_path=str(archive.archive_path),
        )

        # Store discovered archive metadata for downstream tasks
        profile_payload = profile.__dict__
        profile_payload["archive_hash"] = archive.archive_hash
        profile_payload["all_shp_paths"] = [str(p) for p in archive.shp_paths]

        context["ti"].xcom_push(key="dataset_profile", value=profile_payload)
        return {"operation_mode": profile.operation_mode}

    inspect_archive = PythonOperator(
        task_id="inspect_archive",
        python_callable=task_inspect,
    )

    # ── Task 2: Branch on operation mode ─────────────────────────────────────

    def branch_on_mode(**context: Any) -> str:
        result = context["ti"].xcom_pull(task_ids="inspect_archive")
        mode = (result or {}).get("operation_mode", "geometry_only")
        if mode == "NO_NEW_ARCHIVES":
            return "skip_no_new_archives"
        if mode == "analytical":
            return "bronze_write_analytical"
        return "bronze_write_catalog"   # geometry_only or boundary_catalog

    branch_mode = BranchPythonOperator(
        task_id="branch_operation_mode",
        python_callable=branch_on_mode,
    )

    # ── Task 3a/3b: Bronze write (mode-specific) ──────────────────────────────

    def task_bronze_analytical(**context: Any) -> None:
        from geo_service.pipeline.bronze.writer import write_bronze
        profile_dict = context["ti"].xcom_pull(task_ids="inspect_archive", key="dataset_profile")
        run_id       = context["ti"].xcom_pull(task_ids="inspect_archive", key="pipeline_run_id")
        write_bronze(profile_dict, run_id=run_id, mode="analytical")

    def task_bronze_catalog(**context: Any) -> None:
        from geo_service.pipeline.bronze.writer import write_bronze
        profile_dict = context["ti"].xcom_pull(task_ids="inspect_archive", key="dataset_profile")
        run_id       = context["ti"].xcom_pull(task_ids="inspect_archive", key="pipeline_run_id")
        write_bronze(profile_dict, run_id=run_id, mode="catalog")

    bronze_write_analytical = PythonOperator(
        task_id="bronze_write_analytical",
        python_callable=task_bronze_analytical,
    )

    bronze_write_catalog = PythonOperator(
        task_id="bronze_write_catalog",
        python_callable=task_bronze_catalog,
    )

    skip_no_new = PythonOperator(
        task_id="skip_no_new_archives",
        python_callable=lambda **_: None,
    )

    # ── Task 4: Silver (ephemeral — ADR-011) ──────────────────────────────────
    # CRS normalize → make_valid → simplify → dynamic H3 resolution selection.
    # Writes to geo-silver-tmp/<run_id>/ — MUST be deleted by deploy_gate.
    # ProcessPoolExecutor used internally (ADR-007, parallel.py from Day 4).

    def task_silver(**context: Any) -> None:
        from geo_service.pipeline.silver.parallel import run_silver_simplification
        from geo_service.pipeline.silver.crs import normalize_crs
        from geo_service.pipeline.gold.h3_resolution_selector import select_resolution
        from geo_service.config import settings
        import geopandas as gpd

        profile_dict = context["ti"].xcom_pull(task_ids="inspect_archive", key="dataset_profile")
        run_id       = context["ti"].xcom_pull(task_ids="inspect_archive", key="pipeline_run_id")

        bronze_bucket = settings.BRONZE_BUCKET
        gdf = gpd.read_parquet(f"s3a://{bronze_bucket}/{profile_dict['dataset_name']}/")
        gdf = normalize_crs(gdf, profile_dict["dataset_name"], run_id)
        gdf = run_silver_simplification(gdf)

        resolution = select_resolution(gdf, profile_dict["dataset_name"])
        context["ti"].xcom_push(key="accepted_h3_resolution", value=resolution)

        # Ephemeral Silver path — keyed by run_id to prevent cross-run collision.
        silver_bucket = settings.SILVER_TEMP_BUCKET
        temp_key = f"s3a://{silver_bucket}/{run_id}/{profile_dict['dataset_name']}.parquet"
        gdf.to_parquet(
            temp_key,
            geometry_encoding="WKB",
            write_covering_bbox=True,   # ADR-012: Hilbert bbox index
        )
        context["ti"].xcom_push(key="silver_temp_path", value=temp_key)

    silver_transform = PythonOperator(
        task_id="silver_transform",
        python_callable=task_silver,
        trigger_rule=TriggerRule.ONE_SUCCESS,   # runs after either bronze branch
    )

    # ── Task 5: Gold ──────────────────────────────────────────────────────────

    def task_gold(**context: Any) -> None:
        from geo_service.pipeline.gold.h3_aggregate import aggregate_to_h3
        from geo_service.pipeline.gold.kimball_loader import load_fact_geo_observation
        from geo_service.config import settings
        import geopandas as gpd

        profile_dict = context["ti"].xcom_pull(task_ids="inspect_archive", key="dataset_profile")
        silver_path  = context["ti"].xcom_pull(task_ids="silver_transform", key="silver_temp_path")
        resolution   = context["ti"].xcom_pull(task_ids="silver_transform", key="accepted_h3_resolution")
        run_id       = context["ti"].xcom_pull(task_ids="inspect_archive", key="pipeline_run_id")

        gdf = gpd.read_parquet(silver_path)

        gold_bucket = settings.GOLD_BUCKET
        gold_path = f"s3a://{gold_bucket}/{profile_dict['dataset_name']}/features.parquet"
        gdf.to_parquet(
            gold_path,
            geometry_encoding="WKB",
            write_covering_bbox=True,   # ADR-012
        )

        if profile_dict.get("recommended_indicator"):
            h3_results = aggregate_to_h3(
                gdf,
                profile_dict["recommended_indicator"],
                resolutions=(resolution,),
            )
            load_fact_geo_observation(gdf, h3_results, profile_dict, run_id)

        context["ti"].xcom_push(key="gold_path", value=gold_path)

    gold_generate = PythonOperator(
        task_id="gold_generate",
        python_callable=task_gold,
    )

    # ── Task 6: PMTiles ───────────────────────────────────────────────────────

    def task_pmtiles(**context: Any) -> None:
        from geo_service.pipeline.gold.pmtiles import generate_pmtiles

        profile_dict = context["ti"].xcom_pull(task_ids="inspect_archive", key="dataset_profile")
        run_id       = context["ti"].xcom_pull(task_ids="inspect_archive", key="pipeline_run_id")
        generate_pmtiles(profile_dict["dataset_name"], run_id)

    pmtiles_generate = PythonOperator(
        task_id="pmtiles_generate",
        python_callable=task_pmtiles,
    )

    # ── Task 7: Deploy Gate (46 checks) ───────────────────────────────────────
    # HALT RULE: if any check fails, the gate raises ValueError.
    # Do NOT disable checks to force green. Fix the underlying issue.
    # Explicitly waived checks must be documented in geo_service/pipeline/deploy_gate.py
    # with a justification comment and a waiver_reason field in the CheckResult.
    #
    # Gate also deletes ephemeral Silver temp after successful verification (ADR-011).

    def task_deploy_gate(**context: Any) -> None:
        from geo_service.pipeline.deploy_gate import run_all_checks

        run_id       = context["ti"].xcom_pull(task_ids="inspect_archive", key="pipeline_run_id")
        silver_path  = context["ti"].xcom_pull(task_ids="silver_transform", key="silver_temp_path")

        results = run_all_checks(run_id, silver_temp_path=silver_path)
        failed  = [r for r in results if not r.passed]

        if failed:
            # Format failure report — all failed checks listed.
            failure_report = "\n".join(
                f"  FAIL [{r.check_id}]: {r.message}" for r in failed
            )
            raise ValueError(
                f"Deploy gate failed: {len(failed)}/{len(results)} checks failed.\n"
                f"{failure_report}\n\n"
                f"FIX: resolve each failing check. "
                f"Do NOT disable checks to green-light the pipeline."
            )

        # All 46 checks passed. Safe to delete ephemeral Silver temp (ADR-011).
        if silver_path:
            _delete_silver_temp(silver_path)

        print(f"[deploy_gate] All {len(results)} checks passed. Run {run_id} promoted to Gold.")

    deploy_gate = PythonOperator(
        task_id="deploy_gate",
        python_callable=task_deploy_gate,
    )

    # ── DAG wiring ─────────────────────────────────────────────────────────────
    # sense → inspect → branch → [analytical|catalog|skip]
    #                             analytical ──┐
    #                             catalog    ──┤→ silver → gold → tiles → gate
    #                             skip (no-op, does not trigger silver)

    sense_new_archive >> inspect_archive >> branch_mode
    branch_mode >> [bronze_write_analytical, bronze_write_catalog, skip_no_new]
    bronze_write_analytical >> silver_transform
    bronze_write_catalog    >> silver_transform
    silver_transform >> gold_generate >> pmtiles_generate >> deploy_gate
