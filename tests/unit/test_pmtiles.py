"""
tests/test_pmtiles.py

Unit tests for geo_service.pipeline.gold.pmtiles.

Run: pytest tests/test_pmtiles.py -v
Tests that can run without tippecanoe binary use mocking.
Integration tests (require tippecanoe installed) are marked @pytest.mark.integration.

Run integration tests only when tippecanoe is available:
    pytest tests/test_pmtiles.py -v -m integration
"""

import hashlib
import shutil
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from geo_service.domain.exceptions import (
    BinaryIntegrityError,
    FileSizeError,
    PMTilesVerificationError,
    TileGenerationError,
)
from geo_service.pipeline.gold.pmtiles import (
    _check_file_size,
    _verify_pmtiles,
    export_curated_to_geojson,
    generate_pmtiles,
    verify_binary_integrity,
)


# ── verify_binary_integrity ────────────────────────────────────────────────────
class TestVerifyBinaryIntegrity:
    def test_skip_when_env_set(self, monkeypatch, tmp_path):
        monkeypatch.setenv("TIPPECANOE_SKIP_SHA256", "1")
        # Should not raise even with missing binary — env bypass active
        with patch("geo_service.pipeline.gold.pmtiles.settings") as mock_settings:
            mock_settings.TIPPECANOE_SKIP_SHA256 = True
            verify_binary_integrity()   # no raise

    def test_raises_when_binary_missing(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TIPPECANOE_SKIP_SHA256", "0")
        with patch("geo_service.pipeline.gold.pmtiles.settings") as mock_settings:
            mock_settings.TIPPECANOE_SKIP_SHA256 = False
            mock_settings.TIPPECANOE_BIN = str(tmp_path / "nonexistent_binary")
            mock_settings.TIPPECANOE_SHA256 = "abc123"
            with pytest.raises(BinaryIntegrityError, match="not found"):
                verify_binary_integrity()

    def test_raises_on_hash_mismatch(self, tmp_path):
        fake_binary = tmp_path / "tippecanoe"
        fake_binary.write_bytes(b"not a real binary")
        real_sha = hashlib.sha256(b"not a real binary").hexdigest()
        wrong_sha = "0" * 64

        with patch("geo_service.pipeline.gold.pmtiles.settings") as mock_settings:
            mock_settings.TIPPECANOE_SKIP_SHA256 = False
            mock_settings.TIPPECANOE_BIN = str(fake_binary)
            mock_settings.TIPPECANOE_SHA256 = wrong_sha
            mock_settings.TIPPECANOE_VERSION = "2.67.0"
            with pytest.raises(BinaryIntegrityError, match="mismatch"):
                verify_binary_integrity()

    def test_passes_on_correct_hash(self, tmp_path):
        fake_binary = tmp_path / "tippecanoe"
        content = b"fake tippecanoe binary content"
        fake_binary.write_bytes(content)
        correct_sha = hashlib.sha256(content).hexdigest()

        with patch("geo_service.pipeline.gold.pmtiles.settings") as mock_settings:
            mock_settings.TIPPECANOE_SKIP_SHA256 = False
            mock_settings.TIPPECANOE_BIN = str(fake_binary)
            mock_settings.TIPPECANOE_SHA256 = correct_sha
            mock_settings.TIPPECANOE_VERSION = "2.67.0"
            verify_binary_integrity()   # should not raise


# ── _check_file_size ───────────────────────────────────────────────────────────
class TestCheckFileSize:
    def test_passes_under_50mb(self, tmp_path):
        f = tmp_path / "test.pmtiles"
        f.write_bytes(b"x" * 1000)   # 1 KB
        size = _check_file_size(f, "test_dataset")
        assert size == 1000

    def test_raises_at_50mb(self, tmp_path):
        f = tmp_path / "big.pmtiles"
        # Write exactly 50 MB
        f.write_bytes(b"x" * (50 * 1_000_000))
        with patch("geo_service.pipeline.gold.pmtiles.settings") as mock_settings:
            mock_settings.TILE_MAX_OUTPUT_MB = 50.0
            with pytest.raises(FileSizeError, match="50"):
                _check_file_size(f, "test_dataset")

    def test_passes_just_under_50mb(self, tmp_path):
        f = tmp_path / "ok.pmtiles"
        f.write_bytes(b"x" * (49 * 1_000_000))
        with patch("geo_service.pipeline.gold.pmtiles.settings") as mock_settings:
            mock_settings.TILE_MAX_OUTPUT_MB = 50.0
            size = _check_file_size(f, "test_dataset")
            assert size == 49_000_000


# ── _verify_pmtiles ────────────────────────────────────────────────────────────
class TestVerifyPmtiles:
    def test_warns_gracefully_when_cli_absent(self, tmp_path, caplog):
        """pmtiles CLI not installed → warn, do not raise."""
        dummy = tmp_path / "test.pmtiles"
        dummy.touch()
        with patch("shutil.which", return_value=None):
            _verify_pmtiles(dummy)   # should not raise

    def test_raises_on_nonzero_exit(self, tmp_path):
        dummy = tmp_path / "test.pmtiles"
        dummy.touch()
        fake_result = MagicMock()
        fake_result.returncode = 1
        fake_result.stdout = ""
        fake_result.stderr = "invalid header"

        with patch("shutil.which", return_value="/usr/local/bin/pmtiles"), \
             patch("subprocess.run", return_value=fake_result), \
             patch("geo_service.pipeline.gold.pmtiles.settings") as mock_settings:
            mock_settings.PMTILES_BIN = "/usr/local/bin/pmtiles"
            with pytest.raises(PMTilesVerificationError, match="FAILED"):
                _verify_pmtiles(dummy)

    def test_passes_on_zero_exit(self, tmp_path):
        dummy = tmp_path / "test.pmtiles"
        dummy.touch()
        fake_result = MagicMock()
        fake_result.returncode = 0
        fake_result.stdout = "ok"
        fake_result.stderr = ""

        with patch("shutil.which", return_value="/usr/local/bin/pmtiles"), \
             patch("subprocess.run", return_value=fake_result), \
             patch("geo_service.pipeline.gold.pmtiles.settings") as mock_settings:
            mock_settings.PMTILES_BIN = "/usr/local/bin/pmtiles"
            _verify_pmtiles(dummy)   # no raise


# ── generate_pmtiles (mocked tippecanoe) ──────────────────────────────────────
class TestGeneratePmtiles:
    def test_raises_when_geojson_missing(self, tmp_path):
        with pytest.raises(TileGenerationError, match="not found"):
            generate_pmtiles(
                dataset_name="test",
                geojson_path=tmp_path / "nonexistent.geojson",
                output_path=tmp_path / "out.pmtiles",
                skip_integrity_check=True,
            )

    def test_raises_on_tippecanoe_nonzero(self, tmp_path):
        geojson = tmp_path / "input.geojson"
        geojson.write_text('{"type":"FeatureCollection","features":[]}')

        fake_result = MagicMock()
        fake_result.returncode = 1
        fake_result.stderr = "tippecanoe error"
        fake_result.stdout = ""

        with patch("geo_service.pipeline.gold.pmtiles._run_tippecanoe",
                   return_value=fake_result):
            with pytest.raises(TileGenerationError, match="exited 1"):
                generate_pmtiles(
                    dataset_name="test",
                    geojson_path=geojson,
                    output_path=tmp_path / "out.pmtiles",
                    skip_integrity_check=True,
                )

    def test_full_pipeline_mocked(self, tmp_path):
        """End-to-end with mocked tippecanoe + pmtiles verify."""
        geojson = tmp_path / "input.geojson"
        geojson.write_text('{"type":"FeatureCollection","features":[]}')
        output = tmp_path / "out.pmtiles"

        fake_result = MagicMock()
        fake_result.returncode = 0
        fake_result.stderr = ""
        fake_result.stdout = ""

        # Write a small fake pmtiles so size check + path check pass
        def fake_run(cmd, **kwargs):
            output.write_bytes(b"x" * 1000)   # 1 KB fake pmtiles
            return fake_result

        with patch("geo_service.pipeline.gold.pmtiles._run_tippecanoe", side_effect=fake_run), \
             patch("geo_service.pipeline.gold.pmtiles._verify_pmtiles"), \
             patch("geo_service.pipeline.gold.pmtiles.settings") as mock_settings:
            mock_settings.TILE_MAX_OUTPUT_MB = 50.0
            mock_settings.TILE_MIN_ZOOM = 4
            mock_settings.TILE_MAX_ZOOM = 12
            mock_settings.TILE_MAX_BYTES = 500_000
            mock_settings.TIPPECANOE_VERSION = "2.67.0"
            result = generate_pmtiles(
                dataset_name="test",
                geojson_path=geojson,
                output_path=output,
                skip_integrity_check=True,
            )

        assert result["verified"] is True
        assert result["size_bytes"] == 1000
        assert result["dataset"] == "test"


# ── Integration (requires tippecanoe binary) ───────────────────────────────────
@pytest.mark.integration
class TestIntegration:
    """Run with: pytest tests/test_pmtiles.py -v -m integration"""

    def test_tippecanoe_binary_exists(self):
        import os
        binary = os.environ.get("TIPPECANOE_BIN", "/usr/local/bin/tippecanoe")
        assert shutil.which(binary) is not None, \
            f"tippecanoe not found at {binary}. Install per day-2-README.md § Prerequisites."

    def test_version_output(self):
        import os, subprocess
        binary = os.environ.get("TIPPECANOE_BIN", "/usr/local/bin/tippecanoe")
        result = subprocess.run([binary, "--version"], capture_output=True, text=True)
        assert result.returncode == 0
        assert "2.67" in (result.stdout + result.stderr)
