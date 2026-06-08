#!/usr/bin/env python3
"""
render/day4_choropleth_4k_render.py

Generates day4_choropleth_4k.png at exactly 3840×2160 pixels.

Projection : Robinson (central_longitude=121°E — centred on Philippines)
Classification : Jenks NaturalBreaks k=5 (mapclassify)
Output : 3840×2160 PNG  ← HALT if dimensions don't match
Quality bar : r/MapPorn — clean legend, readable labels, no chart junk

Usage:
    python day4_choropleth_4k_render.py [--input PATH] [--output PATH] [--verify]

    --input  : path or s3a:// URI to Gold GeoParquet (env: GEO_GOLD_PARQUET)
    --output : output PNG path (default: day4_choropleth_4k.png)
    --verify : check dimensions after render and exit non-zero on mismatch
    --synthetic : force synthetic data (CI preview without Gold Parquet)
"""

from __future__ import annotations

import argparse
import os
import sys
import warnings
from pathlib import Path
from typing import Optional

import geopandas as gpd
import mapclassify
import matplotlib as mpl
import matplotlib.patches as mpatches
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import structlog
from matplotlib.colors import BoundaryNorm
from PIL import Image

# ── Suppress noisy cartopy/shapely deprecation warnings ────────────────────
warnings.filterwarnings("ignore", category=UserWarning, module="cartopy")
warnings.filterwarnings("ignore", category=DeprecationWarning)

try:
    import cartopy.crs as ccrs
    import cartopy.feature as cfeature

    HAS_CARTOPY = True
except ImportError:
    HAS_CARTOPY = False

log = structlog.get_logger()

# ── Target dimensions ───────────────────────────────────────────────────────
TARGET_W = 3840
TARGET_H = 2160
DPI = 200  # → figsize = (19.2, 10.8)
FIGSIZE = (TARGET_W / DPI, TARGET_H / DPI)  # (19.2, 10.8)

# Philippine bounding box (with margin)
PH_EXTENT = [114.0, 128.5, 3.5, 23.0]  # [lon_min, lon_max, lat_min, lat_max]
ROBINSON_CENTRAL_LON = 121.0

# ── Colour scheme ───────────────────────────────────────────────────────────
# 5-class sequential: white→dark red (r/MapPorn readable on both screen and print)
JENKS_COLORS = ["#FFF5F0", "#FC9272", "#FB6A4A", "#DE2D26", "#A50F15"]
BACKGROUND_COLOR = "#1a1a2e"   # dark navy — r/MapPorn aesthetic
OCEAN_COLOR = "#0d1b2a"
LAND_EDGE_COLOR = "#444466"
TEXT_COLOR = "#E8E8F0"
GRID_COLOR = "#2a2a4a"

FONT_FAMILY = "DejaVu Sans"


# ── Synthetic PH data (CI / no-data fallback) ──────────────────────────────

