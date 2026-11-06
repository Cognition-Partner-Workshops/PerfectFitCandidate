# Manual live LLM extraction validation — stage 1

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
`validation.run1.md`). JSON includes individual results, raw
responses and generation latencies, and Markdown summarizes them. Inputs and
outputs must never contain real candidate data. The script returns nonzero if
final fact validation fails or server memory exceeds 8 GB/is unavailable.
Extraction quality/fallback counts are reported, not hidden by pass thresholds.

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
