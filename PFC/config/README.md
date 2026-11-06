Shipped read-only application defaults and reference data belong here.

Editable domain defaults (REQ-SCR-01, REQ-MAT-05, REQ-ELIG-07):

- `grades.yaml`: ordered grade ladder (D-08).
- `bench_statuses.yaml`: eligibility, priority and evidence flags.
- `status_map.yaml`: source label to canonical status code (D-10).
- `scoring_template.default.json`: versioned baseline scoring settings.
- `skill_synonyms.seed.csv`: approved equivalents and related skills.
- `location_aliases.yaml`: location spelling/remote aliases and travel clusters.

Set `PFC_CONFIG_DIR` to load an edited copy instead of these shipped defaults.

Before building or distributing an Analyst ZIP, provision the OCC Admin's signing
public key in `config/keys/trusted/<key_id>.pub`. An Admin obtains `keyId` and
`publicKeyBase64` from authenticated
`GET /api/v1/occ/settings-package/signing-key` (after the first settings export),
base64-decodes the public key, and saves the raw bytes under that filename.
Never distribute the private key, its passphrase, or `data/keys/private.json`.
`build_zip.py` already copies `PFC/config`, including these read-only trusted keys.
See [keys/trusted/README.md](keys/trusted/README.md) for details.

For related synonyms, CSV `credit` is informational for OCC; scoring uses the
template's `skills.related_synonym_credit` (TDD §7.3, REQ-MAT-05).

Unmapped statuses retain their warning and `unmapped_status_priority` for ranking.
Their binary score is zero; their tiered score follows the mapped priority of
`Overall upskilling (New bench)` and the configured tiered points (REQ-ELIG-07).
If that mapping has no priority, the tiered score uses `unmapped_status_priority`.
