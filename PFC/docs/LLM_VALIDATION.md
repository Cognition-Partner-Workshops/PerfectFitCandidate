# Manual live LLM extraction validation

REQ-LLM-02, REQ-MAT-02, REQ-IN-21/23. This opt-in Linux CPU probe does not
change production prompts, validators, packaging or CI. Synthetic fixtures are
in `app/tests/fixtures/llm_validation.json`: ten JDs and ten CVs, no real people,
only example.com email addresses and Ofcom drama numbers. This is not a quality
acceptance threshold or a target-laptop performance certification.

## Artifacts (outside the repository)

Use `~/pfc-models/llm/` for all downloads, extraction, logs and generated reports.
Download the exact GGUF URL in `packaging/MODELS.lock`; SHA-256 the entire file
and fail closed on any mismatch. The CLI repeats this verification before start.

For the server, query the official release API:
`https://api.github.com/repos/ggml-org/llama.cpp/releases/tags/b8936`.
Select **llama-b8936-bin-ubuntu-x64.tar.gz** (CPU), require a `sha256:` asset
`digest`, download its `browser_download_url`, and verify the entire archive
against that digest before extracting it. Stop if the published digest is absent.
The API digest observed for this stage was:

```text
9eb1116a03be6ab2056c38788cc90a1a86541443bf44b703eb6e2f809b068438
```

Keep the extracted shared libraries next to `llama-server`. Do not commit any
downloaded artifacts. There is no automatic download in the validation CLI.

## Running

Activate the repository virtual environment; from `PFC/app`:

```sh
python scripts/validate_llm_extraction.py \
  --gguf ~/pfc-models/llm/Qwen3-4B-Instruct-2507-Q4_K_M.gguf \
  --llama-server ~/pfc-models/llm/b8936/llama-b8936/llama-server \
  --out ~/pfc-models/llm/validation/report
```

Alternatively set `PFC_VALIDATION_GGUF` and `PFC_VALIDATION_LLAMA_SERVER`.
`--out` is an external output prefix: `.json` and `.md` are appended to the
entire prefix, preserving dots (e.g. `validation.run1.json` and
`validation.run1.md`). Both files use UTF-8. JSON includes individual results, raw
responses and generation latencies, and Markdown summarizes them. Inputs and
outputs must never contain real candidate data. The script returns nonzero if
final fact validation fails or server memory exceeds 8 GB/is unavailable.
Extraction quality/fallback counts are reported, not hidden by pass thresholds.

## Count-only summary pass

Use the same verified model, binary and supervisor with `--summaries-only`:

```sh
PYTHONPATH=. python scripts/validate_llm_extraction.py --summaries-only \
  --gguf ~/pfc-models/llm/Qwen3-4B-Instruct-2507-Q4_K_M.gguf \
  --llama-server ~/pfc-models/llm/b8936/llama-b8936/llama-server \
  --out ~/pfc-models/llm/validation/summaries.run2
```

This mode calls production `services/summaries.summarise_text`, also used by the routes,
with the unchanged summary prompt, schema, 60-second deadline, evidence substring and
digit checks, then summary-only full contact redaction. Other screens retain the unchanged
REQ-OUT-03 masker. No database is needed and no summary output text
is written to JSON, Markdown or progress logs. The normal extraction mode is unchanged
and still retains its synthetic extraction responses.
Redacted strings must still fit the existing 300-character schema bounds; replacement
overflow fails closed with SUMMARY_INVALID rather than truncation or an internal error.

The summary pass covers all ten original JDs, five held-out JDs from
`app/tests/fixtures/llm_validation_held_out.json` (override with `--held-out-fixtures`),
and ten original CVs. Inputs use the normal production text normalization.
Each row contains only fixture ID, schema-valid flag, returned/kept/drop counts, outcome,
latency and remaining email/phone-pattern count across retained text and evidence. The
remaining-pattern check uses the masker's `EMAIL`, `MASK_PHONE` and `YEAR_LIST` rules.
Evidence failures take precedence: a sentence with both failures counts only as an
evidence drop. Invalid-schema calls are not grounded; a parseable `sentences` array still
contributes its returned count, but malformed/missing responses contribute zero.
503 calls have schema-valid=no, not a fabricated schema-invalid 422.

