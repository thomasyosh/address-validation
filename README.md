# Address Search Validation

Python app with two workflows:

1. **Routine validation** — fetch `EADDRESS` and `CADDRESS` from Excel against one endpoint, then compare with the previous run.
2. **Benchmark** — fetch the same rows against multiple endpoints and compare speed and accuracy against your baseline endpoint and dataset ground truth.

## Dataset format (`data/address.xlsx`)

| Column | Purpose |
|--------|---------|
| `id` | Optional row ID |
| `EADDRESS` | English address to fetch |
| `CADDRESS` | Chinese address to fetch |
| `EASTING` | Ground-truth easting for coordinate comparison |
| `NORTHING` | Ground-truth northing for coordinate comparison |
| `BUILDING_CSUID` | Optional ground-truth building CSUID |

Each non-empty `EADDRESS` and `CADDRESS` cell becomes one API fetch for that row.

## Comparison criteria

**Recommended standard: coordinates**

- Compare API easting/northing with dataset `EASTING`/`NORTHING`
- Default match radius: **50 metres**
- If distance is greater than the radius → status is **`not_found`**
- Common alternate radius: **100 metres**

```yaml
comparison:
  criteria: coordinates
  coordinate_tolerance_meters: 50   # or 100
```

Override per command:

```powershell
python main.py accuracy --tolerance 50
python main.py accuracy --tolerance 100
python main.py validate --accuracy --tolerance 100
```

**Optional (not recommended as primary standard): `building_csuid`**

- ALS returns `GeoAddress`, which can be compared with dataset `BUILDING_CSUID` if you explicitly choose this criteria
- Most endpoints do not provide a reliable CSUID field, so coordinate distance is preferred

```powershell
python main.py accuracy --criteria building_csuid
```

## Commands

```powershell
python main.py validate
python main.py validate --compare-with-previous --accuracy
python main.py benchmark --report
python main.py accuracy
python main.py compare --with-previous
python main.py report
python main.py show-run 3
```

## Endpoint response mapping

```yaml
endpoints:
  - name: our_address_api
    response:
      coordinates_path: data.coordinates
      building_csuid_path: data.buildingCsuid
      coordinate_fields:
        easting: easting
        northing: northing
```

## Performance for large datasets (20k–50k)

Fetching is concurrent with per-endpoint rate limiting and automatic retries.

```yaml
performance:
  workers: 6                 # parallel threads
  requests_per_second: 4     # default cap per endpoint
  max_retries: 5
  retry_backoff_seconds: 1.5
  retry_status_codes: [429, 403, 408, 500, 502, 503, 504]
```

Per-endpoint override (useful for stricter public APIs):

```yaml
endpoints:
  - name: als_hk
    rate_limit:
      requests_per_second: 2
```

CLI overrides:

```powershell
python main.py benchmark --workers 8 --rps 3 --summary
python main.py validate --workers 4 --rps 2
```

Retries cover common overload / IP-block responses (`429`, `403`, `503`, etc.) with exponential backoff and `Retry-After` support.

Rough scale: 50,000 addresses × 2 columns × 3 endpoints can mean hundreds of thousands of requests — keep `requests_per_second` conservative to avoid blocks.

## Crash safety / resume

Successful fetches are written to SQLite immediately (`batch_save_size: 1`) using WAL mode.

If your PC shuts down mid-run:

```powershell
python main.py list-runs
python main.py benchmark --resume
python main.py benchmark --resume 12
python main.py validate --resume --retry-errors
```

- `--resume` continues the latest incomplete run (or a specific run ID)
- Already successful rows are skipped
- `--retry-errors` also retries previously failed fetches

Public APIs may need your company proxy. Keep the proxy URL out of git.

**Option A — environment variables (recommended):**

```powershell
$env:HTTPS_PROXY = "http://your-company-proxy:port"
$env:HTTP_PROXY = "http://your-company-proxy:port"
$env:NO_PROXY = "ase.testingaddress.com,localhost,127.0.0.1"
```

Or use `ADDRESS_VALIDATION_HTTP_PROXY` / `ADDRESS_VALIDATION_HTTPS_PROXY`.

**Option B — local file (gitignored):**

```powershell
copy config.local.example.yaml config.local.yaml
# edit config.local.yaml with your proxy URL
```

`NO_PROXY` should include the intranet host `ase.testingaddress.com` so colleague endpoint traffic does not go through the public proxy.

## Match summary table

After a benchmark run:

```powershell
python main.py benchmark --summary
python main.py summary
python main.py summary --csv results/summary.csv
```

Example output:

```text
column_name                   number   percentage
----------------------------------------------------
Number of Address              20000      100.00%
Ase testing query_debug        16126       80.63%
ALS                            15120       75.60%
Map data                       18120       90.60%
```

- **Number of Address** = total address fetches (`EADDRESS` + `CADDRESS`)
- Endpoint **number** = addresses matched within the metre tolerance
- **percentage** = matched / Number of Address

| Name | URL | Method | Notes |
|------|-----|--------|-------|
| `ase_query_debug` | `https://ase.testingaddress.com/query_debug` | POST | Colleague endpoint (intranet). Body: `{"addresses":["<address>"]}` |
| `als_hk` | `https://www.als.gov.hk/lookup?q=<address>` | GET | Returns `Easting`/`Northing` and `GeoAddress` CSUID |
| `map_gov_hk` | `https://www.map.gov.hk/gs/api/v1.0.0/locationSearch?q=<address>` | GET | Returns `x`/`y` coordinates only |

Switch comparison criteria in config or per command:

```powershell
python main.py validate --criteria coordinates --tolerance 50
python main.py validate --criteria coordinates --tolerance 100
python main.py benchmark --report --tolerance 50
# Optional only: ALS GeoAddress / BUILDING_CSUID
python main.py accuracy --criteria building_csuid
```

`map_gov_hk` does not return building CSUID, so use `building_csuid` only when you explicitly want ALS GeoAddress comparison.
