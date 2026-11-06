# KPI-01: blind shortlist agreement (P1-EX-02)

This offline tool implements [BRD §8.1](BRD.md#81-kpi-01-measurement-method-proposed).
It measures a labelled sample; it does not set Leadership's acceptance targets,
create labels, retrain the scorer, or change a run. Use independently prepared,
blind labels and a pre-agreed tune/holdout partition; holdout is the primary figure.

## Usage

From `PFC/app`, with the backend environment activated:

```sh
PYTHONPATH=. python scripts/measure_kpi01.py --run-id 42 \
  --labels A.csv --labels-b B.csv \
  --out-json kpi01.json --out-md kpi01.md
```

Settings locate `data_dir/db/pfc.db` as usual (`PFC_DATA_DIR`, or optional
`--data-dir`). The database must already exist; the script does not migrate it.
`--labels-b` is optional. Output parent directories must exist. Errors return
exit status 1; invalid CSV/split errors identify the source line without its PII.

```csv
code_requisition,email,split
T100001,confirmed.person@example.com,holdout
T100001,another.person@example.com,holdout
T100002,,tune
```

All three columns are required (column order may differ). There is one row per
confirmed candidate; blank email explicitly means no suitable candidate. Blank
split defaults to `holdout`; only `tune` and `holdout` are valid. Requisition and
split boundary whitespace is trimmed; email cells are preserved verbatim.
Resolve emails by exact casefold comparison against stored
CT `resources.email_address` only: no inferred addresses or substring matching.

## Definitions and accounting

- Only a **completed** run is accepted. Assessed demands are the union of
  `dq_summary_json.demands` and demands with persisted results. Labelled codes
  outside that union are issues and excluded, even if the demand exists in CT/DL.
- **Persisted S:** unique profiles among results with ranks **1–5**, ignoring
  overrides. Duplicate profiles do not backfill from rank 6.
- **After overrides S:** use shared `active_overrides` / `apply_overrides`, keep
  effectively shortlisted, non-excluded results in rank order, deduplicate and
  cap at five. A pin may therefore introduce a result ranked below five.
- Profile identity is `candidate_id`, falling back to `resource_id`. Results
  prefer their persisted candidate ID; legacy results without one use the CT
  resource's candidate ID if available. Label resolution uses current CT identity.
  Multiple resources for the same candidate are one profile, not ambiguous.
- **G:** deduplicated resolved analyst-A profiles. Each unresolved or multi-profile
  ambiguous email is an issue and is ignored. If no real emails resolve, exclude
  that demand; do **not** mistake it for an explicit no-suitable label. Mixed empty
  and real rows ignore the empty rows with an issue. Conflicting splits produce
  an issue and assign that demand to holdout.
- For non-empty G, **hit-rate@5** is the share with at least one intersection;
  **precision@5** is the mean of `hits / |S|`, with precision zero for empty S.
  JSON metrics are fractions, not percentages. A split with no scored demands
  reports `null` (Markdown `—`), rather than asserting zero accuracy.
- Explicit **no-suitable** demands do not enter those denominators. Agreement
  means no Strong/Potential result anywhere in the run for that demand (weak-only,
  Avg-only and no-result demands agree). The effective variant uses effective
  bands and ignores excluded results, but does not disregard an otherwise included
  Strong/Potential row merely because it is not shortlisted.
- Optional **analyst B ceiling** substitutes B's full resolved profile set for S;
  A remains G and defines the evaluation split. No-suitable agreement means B
  explicitly labelled no suitable. Missing/unresolvable B labels exclude that
  comparison, not the system comparisons. B-only demands do not enlarge A's sample;
  B's split conflicts are still reported. B's confirmed set is not capped at five.
- Each split/variant reports labelled, scored, excluded and no-suitable counts,
  including no-suitable agree/disagree. `labelled = scored + excluded + no_suitable`.
  Out-of-run labels count as labelled and excluded, so issues cannot silently shrink
  the sample. There is no combined tune/holdout acceptance number.

## Outputs and privacy

JSON and Markdown include the run ID, template name/version, LLM model, embedding
name/version/SHA, engine version and UTC generation timestamp. The embedding
version is the persisted `embedding_models.pipeline_version` (the schema has no
separate model version field). The service accepts an injectable `now` and reads
only the supplied SQLite connection; it performs no writes or external calls.

Reports contain split/variant metrics, a per-demand table (sizes, hits, precision,
status and no-suitable agreement), and structured data issues. Email issues use
the existing masking helper; neither report contains raw label emails or candidate
names/IDs. Keep source CSVs protected locally; they contain PII. Reports still
contain requisition codes and partially masked addresses and require appropriate
handling. This is backend/CLI tooling only, not a UI or an automatic UAT sign-off.