Aggregates separate JDs and CVs, with kept/returned share and 422/call share. Zero
denominators are n/a. Latency covers provider health, generation, validation, grounding
and redaction, including failed calls. The existing 50 ms child-process VmRSS/VmHWM sampler
is used, with a final sample per category. Categories run JD then CV on one supervised
model; HWM is cumulative since process start, so the CV peak can include the JD phase.
The 8 GB comparison is model-process memory only, not combined Python/embedding memory
or target-laptop acceptance. The CLI fails on excessive/unavailable memory or any
remaining email/phone pattern; 422/503 rates are reported, not hidden behind quality gates.

## Summary contact-redaction validation — Linux VM, not target laptop

Measured 2026-11-06 on Linux 6.8.0-1060-aws x86_64, 8 logical CPUs, 33,655,369,728 bytes
RAM, Python 3.12.16; production supervisor defaults: context 4096, 3 CPU threads.
Startup to healthy: **4.067 s**. The model was the MODELS.lock-pinned
Qwen3-4B-Instruct-2507-Q4_K_M (SHA-256
`2fde00ce69dd4899c70d020845e2638353015bba0fdf161b3eb965f2bca4464e`), with official
Linux CPU llama.cpp **b8936**, version **8936 (2dd84169d)**. The archive hash matched
the published release digest above; the harness reverified the entire GGUF before start.

Count-only per-fixture JSON/Markdown and progress log are outside git at
`~/pfc-models/llm/validation/summaries.run2.{json,md,log}`. No model output text was
saved. The earlier run1 measured **2** remaining patterns in `jd08` from the existing
partial email mask's suffix. This rerun uses full contact redaction only for summaries;
the prompt, schema, evidence-substring and digit grounding checks are unchanged, and
other screens retain their REQ-OUT-03 masking. The literal zero-pattern gate is unchanged.

### JD

| Metric | Value |
|---|---:|
| Calls | 15 |
| Schema-valid calls | 15 |
| 200 | 15 |
| 422 SUMMARY_UNGROUNDED | 0 |
| 422 SUMMARY_INVALID | 0 |
| 503 LLM_UNAVAILABLE | 0 |
| Sentences returned | 44 |
| Sentences kept | 43 |
| Evidence drops | 1 |
| Digit-rule drops | 0 |
| Kept share | 97.7% |
| Calls ending in 422 | 0.0% |
| Median latency (s) | 22.587 |
| Maximum latency (s) | 30.771 |
| Remaining email/phone patterns | 0 |
| Peak model RSS/HWM (bytes) | 5330759680 |
| Model ≤ 8 GB | PASS |

### CV

| Metric | Value |
|---|---:|
| Calls | 10 |
| Schema-valid calls | 10 |
| 200 | 10 |
| 422 SUMMARY_UNGROUNDED | 0 |
| 422 SUMMARY_INVALID | 0 |
| 503 LLM_UNAVAILABLE | 0 |
| Sentences returned | 47 |
| Sentences kept | 47 |
| Evidence drops | 0 |
| Digit-rule drops | 0 |
| Kept share | 100.0% |
| Calls ending in 422 | 0.0% |
| Median latency (s) | 27.337 |
| Maximum latency (s) | 30.986 |
| Remaining email/phone patterns | 0 |
| Peak model RSS/HWM (bytes) | 5713330176 |
| Model ≤ 8 GB | PASS |

