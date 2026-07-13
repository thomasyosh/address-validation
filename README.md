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
- A hit also counts if the ground truth appears in the **top N** ranked endpoint results (default **top 5**, not only rank 1)

```yaml
comparison:
  criteria: coordinates
  coordinate_tolerance_meters: 50   # or 100
  top_n: 5                          # or 10, 20, ...
```

Override per command:

```powershell
python main.py accuracy --tolerance 50 --top-n 5
python main.py accuracy --tolerance 100 --top-n 10
python main.py validate --accuracy --tolerance 100 --top-n 10
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
python main.py compare --previous-month
python main.py compare --with-date 2026-06-15
python main.py compare --with-month 2026-06
python main.py report
python main.py show-run 3
python main.py list-runs
```

## Where to find reports

Every `validate`, `benchmark`, and `compare` run writes a folder under `results/`:

```text
results/
  LATEST.txt                          # path to the newest report folder
  run_0003_20260710T084512Z_routine/
    README.txt                        # what each file means
    summary.txt / summary.csv         # match counts within tolerance
    mismatches.csv                    # addresses NOT within 50m (or configured tolerance)
    match_diff.csv / match_diff.txt   # vs previous run (see below)
    accuracy.json
```

Open `results/LATEST.txt` to jump to the newest report, or browse `results/run_*`.

Raw fetch data also stays in SQLite (`data/address_validation.db`) so you can re-compare later.

## What “difference” means

When your colleague asks what changed vs last run / last month / a date, the useful diff is **match status within the metre tolerance** (default **50m**):

| Change | Meaning |
|--------|---------|
| `newly_matched` | Within 50m **this** run, but **not** in the compared run |
| `lost_match` | Within 50m in the compared run, but **not** this run |

```powershell
# vs immediately previous completed run
python main.py compare --with-previous

# vs previous calendar month
python main.py compare --previous-month

# vs a specific date or month
python main.py compare --with-date 2026-06-15
python main.py compare --with-month 2026-06

# vs explicit run IDs
python main.py compare --current 12 --previous 8
```

Optional: also show raw coordinate/value changes with `--value-diff`.

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

Fetching uses **client-side threads** (`ThreadPoolExecutor`) to send HTTP requests in parallel. This is **not** server multi-threading — ASE still receives normal HTTP POST calls.

| Setting | Meaning |
|---------|---------|
| `request.fetch_mode` | `one` = one address per HTTP body; `batch` = array of up to `batch_size` |
| `request.concurrency` | `single-thread` or `multi-thread` (client threads; **not** multiprocessing) |
| `performance.workers` | Global thread-pool size for HTTP |
| `performance.sequential: true` | Force `workers: 1` for **all** endpoints |
| `endpoints[].max_workers` | Optional cap when `concurrency: multi-thread` |

### Choose an ASE fetch profile

Pick **`fetch_mode`** (what goes in the HTTP body) and **`concurrency`** (how many HTTP calls run at once):

| What you want | `fetch_mode` | `concurrency` | `batch_size` |
|---------------|--------------|---------------|--------------|
| One address per call, **parallel** | `one` | `multi-thread` | ignored |
| One address per call, **serial** | `one` | `single-thread` | ignored |
| Many addresses per call, **serial** | `batch` | `single-thread` | e.g. `50` |

```yaml
endpoints:
  - name: ase_query_debug
    request:
      address_in: json_array
      address_key: address
      fetch_mode: one              # or: batch
      concurrency: single-thread   # or: multi-thread
      batch_size: 50               # only when fetch_mode: batch
      auto_parallel_batches: false # only when batch + multi-thread
```

CLI overrides (json_array endpoints only):

```powershell
python main.py validate --fetch-mode one --concurrency multi-thread --workers 40
python main.py benchmark --fetch-mode batch --batch-size 50 --concurrency single-thread
```

`--sequential` still forces `workers: 1` globally (all endpoints). Prefer `concurrency: single-thread` on ASE when you only want ASE serial during a mixed benchmark.

### Mixed: ASE single-thread + public APIs multi-thread

Use ASE `concurrency: single-thread` — do **not** set `performance.sequential: true` (that limits all endpoints).

```yaml
performance:
  workers: 40
  sequential: false

endpoints:
  - name: ase_query_debug
    request:
      fetch_mode: batch
      concurrency: single-thread
      batch_size: 50
      auto_parallel_batches: false

  - name: als_hk
    # uses performance.workers (40)
    rate_limit:
      requests_per_second: 2

  - name: map_gov_hk
    rate_limit:
      requests_per_second: 4
```

During **benchmark**, ALS/Map.gov requests run in parallel while ASE stays at one HTTP call at a time.

Do **not** use `--sequential` for this setup (it forces `workers: 1` globally).

```yaml
performance:
  max_retries: 1
  retry_backoff_seconds: 1.0
  batch_save_size: 50
  retry_status_codes: [429, 403, 408, 500, 502, 503]
```

Per-endpoint override (ASE is high; public APIs stay low):

```yaml
endpoints:
  - name: ase_query_debug
    request:
      concurrency: single-thread
    rate_limit:
      requests_per_second: 0   # 0 = unlimited for intranet ASE
  - name: als_hk
    rate_limit:
      requests_per_second: 2
```

