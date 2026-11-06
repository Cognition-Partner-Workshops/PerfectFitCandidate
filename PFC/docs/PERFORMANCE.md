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

<a id="live-llm-extraction-linux-vm"></a>

## Live LLM extraction validation — Linux VM, 2026-11-06

**Linux VM evidence, not the Phase 0 target laptop or UAT acceptance.** One
manual run of `app/scripts/validate_llm_extraction.py`, committed at
`14c7935faa369971a1c01327e4cbde247df9058c`, against merged main `15a1433`
(#42 JD extraction and #43 structured CV facts). No prompts, validators or
generation settings were tuned. See [LLM_VALIDATION.md](LLM_VALIDATION.md)
for setup, fixture definitions, scoring rules and the recording-proxy design.

### Hardware and production defaults

- Linux 6.8.0-1060-aws x86_64, glibc 2.35; Python 3.12.16.
- Intel Xeon, 8 logical CPUs, AVX2; 33,655,369,728 bytes RAM (~31.34 GiB),
  ~29 GiB available before the run; no swap; ~105 GiB disk available.
- Real application supervisor and published provider, 4096 context, computed
  default **3 CPU inference threads**, one parallel slot. JD timeout 120 s;
  CV timeout 180 s. Artifact/data paths were the only settings overrides.

### Exact verified artifacts

All artifacts, logs and reports remain outside the repository under
`/home/ubuntu/pfc-models/llm/`. No models or binaries are committed.

- **Qwen3-4B-Instruct-2507 Q4_K_M**, exact `MODELS.lock` pin (Bartowski GGUF):
  [pinned download](https://huggingface.co/bartowski/Qwen_Qwen3-4B-Instruct-2507-GGUF/resolve/5ba9dff45461e5bab86959be7d585609fe9e6bc3/Qwen_Qwen3-4B-Instruct-2507-Q4_K_M.gguf).
  Size **2,497,280,736 bytes**; downloaded SHA-256 verified against the lock:
  `2fde00ce69dd4899c70d020845e2638353015bba0fdf161b3eb965f2bca4464e`.
- **llama-server release b8936**, official Linux x64 CPU asset
  [`llama-b8936-bin-ubuntu-x64.tar.gz`](https://github.com/ggml-org/llama.cpp/releases/download/b8936/llama-b8936-bin-ubuntu-x64.tar.gz),
  **13,911,156 bytes**. Archive SHA-256 verified against the published asset
  `digest` from the [GitHub release API](https://api.github.com/repos/ggml-org/llama.cpp/releases/tags/b8936):
  `9eb1116a03be6ab2056c38788cc90a1a86541443bf44b703eb6e2f809b068438`.
  Binary reports version **8936 (2dd84169d)**, GNU 11.4.0, Linux x86_64.
  **The packaging manifest pins only win-x64**; this Linux archive was selected
  from the same official release and verified using the API digest, not a
  Linux entry in `llama_cpp.manifest.json`.

### Results

Ten small synthetic JDs and ten synthetic CVs; example.com email addresses and
Ofcom drama phone numbers only. Twenty raw completion responses were captured.
Scoring uses deduplicated exact NFKC + casefold + whitespace-collapsed values,
micro-averaged per category, with no alias/substring/semantic credit.

| Measurement | Result |
|---|---:|
| Supervisor start → healthy | 3.547 s |
| Generation call median / maximum | 14.736 / 30.405 s |
| llama-server peak VmHWM/VmRSS | 5,555,294,208 bytes (5.555 GB), below 8 GB |
| JD LLM-used share | 10/10 (100%) |
| JD fallback reasons | none |
| JD schema-invalid responses | 0 |
| Mandatory skills recovered; precision / recall | 16/16; 94.1% / 100% |
| Preferred skills recovered; precision / recall | 10/10; 100% / 100% |
| CV extraction failures / schema-invalid responses | 0 / 0 |
| Final CV fact/span validation failures | **0** (required: 0) |

| CV category | Expected recovered | LLM raw → quote-located → validated | Ungrounded drops | Replaced by rule precedence |
|---|---:|---:|---:|---:|
| name | 9/9 | 0 → 0 → 0 | 0 | 0 |
| phone | 8/8 | 0 → 0 → 0 | 0 | 0 |
| experience | 10/10 | 18 → 18 → 18 | 0 | 0 |
| qualifications | 9/9 | 9 → 9 → 9 | 0 | 0 |
| projects | 10/10 | 10 → 10 → 10 | 0 | 0 |

All ten raw CV responses explicitly returned **`name: null`, `phone: null`**:
the zero raw/precedence counts are genuine, not a counting omission. The final
identity facts came from rules. Offline fake-provider tests exercise non-null
identity proposals and verify that rule replacements are reported as precedence,
not ungrounded drops. Zero grounding failures proves the checked span/value
invariants, not complete semantic correctness or production quality.

**Observed issue, no tuning:** fixture **jd05** labels PostgreSQL preferred,
but the model additionally classified it as mandatory. This is the extra
prediction behind mandatory precision 16/17 (94.1%). The issue remains visible
in the report; no prompt or validator changes were made to improve this score.

### Reproduction

With the repository virtual environment activated, from `PFC/app`, after
downloading/verifying the artifacts as described in LLM_VALIDATION.md:

```sh
python scripts/validate_llm_extraction.py \
  --gguf ~/pfc-models/llm/Qwen3-4B-Instruct-2507-Q4_K_M.gguf \
  --llama-server ~/pfc-models/llm/b8936/llama-b8936/llama-server \
  --out ~/pfc-models/llm/validation/report
```

The captured outputs are `~/pfc-models/llm/validation/report.json` and
`report.md`; child logs are under `validation/supervisor-data/logs/llm.log`.
The supervisor stopped the server after the run. Default pytest needs no model,
binary or download. This evidence does not replace locked-down Windows laptop
validation, representative full-size inputs, or analyst review.