All 25 calls were schema-valid and returned 200; all retained sentences passed the
literal post-redaction email/phone-pattern check. `jd03` dropped one sentence for
absent evidence; no digit-only drops occurred in this live sample. Fake-provider tests
exercise digit drops, evidence drops, invalid output and unavailable-provider outcomes.
Both model-memory peaks are below decimal 8 GB. CV HWM is cumulative since startup
and can include the earlier JD phase. This does not measure combined Python/embedding
memory or certify laptop latency, visual states or summary entailment. Grounding checks
verbatim evidence and numbers only, not whether a sentence follows from its quote.
Target-laptop checks are in UAT_RUNBOOK.md step 9 and remain unexecuted here.

## Production wiring and metrics

- Instantiate `Settings` with only artifact/data locations changed. Use the real
  `LlamaServerSupervisor`, its `start` and published `app_state.llm_provider`,
  exactly as the application's lifespan does; no custom server arguments.
  Defaults: 4096 context, one parallel slot, computed CPU thread count,
  120-second supervisor startup and JD timeout, 180-second CV timeout.
  No application database, active-model settings or user data is modified.
- Run `JdLlmBridge.extract_jd` through an AnyIO worker; validate with production
  `Requirements` and use the production deterministic fallback on failure.
  The same fallback reason mapping as `analyze_jd` is retained. Schema-invalid
  raw responses are additionally counted even when the adapter rejects them
  before the JD bridge can classify them.
- Run production CV `extract_facts` in a worker, using its owning event loop
  and a shared semaphore of one. Do not run the shared provider on a new loop.
- The recording provider delegates requests unchanged. A read-only response
  hook on the real adapter's HTTP client also captures completion text before
  adapter schema validation. This private-client access is confined to this
  manual harness and covered by a mock-transport test.
- For schema-valid CV responses, locate exact quotes with `text.find`, then use
  production `validate_facts`. Report raw → quote-located → validated counts
  per category. Ungrounded drops are raw minus validated; schema-invalid/missing
  responses are separate, not made-up per-fact counts. Validated LLM name/phone
  replaced by rule precedence are reported separately, not counted as drops.
- Every final fact is passed back through `validate_facts` against the same
  normalized input, including the exact span invariant. Required failures: zero.
- Expected recovery, JD precision and recall use deduplicated sets of **exact
  NFKC-normalized, casefolded, whitespace-collapsed values** within each category.
  No aliases, substring matching or semantic credit. Totals are micro-averaged;
  zero denominators are null / n/a. Fallback outputs are scored too, but the
  LLM-used share and fallback histogram distinguish their origin.
- Measure every generation call's wall time (including failures), median/max,
  supervisor start → healthy time, and poll the child `/proc/<pid>/status`
  VmHWM/VmRSS every 50 ms. Peak is the maximum of both, plus a final sample;
  the memory limit is decimal **8,000,000,000 bytes**, not parent Python RSS.

Offline regression checks (no model/binary/network required):

```sh
ruff check
ruff format --check
mypy pfc
pytest tests/unit/test_llm_validation.py
```

## Duplicate-only correction validation

### Current main re-baseline — Linux VM, not target laptop

Main's REQ-MAT-06 change added `ExtractedItem.evidence_unverified` to the
generation schema, so both sides were rerun rather than reusing the older
baseline. These are the **current results**; the earlier pair below is history.

| Run | Source commit | External report directory |
|---|---|---|
| Before-m, current origin/main at fetch | `a81110ac05552ab3fb62bbf283a2f5bfd908b90c` | `~/pfc-models/llm/before-m/` |
| After-m, merged branch | `6b43110c257ebd51403521a5da46af4d79e3a4eb` | `~/pfc-models/llm/after-m/` |

The before run used a detached main worktree and its own Python modules.
All four probes ran serially with temperature 0 and seed 0. The main and
merged generation-schema JSON were checked byte-identical, including
`evidence_unverified`; only the branch's internal `classification_reason`
is excluded from generation. The prompt is unchanged.

