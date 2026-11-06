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

| Operation | Seconds |
|---|---:|
| Full 500 × 500 matching, 60,455 scored grade-compatible pairs | 23.302 |
| GET /analytics/summary, median of 3 | 4.631 |
| GET /analytics/demand, median of 3 | 4.789 |
| GET /analytics/bench, median of 3 | 4.949 |
| GET /analytics/match, median of 3 | 6.598 |

Provenance: Linux-6.8.0-1060-aws-x86_64-with-glibc2.35; Python **3.12.16**;
**Intel(R) Xeon(R) Processor**, **8 logical CPUs**; OS-reported RAM
**33,655,369,728 bytes** (~31.34 GiB). No cgroup CPU/memory limits were reported.
Measurement source SHA: `8ec55a7080dfe213b451b15c0ef3e3b422091f96`, with
uncommitted benchmark/configuration files. This SHA includes main's merged PR #22
state; merging that state changed ancestry only, not the measured application code.
The test prints raw samples, hardware, Python, current Git SHA/dirty state,
population counts and limits as a single `NFR01_RESULT=` JSON line.
Generated data/results are not committed.

**Interpretation:** matching is below both the 120 s budget and 60 s aspiration
here. All four analytics medians exceed the 2 s interaction budget (and the
500 ms API aspiration). NFR-01 is therefore **not demonstrated overall**.
The fake cached vectors, CT-only skill bases, unloaded single-client VM and
absence of browser timings cannot establish target-laptop acceptance. Benchmark
the Windows 11 Phase 0 laptop with real cached embeddings/CVs before sign-off.
This task measures the gap; it does not optimize production code.

## Regression limits and rerunning

Limits: **180 s** full matching (~7.7× this measurement; never tighter than
120 s) and **35 s** per analytics median (~5.3× the slowest measurement; never
tighter than 2 s). These are generous CI regression guards, **not NFR pass criteria**.
Matching also has a 180 s cancellation timeout.

The `perf` marker is excluded from ordinary `pytest`. CI explicitly runs it
**once on Linux** after the ordinary backend suite; Windows runs the ordinary
suite only to avoid duplicating this ~109 s benchmark. The standard-library
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
