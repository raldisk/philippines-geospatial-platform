"""
Request validation (Section 23.4).
BboxParam: Pydantic model constraining to Philippine envelope (lon 116-127°, lat 4.5-22°).
validate_indicator: whitelist regex — blocks SQL injection on DuckDB query string.
"""
import re

from pydantic import BaseModel, Field, model_validator

# Philippine bounding box envelope (Section 23.4)
PH_LON_MIN = 116.0
PH_LON_MAX = 127.0
PH_LAT_MIN = 4.5
PH_LAT_MAX = 22.0

# Whitelist: lowercase letters + underscore, max 50 chars
_SAFE_INDICATOR = re.compile(r"^[a-z][a-z_]{0,49}$")


class BboxParam(BaseModel):
    """
    Validated bounding box constrained to Philippine geographic envelope.
    Rejects SSRF attempts targeting non-PH coordinates.
    """

    lon_min: float = Field(..., ge=PH_LON_MIN, le=PH_LON_MAX)
    lat_min: float = Field(..., ge=PH_LAT_MIN, le=PH_LAT_MAX)
    lon_max: float = Field(..., ge=PH_LON_MIN, le=PH_LON_MAX)
    lat_max: float = Field(..., ge=PH_LAT_MIN, le=PH_LAT_MAX)

    @model_validator(mode="after")
    def bbox_is_valid_extent(self) -> "BboxParam":
        if self.lon_max <= self.lon_min:
            raise ValueError("lon_max must be greater than lon_min")
        if self.lat_max <= self.lat_min:
            raise ValueError("lat_max must be greater than lat_min")
        return self


def parse_bbox(bbox_str: str) -> BboxParam:
    """
    Parse 'lon_min,lat_min,lon_max,lat_max' string into validated BboxParam.
    Raises ValueError on malformed input or out-of-envelope coordinates.
    """
    try:
        parts = [float(x.strip()) for x in bbox_str.split(",")]
    except (ValueError, AttributeError):
        raise ValueError("bbox must be four comma-separated floats")

    if len(parts) != 4:
        raise ValueError("bbox must have exactly four values: lon_min,lat_min,lon_max,lat_max")

    return BboxParam(
        lon_min=parts[0],
        lat_min=parts[1],
        lon_max=parts[2],
        lat_max=parts[3],
    )


def validate_indicator(indicator: str) -> str:
    """
    Whitelist-validate indicator name before interpolating into DuckDB query.
    Blocks SQL injection: only lowercase letters and underscores pass.
    """
    if not _SAFE_INDICATOR.match(indicator):
        raise ValueError(
            f"Invalid indicator {indicator!r}. "
            "Only lowercase letters and underscores are allowed (e.g. 'poverty_rate')."
        )
    return indicator