Artifacts: Qwen3-4B-Instruct-2507-Q4_K_M GGUF SHA-256
`2fde00ce69dd4899c70d020845e2638353015bba0fdf161b3eb965f2bca4464e`;
official Linux CPU llama.cpp **b8936**, version **8936 (2dd84169d)**.
Archive SHA-256:
`9eb1116a03be6ab2056c38788cc90a1a86541443bf44b703eb6e2f809b068438`.
The archive hash and all 43 extracted binary/library files were reverified;
each probe also verified the GGUF hash. Raw reports and source-SHA records
remain outside git in the directories above.

#### Original ten — current re-baseline

Default fixtures: jd01–jd10 and ten CVs. Latency covers twenty calls.

| Metric | Before-m (main) | After-m (merged branch) |
|---|---:|---:|
| Mandatory precision | 94.12% (16/17) | 100% (16/16) |
| Mandatory recall | 100% (16/16) | 100% (16/16) |
| Preferred precision | 100% (10/10) | 100% (10/10) |
| Preferred recall | 100% (10/10) | 100% (10/10) |
| JD schema-invalid count | 0 | 0 |
| JD LLM-used share | 100% (10/10) | 100% (10/10) |
| JD fallback count | 0 | 0 |
| Generation calls | 20 | 20 |
| Median latency | 13.654 s | 16.944 s |
| Maximum latency | 47.385 s | 38.620 s |
| Startup → healthy | 3.548 s | 3.558 s |
| Peak server RSS/HWM | 5,555,314,688 bytes | 5,555,421,184 bytes |
| Final CV fact/span failures | 0 | 0 |

**Gate: PASS.** Main still duplicates PostgreSQL on jd05. Its mandatory list
changes only from `["SQL", "PostgreSQL"]` to `["SQL"]`; PostgreSQL remains
preferred with `classification_adjusted`. Every other per-JD mandatory and
preferred text list, including order and casing, is identical.
Raw responses are byte-identical for **20/20 calls**. Only jd05's duplicate
post-check changes a skill list; no fallback or schema failure occurred.

#### Held-out five — current re-baseline

Same separate jd11–jd15 fixtures, with no CVs; no tuning was performed.

| Metric | Before-m (main) | After-m (merged branch) |
|---|---:|---:|
| Mandatory precision | 10% (1/10) | 10% (1/10) |
| Mandatory recall | 11.11% (1/9) | 11.11% (1/9) |
| Preferred precision | 20% (1/5) | 20% (1/5) |
| Preferred recall | 20% (1/5) | 20% (1/5) |
| JD schema-invalid count | 0 | 0 |
| JD LLM-used share | 100% (5/5) | 100% (5/5) |
| JD fallback count | 0 | 0 |
| Generation calls | 5 | 5 |
| Median latency | 21.323 s | 36.832 s |
| Maximum latency | 22.758 s | 49.187 s |
| Startup → healthy | 3.047 s | 3.102 s |
| Peak server RSS/HWM | 5,118,488,576 bytes | 5,118,472,192 bytes |

**Gate: PASS.** All held-out skill lists and extraction metrics are unchanged;
raw responses are byte-identical for **5/5 calls**. The poor exact-text scores
and every missed/extra phrase are also unchanged from after2: jd13 is correct;
jd11, jd12, jd14 and jd15 retain expanded phrases, with an extra task phrase on
jd14. The historical canonicalization table below still describes the same
misses; canonicalization was not rerun or changed.

**Report-only timing flag:** held-out maximum latency increased **2.161×**
(22.758 s → 49.187 s). Latency and RSS are not extraction gates.
These VM observations are not a controlled post-check-overhead measurement
and do not certify target-laptop performance. Both runs stayed below the
8 GB memory ceiling. Machine-readable per-JD/raw-versus-final comparisons
are in `~/pfc-models/llm/after-m/comparison.json`.

### Historical: Before versus after2 — Linux VM, not target laptop

The gate is **extraction quality**, not VM latency. Generation uses temperature
0 and seed 0. The restored system prompt and generation-schema JSON were checked
byte-identical to the before revision, allowing reuse of its reports.

