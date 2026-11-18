# Local PDF reports

`POST /api/v1/reports/pdf` accepts `report` (`dashboard` / `match_results`),
`scope` (`filters` / `all`), `orientation` (`portrait` / `landscape`),
`filters` (DashboardFilters), `resultFilters` (ResultsQuery), and `sections`.
Omitted/empty sections select all sections for the report. Unknown, duplicate,
or inappropriate sections are rejected. Dashboard section identifiers are
`cover`, `summary`, `demand`, `bench`, `match`, `unmatched`, `extended-other`,
`data-quality-method`; match results require `cover` and `match-results`.
Scope `all` discards both query objects, including paging/sort controls.

The response is HTTP 202 `{ "jobId": ... }`. The existing jobs API exposes progress,
the `PDF report` label and, when completed, a direct attachment link. TaskCards
already uses ordinary anchors, so no frontend changes are required for that link.
`GET /api/v1/reports/{job_id}/file` requires the owner or an Admin login; all other
missing/incomplete/unowned report lookups return 404. Files are atomically replaced
under `<data>/reports/` and cannot be downloaded before job completion.

`data.collect` calls precisely the dashboard summary/demand/bench/match/extended
services and the current-results `matching.list_results` service with the existing
filter models. Current-results pagination and ordering are preserved; an absent
limit is capped at 5000, with a visible truncation note. Section selection affects
presentation, not the filter population. Chart previews show the first 20 numeric
values, followed by complete values tables. The API and PDF share the definitions
in `services/analytics/README.md`, including confirmation proxies and email scope.
Source provenance lists available committed DL/CT uploads; the database does not
retain an exact historical CT-upload-to-match-run association.

Formatting mirrors `frontend/src/lib/format.ts`: Match % is `Math.round(score)`
(nonnegative scores), dates are `DD Mon YYYY`, integral numbers omit `.0`, nulls
are `Not recorded`. Dashboard percentage values retain service precision; only
the roles-without-candidate percentage is fixed to one decimal, as on Dashboard.
Accuracy values remain ratios, not percentages. Contacts and free-text evidence
are always masked, even if an upstream caller supplied unmasked values.

Generation/download use the existing hash-chained audit helper. Generation logs
the effective, email-masked filters, scope, sections, report type, job ID, and
row count (demands in a dashboard report; emitted result rows in match results).
No evidence, filenames, contact details, or report bytes enter audit details.
Report files follow the application's local data-directory lifecycle; this does
not introduce automated retention/deletion policy.

## Offline renderer and packaging

The renderer uses ReportLab PDF shapes, not rasterization, HTML browsers, LLMs,
network calls, or external binaries. Only ReportLab-shipped Bitstream Vera regular
and bold TrueType fonts are embedded. `pdftoppm` is for developer sample review,
not an application dependency.

New locked runtime packages (generated using the packaging README's `uv pip
compile` command with universal SHA-256 hashes):

| Package | Version | License |
| --- | --- | --- |
| reportlab | 5.1.0 | BSD |
| pillow | 13.0.0 | MIT-CMU |
| charset-normalizer | 3.5.2 | MIT |

ReportLab requires Pillow and charset-normalizer. Hashes cover its universal
wheel and the Windows cp313/win_amd64 and Linux wheels of both dependencies.
The Windows ZIP continues to use its existing pinned Python 3.13, binary-only,
`--require-hashes --no-deps` download process.
