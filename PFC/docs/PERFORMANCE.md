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

### OCC-domain follow-up (2026-11-06)

The first OCC-domain run measured match analytics at **1.159 s**, versus the
earlier reported 0.586 s. A saved 500 × 500 database (60,455 results) was used for
profiling and a same-database comparison, without changing results or goldens:

| Measurement | Match analytics median |
|---|---:|
| OCC implementation before validation fixes, five warm GETs | 1.205 s |
| After validation fixes, same database, five warm GETs | 0.726 s |
| Main analytics implementations restored in-memory, same database/dependencies | 1.111 s |
| Fresh full perf fixture after fixes, three warm GETs | 0.712 s |

The main control restores the main-branch analytics modules and original mask
behavior in a separate process; it is not a full base checkout or a reproduction
of historical machine load. It rules out attributing the earlier twofold jump
solely to OCC configuration. The earlier 0.586 s was not reproduced: the current
result is about 0.7 s, **not a claimed return to 0.586 s**.

The reachable bottleneck is validation, not config construction: cProfile showed
one OCC config/SynonymTable build per request, about 2 ms. EXPLAIN showed the
capacity-input query choosing `ix_results_resource`, scattering breakdown reads.
Choosing `ix_results_demand` reduced that query from 0.308 to 0.133 s in isolation.
Request-local memoization reuses pure masks/date/score checks across repeated
values (120,910 mask calls became 402; 60,455 score checks became 292; date
checks became 119). Narrower projections and tuple-to-dict conversion reduce
row hydration cost. No cross-request cache, schema, KPI, band or guard changes.
The same-database match JSON is exactly equal before/after and in the main control;
the committed analytics goldens remain unchanged.

Fresh perf fixture medians: summary **0.070 s**, demand **0.094 s**, bench
**0.070 s**, match **0.712 s**; full matching **26.408 s**. Provenance: Linux
6.8.0-1060-aws / glibc 2.35, Python 3.12.16, Intel Xeon, 8 logical CPUs,
33,655,369,728 bytes RAM, base `97f6cc8`, working tree dirty. Evidence is retained
locally under `/home/ubuntu/occ-domain-evidence/`; no new GitHub-runner or target
laptop measurement is claimed here. All existing thresholds remain unchanged.

### Original analytics optimization

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

## Linux CI/VM, not target laptop

### Real ONNX embedding validation — 2026-11-06 (UTC)

One manual CPU-only run of `app/scripts/validate_embedding_model.py`, using the
official `nomic-ai/nomic-embed-text-v1.5` artifacts at revision
`ac6fcd72429d86ff25c17895e47a9bfcfc50c1b2`, pinned in `packaging/MODELS.lock`.
Both existing local files were re-hashed and matched the lock; no replacement
model or download was needed. Model files remain outside the repository at
`/home/ubuntu/pfc-models/nomic-embed-text-v1.5/`.

Official pinned sources:
- [ONNX int8 model](https://huggingface.co/nomic-ai/nomic-embed-text-v1.5/resolve/ac6fcd72429d86ff25c17895e47a9bfcfc50c1b2/onnx/model_int8.onnx)
- [Tokenizer](https://huggingface.co/nomic-ai/nomic-embed-text-v1.5/resolve/ac6fcd72429d86ff25c17895e47a9bfcfc50c1b2/tokenizer.json)

Verified SHA-256s:
```text
model_int8.onnx  b4342336debaea79de872370664b0aaeb67dea4605513d00ee236ea871a81f27
tokenizer.json   d241a60d5e8f04cc1b2b3e9ef7a4921b27bf526d9f6050ab90f9267a1f9e5c66
```

Provenance: Linux 6.8.0-1060-aws; Python **3.12.16**; onnxruntime **1.31.1**,
**CPUExecutionProvider**; **Intel(R) Xeon(R) Processor**, `nproc` **8**.
Before validation, `free -h` reported 31 GiB total RAM, 963 MiB used, 24 GiB free,
75 MiB shared, 5.5 GiB buff/cache and 29 GiB available; swap was 0 B.
The run used main base `f630ad8` plus the validator's reporting additions.

| Measurement | Result |
|---|---:|
| Model load | 0.900 s |
| Batch embed throughput | 58.926 docs/s |
| Tokenizer-counted batch throughput | 1511.449 tokens/s |
| Timed batches | 20 inputs, 513 tokens, 0.339 s |
| Warmed per-chunk latency, mean | 18.653 ms |
| Warmed per-chunk latency, p50 | 18.640 ms |
| Warmed per-chunk latency, p95 | 28.316 ms |
| Peak RSS (`resource.getrusage`) | 332.801 MiB |
| Retrieval top-1 | PASS: 10/10 |
| Related vs unrelated cosine sanity | PASS: 10/10; minimum margin +0.181082 |

The corpus is ten short synthetic JD/CV pairs. JDs use the query task and CVs
the document task. Each input chunk counts as one document for docs/s: two
10-input batches, after twenty warmed single-input measurements. Tokens are
counted by the model tokenizer over task-prefixed input, including tokenizer
special tokens but excluding padding; this counting pass is outside the timed
embed calls. Every related CV beats the highest-scoring non-matching CV, with
both cosines and their margin printed individually. Vectors are 768-dimensional,
finite and L2-normalized (observed norms 0.9999999–1.0000000).

The adapter does not guard pre-prefixed input: the probe reports that it adds
another prefix, so callers must supply raw text. This benchmark is not full-CV
throughput, application matching throughput, or target-laptop acceptance.

Reproduce with the backend virtual environment activated, from `PFC/app`, after
verifying the two local artifacts against `packaging/MODELS.lock`:
```sh
python scripts/validate_embedding_model.py \
  --model-dir /home/ubuntu/pfc-models/nomic-embed-text-v1.5/
```
