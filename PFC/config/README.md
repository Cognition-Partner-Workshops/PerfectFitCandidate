Shipped read-only application defaults and reference data belong here.

Editable domain defaults (REQ-SCR-01, REQ-MAT-05, REQ-ELIG-07):

- `grades.yaml`: ordered grade ladder (D-08).
- `bench_statuses.yaml`: eligibility, priority and evidence flags.
- `status_map.yaml`: source label to canonical status code (D-10).
- `scoring_template.default.json`: versioned baseline scoring settings.
- `skill_synonyms.seed.csv`: approved equivalents and related skills.
- `location_aliases.yaml`: location spelling/remote aliases and travel clusters.

Set `PFC_CONFIG_DIR` to load an edited copy instead of these shipped defaults.

For related synonyms, CSV `credit` is informational for OCC; scoring uses the
template's `skills.related_synonym_credit` (TDD §7.3, REQ-MAT-05).

Unmapped statuses retain their warning and `unmapped_status_priority` for ranking.
Their binary score is zero; their tiered score follows the mapped priority of
`Overall upskilling (New bench)` and the configured tiered points (REQ-ELIG-07).
If that mapping has no priority, the tiered score uses `unmapped_status_priority`.
