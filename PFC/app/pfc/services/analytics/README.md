# Dashboard projections

All four endpoints accept every DashboardFilters key and optional runId.
Filters that do not apply to a population are ignored.

| Population | Applied filters |
| --- | --- |
| Open demands | codeRequisition (substring), accountName, bu, grade (demand grade), region |
| Active resources | grade (person grade), location, status, candidateType, incompleteIdentity |
| Effective match results | All DashboardFilters keys; grade matches demand OR person grade |
| Email messages | Demand and person filters on current entities (including closed/off-bench); result-only band, mandatorySkills and score filters do not apply |
| Latest-ingestion quality counts | No entity filters: issue rows have no demand/resource FK |
| Run metadata counters | runId only; requisitionsAssessed and roleToPersonCombinations retain the selected run's stored counters |

Text comparisons are case-insensitive; other than requisition substring matching,
they are exact. Result filters use scored display facts and effective overrides.
Demand skill panels use the current version's confirmed revision, canonical IDs
when available, and the domain synonym dictionary otherwise. Person skill panels
use CT skills, not CV skills. Supply counts distinct resource rows.

The latest completed run is selected by descending run ID. An explicit missing
run returns 404. With no run, match datasets and validation are empty. Summary
communication and ingestion counters remain independent of run availability.

Demand rows expose latest revision status; confirmed demands assessed in the run
are Scored. Match counts and best bands use filtered, effective, nonexcluded rows.
Missing candidate IDs use 0; missing identity uses incomplete; missing candidate
classification uses Unknown. CV count uses stored versions; CV status is Synced
when any versions exist, otherwise Not synced.

Available Capacity Y/Yes is normalised case-insensitively, with whitespace trimmed.
Every other value contributes to supplyN; resource rows expose null for blanks
and N for other non-Y values. Available Capacity is never a matching filter.
Status-map exclusions are independent of stored person eligibility.

The schema calls ingestion upload time started_at, not created_at. Email
turnaround uses the demand's first ingestion's started_at; timestamps from zero
through exactly 24 hours pass, negative elapsed time does not. Approved messages
without a send/upload time remain in the denominator and fail the numerator.
Quality counts use the latest committed DL and CT runs ordered by committed_at,
then ingestion ID, counting warning/error issue instances rather than people.

Missing bench starts do not contribute to timelines or aging. Negative bench
durations clamp to zero. Aging upper bounds are exclusive: 14, 28, 56, 84 days.
Start pipeline puts all dates before the app-clock date in Overdue; other labels
are YYYY-MM. Unknown grades do not add a new ladder bucket.

Validation uses persisted grade and bench-end component inputs, failing closed
when these are missing. Email presence explicitly checks current CT, not a
historical byte-for-byte comparison. Empty result sets pass universal assertions;
the Available Capacity structural assertion is the sole constant check.