| Run | Source commit | External report directory |
|---|---|---|
| Before, both splits | `8db7dda28d0fa7fb7f46f92345c1884b809006cf` | `~/pfc-models/llm/before/` |
| Failed first attempt, both splits | `563a1c5e988067cf9316de1321abec0f5d60a8f5` | `~/pfc-models/llm/after/` |
| After2, both splits | `dfeb978e07d0bd6e5dfa4030a02d368406baad8d` | `~/pfc-models/llm/after2/` |

All runs used Qwen3-4B-Instruct-2507-Q4_K_M, GGUF SHA-256
`2fde00ce69dd4899c70d020845e2638353015bba0fdf161b3eb965f2bca4464e`,
and official Linux CPU llama.cpp **b8936**, binary version **8936 (2dd84169d)**.
The `llama-b8936-bin-ubuntu-x64.tar.gz` archive SHA-256 is
`9eb1116a03be6ab2056c38788cc90a1a86541443bf44b703eb6e2f809b068438`.
Artifacts were hash-verified; all 43 extracted binary/library files were also
compared with the verified archive. Models, binaries and raw reports remain
outside git. These runs predate the subsequent merge of main and are not
measurements of the post-merge documentation commit.

#### Original ten

Default `app/tests/fixtures/llm_validation.json`: jd01–jd10 plus ten CVs.
Latency covers all twenty generation calls.

| Metric | Before | After2 |
|---|---:|---:|
| Mandatory precision | 94.12% (16/17) | 100% (16/16) |
| Mandatory recall | 100% (16/16) | 100% (16/16) |
| Preferred precision | 100% (10/10) | 100% (10/10) |
| Preferred recall | 100% (10/10) | 100% (10/10) |
| JD schema-invalid count | 0 | 0 |
| JD LLM-used share | 100% (10/10) | 100% (10/10) |
| JD fallback count | 0 | 0 |
| Generation calls | 20 | 20 |
| Median latency | 13.022 s | 20.073 s |
| Maximum latency | 24.890 s | 39.086 s |
| Startup → healthy | 3.554 s | 3.065 s |
| Peak server RSS/HWM | 5,552,574,464 bytes | 5,552,553,984 bytes |

**Original-ten gate passed:** the only final skill-list change is jd05 mandatory
`["SQL", "PostgreSQL"]` → `["SQL"]`; PostgreSQL remains preferred.
All other mandatory/preferred lists, including ordering and casing, are unchanged.

#### Held-out five

Separate `app/tests/fixtures/llm_validation_held_out.json`, split
`held_out_5`: jd11–jd15, no CVs. These fixtures remain checked in. Their before
results were kept unread until the first implementation was final; no tuning of
the fixtures, scorer or canonicalization followed their inspection.

| Metric | Before | After2 |
|---|---:|---:|
| Mandatory precision | 10% (1/10) | 10% (1/10) |
| Mandatory recall | 11.11% (1/9) | 11.11% (1/9) |
| Preferred precision | 20% (1/5) | 20% (1/5) |
| Preferred recall | 20% (1/5) | 20% (1/5) |
| JD schema-invalid count | 0 | 0 |
| JD LLM-used share | 100% (5/5) | 100% (5/5) |
| JD fallback count | 0 | 0 |
| Generation calls | 5 | 5 |
| Median latency | 20.696 s | 22.728 s |
| Maximum latency | 23.622 s | 24.825 s |
| Startup → healthy | 3.049 s | 3.045 s |
| Peak server RSS/HWM | 5,123,481,600 bytes | 5,123,469,312 bytes |

**Held-out extraction-quality gate passed:** all final skill lists and quality
metrics are unchanged. Scores remain poor because expanded phrases do not
receive exact-text credit. jd13 is correct; the remaining errors are listed below.

All **20/20 original** and **5/5 held-out raw response texts are byte-identical**
between before and after2. The original-ten latency rose too, despite identical
outputs. The latency increase is **VM timing noise, not the post-check**; it is
report-only, not an extraction-quality regression. Neither latency nor peak RSS
doubled. These small synthetic probes do not certify target-laptop performance.