def _synthetic_ph_regions() -> gpd.GeoDataFrame:
    """
    Minimal synthetic GeoDataFrame with Philippine regional centroids
    and simplified bounding boxes.  Used only when Gold Parquet unavailable.
    """
    from shapely.geometry import box

    regions = [
        ("NCR",        "National Capital Region",        14.58, 121.00, 28.3),
        ("CAR",        "Cordillera Administrative",       17.35, 121.17, 25.1),
        ("I",          "Ilocos Region",                   16.08, 120.62, 22.6),
        ("II",         "Cagayan Valley",                  17.35, 122.00, 19.8),
        ("III",        "Central Luzon",                   15.48, 120.71, 18.3),
        ("IVA",        "CALABARZON",                      14.10, 121.08, 14.7),
        ("IVB",        "MIMAROPA",                        12.83, 121.73, 20.4),
        ("V",          "Bicol Region",                    13.42, 123.41, 22.9),
        ("VI",         "Western Visayas",                 11.00, 122.53, 21.0),
        ("VII",        "Central Visayas",                 10.30, 123.90, 17.3),
        ("VIII",       "Eastern Visayas",                 11.25, 125.00, 24.6),
        ("IX",         "Zamboanga Peninsula",              8.15, 123.27, 27.2),
        ("X",          "Northern Mindanao",                8.45, 124.65, 19.3),
        ("XI",         "Davao Region",                     6.82, 125.61, 21.1),
        ("XII",        "SOCCSKSARGEN",                     6.75, 124.69, 24.8),
        ("XIII",       "Caraga",                           8.80, 125.75, 26.4),
        ("BARMM",      "Bangsamoro",                       7.70, 124.28, 29.7),
    ]

    rows = []
    for code, name, lat, lon, poverty_rate in regions:
        # Rough bounding box per region (±0.8°)
        geom = box(lon - 0.8, lat - 0.8, lon + 0.8, lat + 0.8)
        rows.append(
            {
                "region_code": code,
                "region_name": name,
                "centroid_lat": lat,
                "centroid_lon": lon,
                "poverty_rate": poverty_rate,
                "geometry": geom,
            }
        )
    gdf = gpd.GeoDataFrame(rows, crs="EPSG:4326")
    return gdf


# ── Data loader ─────────────────────────────────────────────────────────────


def load_gold_parquet(path: str) -> gpd.GeoDataFrame:
    """
    Load Gold GeoParquet from local path or s3a:// URI.
    Requires: geometry column, at least one numeric column for choropleth.
    """
    log.info("render.load_data", path=path)
    gdf = gpd.read_parquet(path)

    if gdf.empty:
        raise ValueError(f"Gold Parquet at {path!r} is empty")
    if not hasattr(gdf, "geometry") or gdf.geometry is None:
        raise ValueError("GeoDataFrame has no geometry column")

    log.info("render.data_loaded", features=len(gdf), columns=list(gdf.columns))
    return gdf


def _select_indicator(gdf: gpd.GeoDataFrame, preferred: str = "poverty_rate") -> str:
    """Select best numeric column for choropleth, preferring known indicators."""
    if preferred in gdf.columns and pd.api.types.is_numeric_dtype(gdf[preferred]):
        return preferred
    numeric_cols = [
        c for c in gdf.columns
        if pd.api.types.is_numeric_dtype(gdf[c])
        and c not in ("geometry", "date_sk", "region_sk", "indicator_sk", "vintage_sk")
    ]
    if not numeric_cols:
        raise ValueError(
            "No numeric columns found for choropleth.  Gold Parquet must have "
            "at least one numeric indicator column."
        )
    chosen = numeric_cols[0]
    log.warning(
        "render.indicator_fallback",
        preferred=preferred,
        chosen=chosen,
        available=numeric_cols,
    )
    return chosen


def _get_label_column(gdf: gpd.GeoDataFrame) -> Optional[str]:
    """Find region name column for labels."""
    candidates = ["region_name", "name", "NAME", "REGION", "NAMEPH"]
    for c in candidates:
        if c in gdf.columns:
            return c
    return None


# ── Jenks classification ────────────────────────────────────────────────────


def apply_jenks(gdf: gpd.GeoDataFrame, value_col: str, k: int = 5) -> gpd.GeoDataFrame:
    """Apply Jenks NaturalBreaks classification. Adds jenks_class and jenks_breaks."""
    values = gdf[value_col].dropna()
    if len(values) < k:
        log.warning(
            "render.jenks_fallback",
            reason=f"Only {len(values)} values < k={k}; using EqualInterval",
        )
        classifier = mapclassify.EqualInterval(values, k=min(k, len(values)))
    else:
        classifier = mapclassify.NaturalBreaks(values, k=k)

    gdf = gdf.copy()
    gdf["jenks_class"] = classifier.find_bin(gdf[value_col].fillna(0)).astype(int)
    gdf["jenks_breaks"] = str(classifier.bins.tolist())
    return gdf, classifier


