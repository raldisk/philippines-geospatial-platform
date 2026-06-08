"""
tests/locustfile.py — Day 3 load test.
Target: p95 < 200ms on all /geo/v1 endpoints under 50 concurrent users.
Day 5 target: p95 < 50ms (after PMTiles + CDN).

Run:
    locust -f tests/locustfile.py --headless \
           --host http://localhost:8002 \
           -u 50 -r 10 --run-time 60s \
           --html day3_locust_report.html

Pass gate:
    p95 < 200ms on /health/ready, /tiles, /geojson, /h3, /metadata
"""
import random

from locust import HttpUser, between, events, task

# PH regional bboxes for realistic spatial queries
_PH_BBOXES = [
    "116.0,4.5,127.0,22.0",     # full PH envelope
    "121.0,9.5,126.5,13.5",     # Visayas
    "120.0,10.0,124.0,14.0",    # Central Philippines
    "118.0,17.0,122.0,21.0",    # Northern Luzon
    "124.0,6.0,127.0,10.0",     # Mindanao east
]

_INDICATORS = ["poverty_rate", "subsistence_incidence"]
_LAYERS     = ["provincial"]
_RESOLUTIONS = [5, 6, 7]
_DATASETS   = ["psa_provincial_2023"]
_TILE_SAMPLES = [(6, 53, 30), (7, 107, 61), (6, 54, 31)]  # z/x/y within PH


class GeoAPIUser(HttpUser):
    """
    Simulates a typical dashboard consumer:
    - health probe (monitoring)
    - tile fetches (map viewport pan)
    - H3 choropleth load (indicator panel)
    - GeoJSON layer (admin boundary overlay)
    - metadata (legend initialization)
    """
    wait_time = between(0.5, 2.0)

    # ------------------------------------------------------------------
    # Health — low weight, monitors readiness under load
    # ------------------------------------------------------------------
    @task(2)
    def health_ready(self):
        with self.client.get("/health/ready", catch_response=True) as resp:
            if resp.status_code != 200:
                resp.failure(f"health/ready returned {resp.status_code}")

    # ------------------------------------------------------------------
    # Tiles — highest weight (map pan = burst of tile requests)
    # ------------------------------------------------------------------
    @task(10)
    def get_tile(self):
        z, x, y = random.choice(_TILE_SAMPLES)
        url = f"/geo/v1/tiles/{z}/{x}/{y}.mvt"
        with self.client.get(url, name="/geo/v1/tiles/:z/:x/:y.mvt",
                             catch_response=True) as resp:
            if resp.status_code not in (200, 204):
                resp.failure(f"tile {z}/{x}/{y} returned {resp.status_code}")

    # ------------------------------------------------------------------
    # H3 aggregates — choropleth indicator panel
    # ------------------------------------------------------------------
    @task(5)
    def get_h3(self):
        resolution = random.choice(_RESOLUTIONS)
        bbox       = random.choice(_PH_BBOXES)
        indicator  = random.choice(_INDICATORS)
        url = f"/geo/v1/h3/{resolution}?bbox={bbox}&indicator={indicator}"
        with self.client.get(url, name="/geo/v1/h3/:resolution",
                             catch_response=True) as resp:
            if resp.status_code != 200:
                resp.failure(f"h3/{resolution} returned {resp.status_code}")

    # ------------------------------------------------------------------
    # GeoJSON — admin boundary overlay (lower rate: heavy payload)
    # ------------------------------------------------------------------
    @task(3)
    def get_geojson(self):
        layer = random.choice(_LAYERS)
        bbox  = random.choice(_PH_BBOXES)
        url   = f"/geo/v1/geojson/{layer}?bbox={bbox}&limit=200"
        with self.client.get(url, name="/geo/v1/geojson/:layer",
                             catch_response=True) as resp:
            if resp.status_code != 200:
                resp.failure(f"geojson/{layer} returned {resp.status_code}")

    # ------------------------------------------------------------------
    # Metadata — legend init (lowest rate: once per session)
    # ------------------------------------------------------------------
    @task(1)
    def get_metadata(self):
        dataset = random.choice(_DATASETS)
        with self.client.get(f"/geo/v1/metadata/{dataset}",
                             name="/geo/v1/metadata/:dataset",
                             catch_response=True) as resp:
            if resp.status_code not in (200, 404):
                resp.failure(f"metadata/{dataset} returned {resp.status_code}")


# ------------------------------------------------------------------
# Gate assertion: fail CI if p95 >= 200ms on any endpoint
# ------------------------------------------------------------------
@events.quitting.add_listener
def assert_p95_gate(environment, **kwargs):
    threshold_ms = 200
    failed = False
    stats = environment.runner.stats.entries

    print("\n--- Day 3 p95 Gate (<200ms) ---")
    for (name, method), entry in stats.items():
        if method != "GET":
            continue
        p95 = entry.get_response_time_percentile(0.95)
        status = "PASS" if p95 < threshold_ms else "FAIL"
        print(f"  [{status}] {name:<45} p95={p95:.0f}ms")
        if p95 >= threshold_ms:
            failed = True

    if failed:
        print("\n[HALT] p95 >= 200ms on one or more endpoints. "
              "Profile DuckDB query plan before Day 4.")
        environment.process_exit_code = 1
    else:
        print("\n[PASS] All endpoints p95 < 200ms. Day 3 performance gate: OK.")