#### Failed first attempt

The first attempt at `563a1c5` changed the prompt and added general movement.
Raw output combined atomic skills into phrases such as `Python and SQL`,
`Kubernetes and Linux skills` and `Java and Spring`. The original-ten gate
blocked: mandatory precision/recall fell to 76.92%/62.50%, preferred to 90%/90%.
Raw and final lists matched for these regressions: the compound skills came from
the changed prompt/model output, not Python merging names. The exact responsible
prompt wording was not isolated. This attempt was not pushed at that gate.

After2 restores the original prompt and limits correction to skills present in
**both** lists under NFKC/casefold/whitespace normalization. Grounded evidence,
otherwise the skill's clause, otherwise its preceding heading supplies context.
Only unambiguous mandatory-only or preferred-only cues remove the wrong-list
copy; both/neither cues and all single-list items remain untouched.

#### Canonicalization finding — no change made

A fresh migrated DB seeded from the default config contained **13 skills and
31 synonym rows**. Each phrase was run through the production OCC-backed
`config.synonyms.resolve` path and existing skill-name lookup used by matching
persistence (`requirements._write` and `runs._demands`).
Approved synonyms resolve whole normalized terms, not embedded substrings.
The CV skill tokenizer is not the JD requirement path.

**Every row below was evaluated in both before and after2**, with identical
results. `NULL` means no canonical result. The extra task phrase has no expected
skill counterpart. This covers all missed expected skills and extra predictions;
no synonym, normalizer, prompt or scoring change was made for this diagnosis.

| JD | Extracted phrase (before and after2) | Expected skill | Canonical result | Resolves |
|---|---|---|---|---|
| jd11 | Use Python to develop ingestion jobs | Python | NULL | No |
| jd11 | Use SQL for reconciliation checks | SQL | NULL | No |
| jd11 | Kafka experience for asynchronous order updates | Kafka | NULL | No |
| jd12 | R for statistical reporting workflow | R | NULL | No |
| jd12 | SQL to validate source extracts | SQL | NULL | No |
| jd12 | Power BI experience for sharing dashboards with study coordinators | Power BI | NULL | No |
| jd14 | Develop control software for remote environmental instruments | — extra prediction | NULL | No |
| jd14 | C++ is essential for the control loop | C++ | NULL | No |
| jd14 | Must be able to diagnose device faults with Linux | Linux | NULL | No |
| jd14 | Rust experience for future telemetry components | Rust | NULL | No |
| jd15 | Terraform for environment provisioning | Terraform | NULL | No |
| jd15 | Kubernetes knowledge to operate production clusters | Kubernetes | NULL | No |
| jd15 | Prometheus experience for expanding operational dashboards | Prometheus | NULL | No |

Thus default canonicalization does **not** rescue these exact-text misses. This
does not evaluate possible embedding-based credit in a matching run.


### Item provenance

The retained duplicate records `classification_reason: "classification_adjusted"`
in its existing revision's `extraction_json`; the additive nullable API item
field is `items[].classificationReason`. Unset keys are omitted from stored
JSON, preserving existing item serialization. No migration was needed.
Manual saves inherit the reason through validated same-JD-version
`source_jd_req_id` links with matching kind and NFKC/casefold/whitespace-normalized
text, not client assertions. Offline tests verify the API,
two saves and confirmation, plus rejection of forged/unknown-source reasons.
`extractionMethod` and `fallbackReason` are unchanged. The model cannot supply
this internal reason: `jd_generation_schema()` excludes it. The current schema
includes main's `evidence_unverified` field and byte-matches the re-baselined
main schema; the original-schema equality claim applies to the historical pair.

The initial jd05 reproduction, prompt/schema quotations and fixture history
are retained in [JD_MANDATORY_PREFERRED_DIAGNOSIS.md](JD_MANDATORY_PREFERRED_DIAGNOSIS.md).
