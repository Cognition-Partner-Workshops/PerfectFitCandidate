# Phase 1f: NFR-01 performance

## Targets (exact source text)

**REQUIREMENTS_BASELINE.md, NFR-01:**
> Performance: full re-match of 500 demands × 500 resources completes within a few minutes on target hardware (embeddings cached; LLM used only for JD/CV extraction and drafting, not per pair). Dashboard interactions < 2 s.

**TDD.md §15:**
> Target hardware: Windows 11 laptop, 16 GB RAM, modern 8-core x64 CPU, SSD, CPU-only inference (REQ-LLM-01). Volume: 500 demands × 500 resources (NFR-01).

| Operation (verbatim TDD row) | Budget (target) | Basis |
|---|---|---|
| **Full re-match 500 × 500** (vectors cached) | **≤ 2 min**, target < 60 s | ~94k grade-compatible pairs (±1 of nine grades); NumPy item × chunk similarity; no LLM |
| Dashboard / grid interactions | < 2 s (target < 500 ms API) | pre-aggregated run summaries, indexed queries, server paging |

**PHASES.md, P1-EX-06:**
> 500 × 500 full re-match within the NFR-01 target on the Phase 0 laptop; dashboard interactions < 2 s.

## Method

`app/tests/perf/test_nfr01.py` uses seed **19401**, a frozen application clock,
and direct SQL into the migrated 0001 tables: 500 open demands with confirmed
revisions (two mandatory and one preferred skill), and 500 active CT resources.
All nine grades, four locations, eight accounts, four BUs, ten skills and several
statuses are represented. There are 400 eligible and 100 Selected/excluded
resources, unique candidate identities, and Y/N/blank capacity values.

The real app, authentication, SQLite, job runner, domain scoring and persistence
run unchanged. In-module deterministic **8-dimensional** fake embeddings and an
in-memory vector store avoid model/network dependencies. Vectors are primed
before timing; the test asserts no new embedding calls during matching.
A one-demand submission warms the matching path. `perf_counter` measures one
full-scope HTTP submission through terminal job completion, including snapshot
and persistence; completion polling adds up to about 100 ms.

Each analytics endpoint uses the resulting full run via `runId`, with no entity
filters: one warm-up GET followed by **three** sequential timed TestClient GETs
(including response serialization). Reported latency is the median; every
response and population size is checked. No browser rendering is measured.

## Measurements — Linux CI/VM, not target laptop

Seconds; analytics are medians of three warm requests. Every run scores **60,455**
grade-compatible pairs from the same 500 × 500 population.

| Operation | VM before | VM after | GitHub before | GitHub after |
|---|---:|---:|---:|---:|
| Full matching | 23.302 | 25.070 | 45.008 | 43.365 |
| GET /analytics/summary | 4.631 | 0.079 | 13.933 | 0.118 |
| GET /analytics/demand | 4.789 | 0.054 | 9.498 | 0.076 |
| GET /analytics/bench | 4.949 | 0.075 | 7.812 | 0.114 |
| GET /analytics/match | 6.598 | 0.557 | 15.503 | 0.810 |

Provenance (all counts are logical CPUs; RAM is OS-reported):
- **VM before/after:** Linux-6.8.0-1060-aws-x86_64-with-glibc2.35,
  Python 3.12.16, Intel(R) Xeon(R) Processor, 8 CPUs,
  33,655,369,728 bytes RAM (~31.34 GiB). Before: SHA
  `8ec55a7080dfe213b451b15c0ef3e3b422091f96`, benchmark files uncommitted.
  After: SHA `26dc675c72955858c2eaee70241a1654b4188cf9`, with the
  tighter perf guards and documentation uncommitted at measurement.
- **GitHub before:** PR #29 backend job `127749979209`, SHA
  `770320c0ca91cd28875a52c47107f375a68a342a`, clean tree;
  Ubuntu 26.04.1, Linux-7.0.0-1019-azure-x86_64-with-glibc2.43,
  Python 3.13.18, AMD EPYC 7763 64-Core Processor, 4 CPUs,
  16,759,439,360 bytes RAM (~15.61 GiB).
- **GitHub after:** push backend job `127760671435`, SHA
  `3ea2348f0e3e535a0f702143be1510a35c6446f3`, clean tree;
  Ubuntu 26.04.1, same Linux/glibc/Python versions as GitHub before,
  AMD EPYC 7763 64-Core Processor, 4 CPUs, 16,759,439,360 bytes RAM.
  The reported CPU class matches before, but hosted load is not controlled.

No cgroup CPU/memory limits were reported. Raw samples, hardware, Python, current
Git SHA/dirty state, counts and thresholds print as `NFR01_RESULT=` JSON.
Generated data/results are not committed.

**Profiling and equivalence:** 500 × 500 cProfile attributed 3.27 s to demand
loading (a repeated scan of 60k results per demand), 2.14 s to full result loading
and 2.61 s to match validation; the match profile executed 183,874 SQL statements.
Lazy request-local projections, grouped SQL counts, batched JD/CV/status/override
reads and joined validation remove these repeated operations. EXPLAIN also showed
validation choosing the resource index; using the demand index improves locality.
No cross-request cache or invalidation machinery was added.

Before production changes, exact JSON goldens were committed for nine filter
combinations on both the existing fixture and a seeded 20 × 20 real match run
with overrides and exclusions, under Analyst and Admin sessions. A separate,
uncommitted 500 × 500 capture compares all four endpoints with latest and explicit
run selection: **8/8 complete JSON responses identical** before and after.

**Interpretation:** all measured APIs now meet 2 s on GitHub; summary, demand and
bench also meet the 500 ms aspiration. Match still exceeds 500 ms. Matching remains
below 120 s. Fake cached vectors, CT-only bases, different hosted CPUs and absent
browser timings cannot establish target-laptop acceptance: benchmark the Windows
11 Phase 0 laptop with real cached embeddings/CVs before sign-off.

## Regression limits and rerunning

Limits: **180 s** full matching (unchanged), and **2 s each** for summary, demand,
bench and match medians. Each analytics limit is at least twice its measured
GitHub median: 0.236 / 0.153 / 0.229 / 1.620 s respectively round up to 2 s.
These API regression guards do not establish full target-laptop NFR acceptance.
Matching also has a 180 s cancellation timeout.

The `perf` marker is excluded from ordinary `pytest`. CI explicitly runs it
**once on Linux** after the ordinary backend suite; Windows runs the ordinary
suite only to avoid duplicating this ~49 s benchmark on the measured GitHub runner.
The standard-library
provenance collector also supports manual Windows runs. CI uses Python 3.13;
these recorded VM measurements use 3.12.16.

With the backend dev environment installed, from `PFC/app`:

```sh
pytest -m perf -s tests/perf
ruff check pfc tests
ruff format --check pfc tests
mypy pfc
pytest
```
