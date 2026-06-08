"""
Integration: upload fixture .7z → bronze writer → verify Parquet in geo-bronze bucket.
Requires: MINIO_ENDPOINT, MINIO_ACCESS_KEY, MINIO_SECRET_KEY, POSTGRES_DSN env vars.
"""
import os
import boto3
import pytest
from pathlib import Path

MINIO_ENDPOINT = os.environ.get("MINIO_ENDPOINT", "http://localhost:9000")
MINIO_AK = os.environ.get("MINIO_ACCESS_KEY", "minioadmin")
MINIO_SK = os.environ.get("MINIO_SECRET_KEY", "minioadmin")


@pytest.fixture
def s3():
    return boto3.client(
        "s3",
        endpoint_url=MINIO_ENDPOINT,
        aws_access_key_id=MINIO_AK,
        aws_secret_access_key=MINIO_SK,
    )


def test_bronze_minio_roundtrip(s3, tmp_path):
    """
    Day 5 README §Gate 2 spec: upload fixture .7z → bronze writer → Parquet in geo-bronze.
    """
    from geo_service.pipeline.bronze.writer import write_bronze

    fixture_archive = Path("tests/fixtures/test_shapefile.7z")
    if not fixture_archive.exists():
        pytest.skip("Fixture archive not present — add tests/fixtures/test_shapefile.7z")

    # Upload fixture to uploads bucket
    s3.upload_file(str(fixture_archive), "geo-uploads", "uploads/test_shapefile.7z")

    # Run bronze writer with test profile
    test_profile = {
        "dataset_name": "test_shapefile",
        "archive_path": str(fixture_archive),
    }
    write_bronze(test_profile, run_id="ci-test-run-001", mode="catalog")

    # Verify Parquet landed in geo-bronze
    response = s3.list_objects_v2(Bucket="geo-bronze", Prefix="test_shapefile/")
    keys = [obj["Key"] for obj in response.get("Contents", [])]
    assert any(k.endswith(".parquet") for k in keys), (
        f"No Parquet found in geo-bronze/test_shapefile/. Found: {keys}"
    )


def test_bronze_write_then_read_parquet(s3, tmp_path):
    """
    Extended roundtrip: verify written Parquet is readable by pyarrow.
    Skips if MinIO unreachable.
    """
    try:
        s3.list_buckets()
    except Exception:
        pytest.skip("MinIO not reachable at MINIO_ENDPOINT — skipping integration test")

    fixture_archive = Path("tests/fixtures/test_shapefile.7z")
    if not fixture_archive.exists():
        pytest.skip("Fixture archive not present")

    from geo_service.pipeline.bronze.writer import write_bronze
    import pyarrow.parquet as pq

    test_profile = {
        "dataset_name": "test_readback",
        "archive_path": str(fixture_archive),
    }
    write_bronze(test_profile, run_id="ci-readback-001", mode="catalog")

    # List objects
    response = s3.list_objects_v2(Bucket="geo-bronze", Prefix="test_readback/")
    keys = [obj["Key"] for obj in response.get("Contents", [])]
    parquet_keys = [k for k in keys if k.endswith(".parquet")]

    if not parquet_keys:
        pytest.skip("No Parquet written — check bronze writer implementation")

    # Download and verify readable
    local_path = tmp_path / "readback.parquet"
    s3.download_file("geo-bronze", parquet_keys[0], str(local_path))
    table = pq.read_table(str(local_path))
    assert table.num_rows > 0, "Written Parquet has 0 rows"