`--rps` overrides **every** endpoint's `rate_limit.requests_per_second`. Endpoint `rate_limit` wins over global `performance.requests_per_second`.

Intranet ASE: set `rate_limit.requests_per_second: 0` for no client throttle. Keep ALS / Map.gov caps low.

### ASE presets (copy one block)

**One-by-one, multi-thread** (when ASE server can handle parallel load):

```yaml
performance:
  workers: 40
  sequential: false

endpoints:
  - name: ase_query_debug
    request:
      fetch_mode: one
      concurrency: multi-thread
    # optional cap: max_workers: 20
```

**One-by-one, single-thread** (downsized ASE):

```yaml
endpoints:
  - name: ase_query_debug
    request:
      fetch_mode: one
      concurrency: single-thread
```

**Batch array, single-thread**:

```yaml
endpoints:
  - name: ase_query_debug
    request:
      fetch_mode: batch
      concurrency: single-thread
      batch_size: 50
      auto_parallel_batches: false
```

`auto_parallel_batches` only applies when `fetch_mode: batch` and `concurrency: multi-thread` (splits work into more parallel HTTP units).

**ASE one-by-one single-thread + public APIs multi-thread (benchmark):**

```yaml
endpoints:
  - name: ase_query_debug
    request:
      fetch_mode: one
      concurrency: single-thread

  - name: als_hk
    # uses performance.workers (40)
```

```powershell
python main.py validate --fetch-mode one --concurrency multi-thread --workers 40
python main.py benchmark --fetch-mode one --concurrency single-thread
```

Logs show `effective_batch` (actual array size per HTTP call) and `HTTP N/M` progress. Batch responses are split by the `data` bucket key per address.

## Crash safety / resume

Successful fetches are written to SQLite in small batches (`batch_save_size: 50`) using WAL mode.

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
$env:NO_PROXY = "ase.testingaddress.com,10.77.242.157,10.0.0.0/8,localhost,127.0.0.1"
```

Or use `ADDRESS_VALIDATION_HTTP_PROXY` / `ADDRESS_VALIDATION_HTTPS_PROXY`.

**Option B — local file (gitignored):**

```powershell
copy config.local.example.yaml config.local.yaml
# edit config.local.yaml with your proxy URL
```

Intranet ASE uses private IP `10.77.242.157` with `force_direct: true` so it never goes through the company proxy (that path commonly causes HTTP 504). Public APIs still use the proxy when configured.

### Slow ASE / HTTP 504 during one-by-one fetch

Global `no_retry_status_codes: [504]` skips retries for proxy timeouts. On a **slow downsized ASE**, 504 can be transient — override per endpoint:

```yaml
endpoints:
  - name: ase_query_debug
    max_workers: 1
    timeout_seconds: 120
    rate_limit:
      requests_per_second: 2
    retry:
      max_retries: 3
      retry_backoff_seconds: 2.0
      max_retry_backoff_seconds: 30.0
      retry_status_codes: [408, 500, 502, 503, 504]
      no_retry_status_codes: []
    request:
      fetch_mode: one
      timeout_seconds: 120
```

Resume failed rows: `python main.py validate --resume --retry-errors`

## Match summary table

After a benchmark run:

```powershell
python main.py benchmark --summary
python main.py summary
python main.py summary --csv results/summary.csv
```

Example output:

```text
column_name                                        number   percentage
----------------------------------------------------------------------
Number of Address                                   20000      100.00%
Number of English Address                           10000       50.00%
Number of Chinese Address                           10000       50.00%
Ase testing query_debug — English                    8500       85.00%
Ase testing query_debug — Chinese                    7626       76.26%
ALS — English                                        7560       75.60%
ALS — Chinese                                        7560       75.60%
Map data — English                                   9060       90.60%
Map data — Chinese                                   9060       90.60%
```

- **Number of Address** = total address fetches (`EADDRESS` + `CADDRESS`)
- **Number of English/Chinese Address** = count for that column type
- Endpoint rows are split by language; **percentage** = matched / that language's total

| Name | URL | Method | Notes |
|------|-----|--------|-------|
| `ase_query_debug` | `https://10.77.242.157/query_debug` | POST | Colleague intranet API (IP + `Host: ase.testingaddress.com`). Body: `{"address":["<address>"]}` |
| `als_hk` | `https://www.als.gov.hk/lookup?q=<address>` | GET | Returns `Easting`/`Northing` and `GeoAddress` CSUID |
| `map_gov_hk` | `https://www.map.gov.hk/gs/api/v1.0.0/locationSearch?q=<address>` | GET | Returns `x`/`y` coordinates only |

Switch comparison criteria in config or per command:

```powershell
python main.py validate --criteria coordinates --tolerance 50 --top-n 5
python main.py validate --criteria coordinates --tolerance 100 --top-n 10
python main.py benchmark --report --tolerance 50 --top-n 5
# Optional only: ALS GeoAddress / BUILDING_CSUID (also uses top_n ranking)
python main.py accuracy --criteria building_csuid --top-n 5
```

`map_gov_hk` does not return building CSUID, so use `building_csuid` only when you explicitly want ALS GeoAddress comparison.
