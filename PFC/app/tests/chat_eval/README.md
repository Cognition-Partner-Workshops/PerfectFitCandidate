# Chat SQL evaluation

From `PFC/app`, run the offline CI checks:

```sh
../../.venv/bin/pytest tests/chat_eval tests/unit/test_chat_intent.py tests/unit/test_chat_sql.py tests/integration/test_chat_api.py -q
```

Run against an already running local llama-server (no downloads or server startup are performed):

```sh
../../.venv/bin/python tests/chat_eval/run_eval.py --base-url http://127.0.0.1:8080 --out results.json
```

The existing `LlamaCppProvider` enforces loopback addresses and uses `/health` and
`/v1/chat/completions`. The runner saves JSON, prints overall/category accuracy,
median/p95 latency, and every failure. Latency includes classification, database
execution and correction retries, but excludes fixture building and reference execution.
The JSON includes attempted SQL, actually executed generated SQL, outcome and errors.
The synthetic database is retained next to the output in a unique `results-fixture-*`
directory; reruns build new fixtures. These are evaluation artifacts, not files to commit.

The optional pytest live run is skipped unless `PFC_CHAT_EVAL_URL` is set:

```sh
PFC_CHAT_EVAL_URL=http://127.0.0.1:8080 ../../.venv/bin/pytest tests/chat_eval/test_eval.py::test_real_model -s
```

## Dataset and scoring

- Exactly 100 questions: 30 single-view, 25 aggregate/group-by, 20 top-N/rank,
  10 joins, 15 must-refuse. All 85 reference queries have non-empty results.
- Fixed RNG seed 1729; 50 demands and 80 resources, including 3 archived demands
  and 4 archived resources. Two completed runs have independently shuffled ranks
  and different scores. All JDs are confirmed and skills are canonical.
- Both router and evaluator use `answer_question`: classification followed by the
  real lookup/SQL guard with at most two SQL-correction retries. Inputs are masked.
- Compare result **multisets**, preserving duplicate counts, rounding floats to two
  decimal places and ignoring column names. Compare row order only for `ordered: true`.
  Every ordered reference has deterministic tie-breaking.
- Refusals require `unsupported` or `uninterpretable` and **no generated SQL actually
  executed**. SQLite tracing distinguishes rejected attempts from executed statements;
  trusted setup/masking/reference queries do not count as generated SQL.
- The four fixed lookup intents score **incorrect** for non-refusal items. Their
  prose/source payloads do not have a general tabular equivalence mapping, and the
  evaluator deliberately does not scrape numbers from prose. This is a SQL-generation
  benchmark, not an end-user answer-quality score; exact fixed-lookup questions can
  therefore lower real-model accuracy despite valid product behavior.
