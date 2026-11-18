# Dashboard projections

## Extended panel (`GET /analytics/extended`)

Uses the same Population and dataset-specific filter semantics as the original
panels, including current completed run per demand or explicit runId. Demand and
resource charts remain current-entity views even with runId; email charts retain
the existing all-message/current-entity semantics (not run-scoped; result-only
filters do not apply). Match charts use scored snapshot filters and effective
bands, excluding analyst-excluded pairs except in override-rate denominators.
All ordering is deterministic; ratios with no confirmation data are null rather
than reporting zero accuracy. No new persisted data is required.

* DB-14 gradeMatrix: complete configured grade ladder, demand count × eligible
  resource count in each cell, with exact/adjacent/outside relation. This is
  potential grade supply, not a count of scored matches.
* DB-16 scoreHistogram: [lower, upper) 5-point bins, final bin includes 100;
  allPairs and persisted rank <= 5 counts; thresholds 61/70/90.
* DB-17 shortlistFunnel: distinct matched requisitions, explicitly reviewed /
  shortlisted, approved, sent. Confirmation proxy is an active last pin/include
  (not superseded by unpin), or an approved email for the demand/resource pair.
  The automatic match_results.shortlisted flag is NOT analyst confirmation.
  Approved includes approval timestamps and Approved/Sending/Sent status; Sent
  requires Sent status and a timestamp. Counts are deduplicated per requisition.
* DB-18 unmatchedDemands: open demands without a filtered effective Strong,
  Potential or Avg row. Deterministic reason codes use current eligible grade
  supply and stored mandatory_missing/bands/overrides; unknown explanations are
  `other`, not inferred facts. Dates ascend, missing dates last.
* DB-19 emailTrend: sent-message samples, UTC Monday week of send, hours since
  first DL ingestion start, falling back to selected demand run start. Missing,
  invalid and negative durations are omitted; exactly 24 hours is within target.
  Re-sends are separate samples. Median hours and within24hPct use valid samples.
* DB-20 readinessStatus: active resource counts by CT Group and bench status.
* DB-21 locationMatrix: open demand-region count × eligible resource-location
  count; split locations on slash, comma, semicolon or pipe, deduplicated per
  resource. Potential supply pairs, not matched/aligned pairs.
* DB-22 skillCooccurrence: top 15 confirmed canonical open-demand skills, ordered
  by frequency then name; full matrix, diagonal = demand frequency, each JD once.
* DB-24 upskilling: exact case-insensitive Overall upskilling (New bench) status,
  top-five demanded skills present/missing in CT skills, best filtered score or
  null; descending score then resource ID. Contact fields and embedded emails
  in person names are masked using the shared PII helpers.
* DB-25 accuracy: same explicit-confirmation proxy as funnel, evaluated only on
  confirmed requisitions in each selected contributing run. hits counts confirmed
  pairs ranked <= 5; hitRateAt5 is requisitions with >=1 such hit / confirmed
  requisitions, precisionAt5 is hits / actual available top-five rows for those
  requisitions (not a fixed five denominator). Both ratios are 0..1. Legacy
  pair-level approvals may apply to multiple runs; these are proxies, not an
  independent ground-truth accuracy measurement.
* DB-26 exclusionReasons: distinct comma-separated stored reasons per noneligible
  active resource, fallback `other`; one resource can contribute multiple reasons.
* DB-27 candidateTypes: active resource counts, Internal/External/Unknown order.
* DB-28 contention: resources with Strong/Potential rows for >1 open requisition,
  deduplicated, top 20 by demand count then resource ID; masked person fields.
* DB-29 demandAging: first DL ingestion start (fallback demand.created_at) to app
  date, future dates clamp to zero; buckets [0,7), [7,14), [14,30), [30,60), 60+;
  unknownStart counts invalid/missing starts, pastStartWithoutMatch counts DB-18
  rows with valid start dates strictly before today.
* DB-30 overrideRates: active, nonrevoked overrides in selected filtered pairs,
  including excluded pairs; per run overridden unique pairs / pairCount as
  ratePct (0..100), byType counts actions, not unique pairs. No historical
  inactive runs are silently added to the selected Population.

## Original panels

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