# ── Render ──────────────────────────────────────────────────────────────────


def render_4k_choropleth(
    gdf: gpd.GeoDataFrame,
    value_col: str,
    output_path: Path,
    title: str = "Philippine Poverty Incidence by Region",
    attribution: str = "Data: Philippine Statistics Authority (PSA) | Projection: Robinson",
) -> Path:
    """
    Core render function.  Returns path to written PNG.
    HALT condition in caller: verify output is exactly 3840×2160.
    """
    gdf, classifier = apply_jenks(gdf, value_col, k=5)
    breaks = classifier.bins.tolist()
    n_classes = len(breaks)

    label_col = _get_label_column(gdf)

    # ── Matplotlib global style ─────────────────────────────────────────────
    mpl.rcParams.update(
        {
            "font.family": FONT_FAMILY,
            "font.size": 11,
            "text.color": TEXT_COLOR,
            "axes.facecolor": BACKGROUND_COLOR,
            "figure.facecolor": BACKGROUND_COLOR,
            "axes.edgecolor": GRID_COLOR,
        }
    )

    # ── Figure / axes ───────────────────────────────────────────────────────
    if HAS_CARTOPY:
        projection = ccrs.Robinson(central_longitude=ROBINSON_CENTRAL_LON)
        fig = plt.figure(figsize=FIGSIZE, dpi=DPI, facecolor=BACKGROUND_COLOR)
        ax = fig.add_axes([0.05, 0.08, 0.82, 0.82], projection=projection)

        ax.set_extent(PH_EXTENT, crs=ccrs.PlateCarree())
        ax.set_facecolor(OCEAN_COLOR)

        # Ocean / natural earth features
        ax.add_feature(
            cfeature.OCEAN.with_scale("50m"),
            facecolor=OCEAN_COLOR,
            edgecolor="none",
            zorder=0,
        )
        ax.add_feature(
            cfeature.LAND.with_scale("50m"),
            facecolor="#1e1e3e",
            edgecolor=LAND_EDGE_COLOR,
            linewidth=0.3,
            zorder=1,
        )
        ax.add_feature(
            cfeature.COASTLINE.with_scale("50m"),
            edgecolor=LAND_EDGE_COLOR,
            linewidth=0.4,
            zorder=2,
        )
        ax.gridlines(
            draw_labels=False,
            linewidth=0.3,
            color=GRID_COLOR,
            alpha=0.5,
            linestyle="--",
        )

        # Reproject GDF to Robinson for plotting
        gdf_proj = gdf.to_crs(projection.proj4_init) if gdf.crs else gdf

    else:
        # Fallback: plain matplotlib without cartopy
        log.warning("render.cartopy_missing", fallback="PlateCarree axes")
        fig, ax = plt.subplots(figsize=FIGSIZE, dpi=DPI)
        fig.patch.set_facecolor(BACKGROUND_COLOR)
        ax.set_facecolor(OCEAN_COLOR)
        ax.set_xlim(PH_EXTENT[0], PH_EXTENT[1])
        ax.set_ylim(PH_EXTENT[2], PH_EXTENT[3])
        ax.set_aspect("equal")
        gdf_proj = gdf

    # ── Colour map ──────────────────────────────────────────────────────────
    cmap = mpl.colors.ListedColormap(JENKS_COLORS)
    norm = BoundaryNorm(
        boundaries=[-0.5] + [i + 0.5 for i in range(n_classes)],
        ncolors=n_classes,
    )

    # ── Plot polygons ───────────────────────────────────────────────────────
    if HAS_CARTOPY:
        gdf_proj.plot(
            ax=ax,
            column="jenks_class",
            cmap=cmap,
            norm=norm,
            edgecolor="#444466",
            linewidth=0.4,
            zorder=3,
            transform=ccrs.Robinson(central_longitude=ROBINSON_CENTRAL_LON),
        )
    else:
        gdf_proj.plot(
            ax=ax,
            column="jenks_class",
            cmap=cmap,
            norm=norm,
            edgecolor="#444466",
            linewidth=0.4,
        )

    # ── Region labels ───────────────────────────────────────────────────────
    if label_col and HAS_CARTOPY:
        for _, row in gdf.iterrows():
            if row.geometry is None or row.geometry.is_empty:
                continue
            cx, cy = row.geometry.centroid.x, row.geometry.centroid.y
            label = str(row.get(label_col, ""))
            # Abbreviate long names to keep labels readable at 4K
            if len(label) > 12:
                label = label[:10] + "…"
            try:
                ax.text(
                    cx, cy, label,
                    ha="center", va="center",
                    fontsize=7,
                    color=TEXT_COLOR,
                    fontweight="normal",
                    transform=ccrs.PlateCarree(),
                    zorder=5,
                    bbox=dict(
                        boxstyle="round,pad=0.1",
                        facecolor=BACKGROUND_COLOR,
                        alpha=0.5,
                        edgecolor="none",
                    ),
                )
            except Exception:
                pass  # label placement failure is non-fatal

    # ── Title ───────────────────────────────────────────────────────────────
    fig.text(
        0.5, 0.965,
        title,
        ha="center", va="top",
        fontsize=28, fontweight="bold",
        color=TEXT_COLOR,
        transform=fig.transFigure,
    )

    # ── Subtitle / vintage ──────────────────────────────────────────────────
    vintage_label = ""
    if "vintage_year" in gdf.columns:
        years = sorted(gdf["vintage_year"].dropna().unique())
        vintage_label = f"Vintage: {years[-1]}" if years else ""
    fig.text(
        0.5, 0.94,
        vintage_label or "Philippine Statistics Authority Data",
        ha="center", va="top",
        fontsize=14,
        color="#AAAACC",
        transform=fig.transFigure,
    )

    # ── Legend ──────────────────────────────────────────────────────────────
    legend_breaks = [0.0] + breaks
    legend_patches = []
    for i in range(n_classes):
        lo = legend_breaks[i]
        hi = legend_breaks[i + 1] if i + 1 < len(legend_breaks) else breaks[-1]
        legend_patches.append(
            mpatches.Patch(
                color=JENKS_COLORS[i],
                label=f"{lo:.1f} – {hi:.1f}%",
            )
        )

    legend = ax.legend(
        handles=legend_patches,
        title=f"{value_col.replace('_', ' ').title()} (%)",
        title_fontsize=13,
        fontsize=11,
        loc="lower left",
        frameon=True,
        framealpha=0.85,
        facecolor="#0d1b2a",
        edgecolor="#444466",
        labelcolor=TEXT_COLOR,
        handlelength=1.8,
        handleheight=1.6,
        borderpad=0.8,
        labelspacing=0.6,
    )
    legend.get_title().set_color(TEXT_COLOR)
    legend.get_title().set_fontweight("bold")

    # ── Attribution footer ──────────────────────────────────────────────────
    fig.text(
        0.5, 0.015,
        attribution,
        ha="center", va="bottom",
        fontsize=9,
        color="#888899",
        transform=fig.transFigure,
    )

    # ── Compass rose (simple N arrow) ───────────────────────────────────────
    fig.text(
        0.895, 0.88,
        "N", ha="center", va="bottom",
        fontsize=16, fontweight="bold", color=TEXT_COLOR,
    )
    ax_arrow = fig.add_axes([0.888, 0.82, 0.012, 0.06])
    ax_arrow.set_facecolor("none")
    ax_arrow.axis("off")
    ax_arrow.annotate(
        "", xy=(0.5, 1.0), xytext=(0.5, 0.0),
        arrowprops=dict(arrowstyle="-|>", color=TEXT_COLOR, lw=2),
    )

    # ── Save ────────────────────────────────────────────────────────────────
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(
        output_path,
        dpi=DPI,
        format="png",
        bbox_inches=None,  # exact figsize respected — no bbox trimming
        facecolor=BACKGROUND_COLOR,
        metadata={
            "Title": title,
            "Author": "PH Geospatial Platform v2.2",
            "Software": "matplotlib + cartopy",
        },
    )
    plt.close(fig)

    log.info("render.saved", path=str(output_path))
    return output_path


