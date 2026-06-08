"""
tests/test_h3_aggregate.py

Unit tests for geo_service.pipeline.gold.h3_aggregate.

Run: pytest tests/test_h3_aggregate.py -v
Does not require real PSA data — uses synthetic fixture.
"""

import json

import geopandas as gpd
import pandas as pd
import pytest
from shapely.geometry import box

from geo_service.domain.exceptions import H3ResolutionError
from geo_service.pipeline.gold.h3_aggregate import (
    aggregate_to_h3,
    run_gold_h3,
    select_resolution,
)
from tests.fixtures.generate_fixture import make_fixture_gdf


# ── Fixtures ───────────────────────────────────────────────────────────────────
@pytest.fixture(scope="module")
def ph_fixture() -> gpd.GeoDataFrame:
    """82-province synthetic Philippine GeoDataFrame."""
    return make_fixture_gdf()


@pytest.fixture(scope="module")
def sparse_fixture() -> gpd.GeoDataFrame:
    """3-feature GeoDataFrame — designed to fail resolution 7 occupancy."""
    records = [
        {"poverty_rate": 12.5, "geometry": box(118.0, 5.0, 118.2, 5.2)},
        {"poverty_rate": 38.0, "geometry": box(121.0, 14.0, 121.2, 14.2)},
        {"poverty_rate": 55.0, "geometry": box(126.0, 8.0, 126.2, 8.2)},
    ]
    return gpd.GeoDataFrame(records, crs="EPSG:4326")


# ── select_resolution ──────────────────────────────────────────────────────────
class TestSelectResolution:
    def test_returns_int(self, ph_fixture):
        res = select_resolution(ph_fixture, "test_dataset")
        assert isinstance(res, int)

    def test_valid_candidate(self, ph_fixture):
        res = select_resolution(ph_fixture, "test_dataset")
        assert res in (5, 6, 7), f"Resolution {res} not in candidate set"

    def test_sparse_data_still_resolves(self, sparse_fixture):
        """3 widely-spread points: resolution 5 should pass; 7 and 6 may fail."""
        res = select_resolution(sparse_fixture, "sparse_test")
        assert res in (5, 6, 7)

    def test_empty_gdf_raises(self):
        empty = gpd.GeoDataFrame(
            {"poverty_rate": pd.Series([], dtype=float)},
            geometry=gpd.GeoSeries([], crs="EPSG:4326"),
            crs="EPSG:4326",
        )
        with pytest.raises(H3ResolutionError):
            select_resolution(empty, "empty_dataset")


# ── aggregate_to_h3 ────────────────────────────────────────────────────────────
class TestAggregateToH3:
    def test_returns_dict(self, ph_fixture):
        result = aggregate_to_h3(ph_fixture, "poverty_rate", "test", auto_select=True)
        assert isinstance(result, dict)
        assert len(result) == 1   # auto_select yields single resolution

    def test_jenks_class_column_exists(self, ph_fixture):
        result = aggregate_to_h3(ph_fixture, "poverty_rate", "test", auto_select=True)
        df = next(iter(result.values()))
        assert "jenks_class" in df.columns, "jenks_class column missing"

    def test_jenks_class_range(self, ph_fixture):
        result = aggregate_to_h3(ph_fixture, "poverty_rate", "test", auto_select=True)
        df = next(iter(result.values()))
        assert df["jenks_class"].between(0, 5).all(), "jenks_class out of 0–5 range"

    def test_jenks_breaks_valid_json(self, ph_fixture):
        result = aggregate_to_h3(ph_fixture, "poverty_rate", "test", auto_select=True)
        df = next(iter(result.values()))
        sample_breaks = df["jenks_breaks"].iloc[0]
        parsed = json.loads(sample_breaks)
        assert isinstance(parsed, list), "jenks_breaks should deserialise to list"
        assert len(parsed) >= 2, "Jenks breaks list too short"

    def test_feature_count_positive(self, ph_fixture):
        result = aggregate_to_h3(ph_fixture, "poverty_rate", "test", auto_select=True)
        df = next(iter(result.values()))
        assert (df["feature_count"] > 0).all(), "feature_count must be positive"

    def test_h3_index_column_exists(self, ph_fixture):
        result = aggregate_to_h3(ph_fixture, "poverty_rate", "test", auto_select=True)
        df = next(iter(result.values()))
        assert "h3_index" in df.columns

    def test_h3_index_valid_strings(self, ph_fixture):
        result = aggregate_to_h3(ph_fixture, "poverty_rate", "test", auto_select=True)
        df = next(iter(result.values()))
        # H3 addresses are 15-char hex strings
        assert df["h3_index"].str.len().gt(10).all(), "H3 index strings too short"

    def test_raises_on_missing_column(self, ph_fixture):
        with pytest.raises(ValueError, match="not in GeoDataFrame"):
            aggregate_to_h3(ph_fixture, "nonexistent_column", "test", auto_select=True)

    def test_raises_on_non_numeric_column(self, ph_fixture):
        gdf = ph_fixture.copy()
        gdf["text_col"] = "string"
        with pytest.raises(ValueError, match="numeric column"):
            aggregate_to_h3(gdf, "text_col", "test", auto_select=True)

    def test_multi_resolution_explicit(self, ph_fixture):
        result = aggregate_to_h3(
            ph_fixture, "poverty_rate", "test",
            resolutions=(5, 6),
            auto_select=False,
        )
        assert set(result.keys()) == {5, 6}

    def test_resolution_column_set(self, ph_fixture):
        result = aggregate_to_h3(
            ph_fixture, "poverty_rate", "test",
            resolutions=(6,),
            auto_select=False,
        )
        df = result[6]
        assert (df["resolution"] == 6).all()


# ── run_gold_h3 ────────────────────────────────────────────────────────────────
class TestRunGoldH3:
    def test_returns_tuple(self, ph_fixture):
        resolution, df = run_gold_h3(ph_fixture, "poverty_rate", "test")
        assert isinstance(resolution, int)
        assert isinstance(df, pd.DataFrame)

    def test_resolution_in_candidate_set(self, ph_fixture):
        resolution, _ = run_gold_h3(ph_fixture, "poverty_rate", "test")
        assert resolution in (5, 6, 7)

    def test_df_has_required_columns(self, ph_fixture):
        _, df = run_gold_h3(ph_fixture, "poverty_rate", "test")
        required = {"h3_index", "poverty_rate_mean", "feature_count", "jenks_class", "jenks_breaks"}
        assert required.issubset(set(df.columns)), f"Missing: {required - set(df.columns)}"
