# Structured CV facts (REQ-IN-21 / REQ-IN-23)

Sync enriches normalized CV text before opening the persistence transaction.
The existing PDF/DOCX subprocess and `cv.extract_timeout_s` remain text-only.
Deterministic rules always run; a healthy local provider can additionally extract
facts using the versioned `cv_extract_v1` prompt, JSON Schema, temperature 0 and
seed 0. CV generation concurrency is one across jobs in the app. The system
setting `cv.llm_timeout_s` defaults to 180 seconds and includes health/queue time.
Absent providers use rules. Other failures use rules and increment
`facts_llm_failed` or `facts_llm_timeout` without failing the batch.

The async sync route captures the app event loop and published provider. The
background job submits a coroutine with `run_coroutine_threadsafe`; it does not
create a new event loop or move the provider's shared HTTP client into a parser
process. Its wait has a deadline and checks job cancellation. No existing
`llm_calls` writer exists, so this change does not introduce one.

`facts_json` keeps the existing `emails` array and adds `facts_schema: 1` and
`facts`, containing nullable `name`/`phone` and lists `experience`, `qualifications`,
`projects`. Each fact contains `value`, `evidence`, `[start,end]` span, and `source`
(`rule` or `llm`). Offsets are Python character indices into stored `text_norm`,
not byte offsets or offsets into the original PDF/DOCX. One validator drops every
fact whose span/quote is not exact or whose non-empty value is not a substring
of its quote. Phone values additionally must contain the same phone digits.
LLM quotes are located exactly; neither values nor quotes are repaired.

Rule names and phones take precedence. Each non-empty validated LLM category
supplies its list; empty or entirely ungrounded categories use their rule facts.
Failure uses rule lists.
Retained LLM facts record `extracted_by=llm:<provider>:<model>` and
`prompt_version=cv_extract_v1`; rules-only output records `rule_based`/NULL.
Versions without `facts_schema` are backfilled in place, preserving their emails
and other existing JSON keys. Already enriched versions are not regenerated.

Both candidate CV-history responses expose additive `facts` fields, including
CV entries within the candidate detail response. Legacy versions return null.
Phone values keep the existing `***` plus last-two format; all fact values and
evidence mask phone numbers and emails. Response spans remain coordinates in
the original stored text, so redacted evidence does not retain its original
length. Raw facts remain local in SQLite; audits contain counts, not CV facts.
Neither `candidates.phone` nor CV `resource_skills` is written by enrichment.
