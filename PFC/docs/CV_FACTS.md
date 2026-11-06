# Structured CV facts (REQ-IN-21 / REQ-IN-23)

Sync enriches normalized CV text before opening the persistence transaction.
The existing PDF/DOCX subprocess and `cv.extract_timeout_s` remain text-only.
Deterministic rules always run; a healthy local provider can additionally extract
facts using the versioned `cv_extract_v2` prompt, JSON Schema, temperature 0 and
seed 0. CV generation concurrency is one across jobs in the app. The system
setting `cv.llm_timeout_s` defaults to 180 seconds and includes health/queue time.
Absent providers use rules. Other failures use rules and increment
`facts_llm_failed` or `facts_llm_timeout` without failing the batch.

The async sync route captures the app event loop and published provider. The
background job submits a coroutine with `run_coroutine_threadsafe`; it does not
create a new event loop or move the provider's shared HTTP client into a parser
process. Its wait has a deadline and checks job cancellation. No existing
`llm_calls` writer exists, so this change does not introduce one.

`facts_json` keeps the existing `emails` array and adds `facts_schema: 2` and
`facts`, containing nullable `name`/`phone` and lists `experience`, `qualifications`,
`projects`, `skills`. Each fact contains `value`, `evidence`, `[start,end]` span, and `source`
(`rule` or `llm`). Offsets are Python character indices into stored `text_norm`,
not byte offsets or offsets into the original PDF/DOCX. One validator drops every
fact whose span/quote is not exact or whose non-empty value is not a substring
of its quote. Phone values additionally must contain the same phone digits.
LLM quotes are located exactly; neither values nor quotes are repaired.

Skill facts additionally contain `canonical: string | null`. Their verbatim
`value` must equal `evidence`, with that exact token at the stored span; canonical
names are not part of grounding. Rules split lines under Skills, Technical Skills,
Key Skills and similar headings on commas, semicolons, pipes and bullet separators,
trim tokens, and keep the first case-insensitive occurrence. LLM proposals use the
same quote-location and validation path; proposed canonical metadata is ignored.
After validation, one approved OCC synonym snapshot per sync job resolves canonical
names, outside the persistence transaction. Unknown or unapproved terms remain null.

Rule names and phones take precedence. Each non-empty validated LLM category
supplies its list; empty or entirely ungrounded categories use their rule facts.
Failure uses rule lists.
Retained LLM facts record `extracted_by=llm:<provider>:<model>` and
`prompt_version=cv_extract_v2`; rules-only output records `rule_based`/NULL.
Versions whose `facts_schema` is absent or less than 2 are backfilled in place,
preserving their version IDs, emails and other existing JSON keys. Schema-2 versions
are not regenerated. Schema-1 facts remain readable until backfill.

Both candidate CV-history responses expose additive `facts` fields, including
CV entries within the candidate detail response. Legacy versions return null.
Phone values keep the existing `***` plus last-two format; all fact values and
evidence mask phone numbers and emails. Response spans remain coordinates in
the original stored text, so redacted evidence does not retain its original
length. Raw facts remain local in SQLite; audits contain counts, not CV facts.
Neither `candidates.phone` nor CV `resource_skills` is written by enrichment.
Grounded CV skills count alongside CT skills under each CV basis. Matching combines
existing CV `resource_skills` first with nonduplicate JSON skill facts, resolving
canonical names against the run's current approved synonyms. Recognised CV-only
skills earn skill credit and can lift NO_SKILLS when CT has no recognised skills.
Removed extractions contribute no JSON skill facts. Fact changes invalidate the
resource's incremental fingerprint. Ingestion still does not write scoring skill
records, and embedding inputs remain normalized CV text/sections, not `facts_json`.
Integration regressions cover CV-labelled evidence, credit, NO_SKILLS and changed-only
rescoring; skill facts are not separately vectorised/retrieved.

Public facts add `warnings: [{code, message}]`, computed on response rather than
stored. A null phone produces `PHONE_NOT_FOUND` / `Phone not found in CV.`; otherwise
the list is empty. Warnings appear on each card, plus at profile level for the latest
CV's missing phone. They never block actions. Skill values, evidence and canonical
metadata are redacted like other facts; response spans still refer to original text.
