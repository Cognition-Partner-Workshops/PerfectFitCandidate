# KPI-02 and KPI-05 baseline capture (P1-EX-03)

This offline, read-only tool implements [BRD §8](BRD.md#8-success-metrics-and-kpis)
and P1-EX-03. It produces JSON and Markdown without a UI, migrations, rescoring,
audit events, or database writes. It does not set acceptance targets.

## CLI usage

From `PFC/app`, with the backend environment activated:

```sh
PYTHONPATH=. python scripts/measure_kpi_baselines.py \
  --data-dir /path/to/pfc-data \
  --out-json baseline.json --out-md baseline.md
```

`--data-dir` is optional; `Settings` / `PFC_DATA_DIR` otherwise select the existing
database and upload directory. The CLI opens SQLite with `mode=ro` and takes a
single read transaction. It never creates a database or migrates an existing one.
Output directories must already exist. Reports must be distinct and outside the
application data directory; an existing hardlink to the database is also rejected.
Errors return exit status 1 without printing source names, emails or file paths.

## KPI-05: data quality

For **each committed CT run**, report total physical data rows and the count and
percentage with (a) non-empty grade, (b) non-empty skills, (c) valid email, and
(d) all three. Whitespace-only grade/skills are empty; a non-empty grade need not
be in the configured ladder. The denominator includes rejected, incomplete and
superseded duplicate rows, not only accepted/unique resources. Header and blank
separator rows are not data rows, following the ingestion parser.

Historical row fields are not stored in `ingestion_runs` or its review JSON
sidecar: that sidecar contains only diffs and counts. Therefore the service reads
the retained `uploads/<run_id>.xlsx`, checks its SHA-256 against the committed
run, and reuses the bounded CT parser. It imports `valid_ct_email`, the exact
predicate used by that parser, on the parser's stripped/lowercased email key.
Raw source emails are never emitted. Current mutable `resources` rows are **not**
used as a historical substitute. Current configured workbook safety limits apply
when reparsing; a missing, changed, invalid, or newly over-limit upload fails the
report rather than producing a fabricated baseline. Zero-row runs need no upload
read and produce zero counts with JSON `null` / Markdown `n/a` percentages.

The headline is the latest committed CT run by `committed_at`, breaking ties by
run id. Runs lacking a commit timestamp sort before timestamped runs. Preview,
failed, discarded and no-op runs are not committed uploads and are excluded.

For **both committed CT and committed DL runs**, parse-error rate is
`ingestion_runs.rows_rejected / ingestion_runs.rows_read * 100`.
These are the persisted upload-preview/commit counts (REQ-ING-02, REQ-ING-06).
`ingestion_row_issues` is not the numerator: one rejected row can have multiple
issues, and warnings do not make a row rejected. Zero denominators give `null` /
`n/a`; missing or inconsistent log counts fail the report explicitly.

## KPI-02: analyst effort, PFC timestamp part only

Per demand, the three milestones are:

1. **Upload:** `committed_at` of the committed DL `ingestion_runs` row referenced
   by `demands.first_ingestion_id`. Later uploads never replace this cycle.
2. **Confirm & Start:** the earliest non-null `jd_requirements.confirmed_at` for
   `demands.current_version_id` (REQ-MAT-02). Superseded revisions that were
   confirmed still count; confirmations of old demand versions do not.
3. **Shortlist reviewed proxy:** the earliest `email_messages.created_at` for
   that demand, regardless of its later status (REQ-EML-01). The report labels
   this **first Send to Email Approval**. It is not proof of human review or of
   approval/sending, nor does it prove every shortlisted candidate was reviewed.

JSON includes the UTC milestone timestamps and elapsed hours upload→confirm,
confirm→reviewed proxy and upload→reviewed proxy. Markdown includes per-demand
elapsed hours. All demands, including closed/archived demands, are included.
Missing milestones are counted separately per stage, and only intervals needing
that missing stage are excluded. Demands with no known committed DL origin form
an `unknown` cycle rather than being silently dropped.

Per DL cycle (the first ingestion run), each interval has a non-missing sample
count, median and p90. Median is the middle value, averaging the two central
values for even samples. **p90 uses nearest rank:** sort ascending and select
the 1-based rank `ceil(0.9 × n)`; no interpolation. Empty samples give null/n/a.
Hand check: hours `1,2,...,10` have median `5.5` and p90 at rank `9`, i.e. `9`.
Doubling those hours gives median `11`, p90 `18`. A fixture cycle with intervals
upload→confirm `[2,5,10]`, confirm→proxy `[3,3]`, upload→proxy `[5,8,10]` gives
medians `5,3,8` and p90s `10,3,10` respectively.

Persisted timestamps must be timezone-aware and are compared as instants in UTC.
The definitions deliberately combine the first upload, the current version's
first confirmation, and the demand's first-ever draft. Consequently a later JD
version or clock inconsistency can produce a negative interval; the tool retains
that signed value rather than substituting another milestone or silently dropping
it. These are elapsed wall-clock hours, not active analyst working time.

**The time-and-motion baseline for the current Excel process is manual and out
of scope.** It must be collected separately, along with the analyst log during
UAT/pilot. This timestamp report alone does not establish an effort reduction.

## Privacy and repeatability

Reports identify entities by valid DL requisition codes and ingestion run ids.
Invalid requisition values are replaced by `demand:<demand_id>` in both formats,
using the same format predicate as the DL parser, so malformed source values
cannot expose a name or email. They
contain no names, emails (even masked), CV data, filenames, owners, account names,
draft text, or upload paths. Preserve the source database and
original workbooks when capturing a baseline; later changes to the current demand
version can legitimately change KPI-02's confirmation milestone. Tests use
hand-computed fixtures, verify both output formats against fixture PII, and run
the service with SQLite `query_only` enabled.