# ── Dimension verification (HALT condition) ─────────────────────────────────


def verify_dimensions(path: Path) -> bool:
    """
    Check output PNG is exactly 3840×2160.
    Returns True on pass.  Raises SystemExit on mismatch (HALT condition).
    """
    with Image.open(path) as img:
        w, h = img.size

    if w == TARGET_W and h == TARGET_H:
        log.info("render.verify_pass", width=w, height=h)
        return True

    msg = (
        f"HALT: Render dimensions {w}×{h} ≠ target {TARGET_W}×{TARGET_H}. "
        f"Check DPI ({DPI}) and figsize ({FIGSIZE}). "
        "Fix before Day 5."
    )
    log.error("render.verify_fail", width=w, height=h, target_w=TARGET_W, target_h=TARGET_H)
    raise SystemExit(msg)


# ── CLI ─────────────────────────────────────────────────────────────────────


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--input", "-i",
        default=os.environ.get("GEO_GOLD_PARQUET", ""),
        help="Path or s3a:// URI to Gold GeoParquet (env: GEO_GOLD_PARQUET)",
    )
    p.add_argument(
        "--output", "-o",
        default="day4_choropleth_4k.png",
        help="Output PNG path (default: day4_choropleth_4k.png)",
    )
    p.add_argument(
        "--indicator",
        default="poverty_rate",
        help="Indicator column to visualise (default: poverty_rate)",
    )
    p.add_argument(
        "--title",
        default="Philippine Poverty Incidence by Region",
        help="Map title",
    )
    p.add_argument(
        "--verify", action="store_true",
        help="Verify output dimensions after render (exit non-zero on mismatch)",
    )
    p.add_argument(
        "--synthetic", action="store_true",
        help="Use synthetic PH data (CI preview / no Gold Parquet required)",
    )
    return p


def main() -> None:
    args = _build_parser().parse_args()
    output_path = Path(args.output)

    # ── Load data ───────────────────────────────────────────────────────────
    if args.synthetic or not args.input:
        log.info("render.using_synthetic_data")
        gdf = _synthetic_ph_regions()
        value_col = "poverty_rate"
    else:
        gdf = load_gold_parquet(args.input)
        value_col = _select_indicator(gdf, preferred=args.indicator)

    log.info(
        "render.config",
        output=str(output_path),
        indicator=value_col,
        features=len(gdf),
        dpi=DPI,
        figsize=FIGSIZE,
        target=f"{TARGET_W}×{TARGET_H}",
        cartopy=HAS_CARTOPY,
    )

    # ── Render ──────────────────────────────────────────────────────────────
    render_4k_choropleth(
        gdf=gdf,
        value_col=value_col,
        output_path=output_path,
        title=args.title,
    )

    # ── Verify dimensions ───────────────────────────────────────────────────
    if args.verify:
        verify_dimensions(output_path)
        print(f"✓ {output_path}  {TARGET_W}×{TARGET_H}px — visual review required before Day 5.")
    else:
        with Image.open(output_path) as img:
            w, h = img.size
        print(f"Rendered: {output_path}  ({w}×{h}px)")
        if w != TARGET_W or h != TARGET_H:
            print(
                f"WARNING: Expected {TARGET_W}×{TARGET_H}.  Got {w}×{h}.  "
                "HALT condition — fix DPI/figsize before Day 5.",
                file=sys.stderr,
            )
            sys.exit(1)


if __name__ == "__main__":
    main()
