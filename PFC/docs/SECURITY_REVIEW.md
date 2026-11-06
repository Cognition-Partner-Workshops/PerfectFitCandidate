# Phase 1f security review — P1-EX-08

## Scope and interpretation

Checklist summary: **26 Pass, 4 Fixed, 9 Deferred** (39 items).

Reviewed the local FastAPI application against TDD §§9.2–9.5 and 13,
REQ-AUTH-02–07, REQ-OUT-03, and the GUI privacy rules. Evidence paths below are
relative to `PFC/app/` unless specified otherwise. Pass means the cited code
and/or automated regression supports that item, not a production security certification.
Fixed means this branch changes the behavior; Deferred means the stated gap remains.

This was a Linux source review with fake/model-free LLM tests, not a native Windows
or browser assessment. No live SMTP/provider service, real llama-server model, or
SonarQube SAST scan was run. SonarQube is the enterprise SAST standard; these Python
checks are not a substitute for it. The parked quality-check branch was not changed.

## Checklist

| Item | Requirement/REQ ID | Status (Pass/Fixed/Deferred) | Evidence (file:line or test name) | Notes |
| --- | --- | --- | --- | --- |
| A1 Argon2id profile | REQ-AUTH-03/04; TDD §9.2 | Pass | `pfc/security/passwords.py:10`; `test_auth_defaults_and_cookie_rotation` | Argon2id, 65,536 KiB memory, time cost 3, parallelism 4. Tests use cheaper hashes but assert production defaults separately. |
| A2 Lockout | REQ-AUTH-04 | Pass | `pfc/api/routers/auth.py:99`; `tests/integration/test_auth.py::test_lockout` | Five consecutive failed attempts, fifteen-minute lockout; successful login resets failures. |
| A3 Session cookie flags | REQ-AUTH-04; TDD §9.3 | Pass | `pfc/api/routers/auth.py:36`; `test_auth_defaults_and_cookie_rotation` | HttpOnly, SameSite=strict, Path=/, no Domain attribute (host-only), no persistent Max-Age. |
| A4 Secure-cookie decision | REQ-AUTH-07; TDD §9.3 | Pass | `pfc/api/routers/auth.py:36`; `pfc/launcher.py:91` | Secure intentionally omitted for HTTP loopback, as the TDD specifies. This is not an HTTPS deployment configuration. |
| A5 Idle and absolute expiry | REQ-AUTH-04 | Pass | `pfc/security/middleware.py:39`; `pfc/security/sessions.py:24`; `tests/integration/test_auth.py::test_expiry` | Default idle 60 minutes; absolute 12 hours. Session touching is capped at the absolute deadline; expiry is checked server-side. |
| A6 Login rotation and token storage | REQ-AUTH-04 | Pass | `pfc/security/sessions.py:24`; `test_auth_defaults_and_cookie_rotation` | Each login issues a fresh 32-byte random session token. Session and CSRF hashes, not bearer tokens, are stored. Existing other sessions are not revoked merely by logging in again. |
| A7 Password-change rotation | REQ-AUTH-04 | Pass | `pfc/api/routers/auth.py:170`; `test_auth_defaults_and_cookie_rotation` | Revokes all prior sessions and issues a new session/CSRF pair. Regression verifies both older login cookies fail afterward. |
| A8 Privilege changes | REQ-AUTH-04; REQ-ROLE-03 | Pass | `pfc/services/occ/users.py:96`; `tests/integration/test_occ.py::test_unchanged_role_preserves_session` | Actual role changes or deactivation revoke all sessions, requiring fresh authentication; unchanged-role edits do not. Password reset also revokes sessions and forces password change. |
| A9 Mandatory password change | REQ-AUTH-02 | Pass | `test_must_change_covers_every_api_route`; `pfc/security/middleware.py:49` | Enumerates all defined HTTP operations, including documentation routes and lazy included routers. Only health, setup/status, auth/me, auth/password and auth/logout remain accessible. Public endpoints are the first two. |
| C1 CSRF coverage | REQ-AUTH-04/07; TDD §9.3 | Pass | `test_all_api_writes_require_csrf` | Enumerates app.routes and included effective routes; every authenticated POST/PUT/PATCH/DELETE gets 403 CSRF_INVALID without a token, before payload validation. No authenticated exemptions. Login and setup/admin work without CSRF only before a session exists. |
| L1 Application binding | REQ-AUTH-07; TDD §13 | Pass | `pfc/config.py:32`; `pfc/launcher.py:156`; `tests/unit/test_launcher.py::test_launch` | Settings rejects non-loopback hosts; launcher explicitly binds 127.0.0.1 with one worker, including port fallback. |
| L2 Host and Origin | REQ-AUTH-07 | Pass | `pfc/security/middleware.py:23`; `tests/integration/test_auth.py::test_transport_and_problems` | Exact Host allowlist, including port. A supplied Origin must equal scheme://Host. Origin absence is allowed for local tools; it does not bypass write CSRF. |
| L3 Extra allowed_hosts | REQ-AUTH-07 | Pass | `pfc/config.py:22`; `pfc/security/middleware.py:25` | Default is empty; no wildcard expansion. Explicit additional hosts are trusted operator configuration, primarily for tests, not permission to expose the listener. Production should leave this empty. |
| L4 Inference binding and key | REQ-AUTH-07; REQ-LLM-01 | Pass | `pfc/adapters/llm/supervisor.py:278`; `tests/unit/adapters/llm/test_supervisor.py::test_api_key_passed_only_in_environment` | Child binds 127.0.0.1; key goes in LLAMA_API_KEY and LLAMA_ARG_API_KEY, never argv. Supervisor health/provider URLs are constructed from the selected loopback port. |
| L5 Provider URL protection | REQ-AUTH-07 | Pass | `pfc/adapters/llm/providers.py:116` | Only literal 127.0.0.1, localhost and ::1 hostnames are accepted; httpx trust_env=False prevents proxy redirection. |
| U1 Login success/failure/lockout audits | REQ-AUTH-05 | Pass | `pfc/api/routers/auth.py:99`, `:109`, `:129`; `test_auth_setup_unmask_logs_omit_pii` | LOGIN_FAIL, LOCKOUT, LOGIN_OK, with actor where known; no submitted credentials in audit details. SETUP_ADMIN is also recorded. |
| U2 Unlock and password audits | REQ-AUTH-05 | Pass | `pfc/services/occ/users.py:107`, `:116`; `pfc/api/routers/auth.py:182` | USER_UNLOCK, USER_RESET_PASSWORD and PASSWORD_CHANGE. OCC user changes use field-name-only audit snapshots, not credential/identity values. |
| U3 Unmask audit | REQ-AUTH-05; REQ-OUT-03 | Pass | `pfc/services/pii.py:50`; `tests/integration/test_pii.py::test_unmask_and_audit` | Successful UNMASK stores actor, entity/id and field only; never the unmasked value. Denied attempts intentionally have no UNMASK audit. |
| U4 OCC/settings audits | REQ-AUTH-05 | Pass | `pfc/services/occ/common.py:14`; `pfc/services/occ/admin.py:42`, `:95`, `:153`; `test_secret_settings_encrypted_and_write_only` | OCC_EDIT and SETTING_CHANGE use the common audit writer. Secret setting values are redacted, including nested credential objects. |
| U5 Package import/rollback audits | REQ-AUTH-05 | Pass | `pfc/services/packages/importer.py:116`, `:175` | settings_package.import and settings_package.rollback record package/version metadata, not provider token values. |
| U6 Email approval/send audits | REQ-AUTH-05; REQ-EML-01 | Pass | `pfc/services/email/service.py:615`, `:983`, `:1085`, `:1102` | EMAIL_APPROVE, EMAIL_SEND, EMAIL_SENT and reconciliation/failure events are present. Email services were inspected, not modified. |
| P1 Resource default masking | REQ-OUT-03 | Pass | `pfc/services/ingestion/browse.py:72`; `tests/integration/test_ingestion.py::test_req_out_03_resource_sort_mask_flags_and_detail` | Resource list/detail do not expose raw email; no raw phone field is returned. Pattern discrepancy is tracked separately below. |
| P2 Result default masking | REQ-OUT-01/03 | Pass | `pfc/services/matching/results.py:45`; `pfc/api/schemas/results.py:142` | Email local-part is masked; person_email_masked immediately follows person_name. No phone field in result rows. Masking is presentation, not encryption. |
| P3 Candidate response coverage | REQ-OUT-03 | Deferred | `tests/contract/test_frontend_api_contract.py` PENDING; `pfc/db/migrations/0001_initial.sql:174` | Candidate profile/list endpoints remain unimplemented; candidates.phone and candidate_emails.email exist in storage and audited unmask is implemented. Cannot certify nonexistent default candidate responses. Require masking tests when those endpoints land. |
| P4 OCC user-list email policy | REQ-OUT-03; GUI_OCC §16.2 | Deferred | `pfc/services/occ/users.py:16`; `pfc/api/routers/occ.py` Admin dependency | Admin user listing excludes password_hash but returns raw corporate email. GUI_OCC §16.2 explicitly lists email as a table column, while general privacy rules say mask by default. Decision needed on wire masking versus presentation masking; did not silently change identity-management responses. |
| P5 Resource mask pattern | REQ-OUT-03; GUI_OCC §16.14 | Deferred | `pfc/services/ingestion/browse.py:72`; `pfc/services/matching/results.py:45` | Resource helper uses first character + three stars; results use keep-two/mask-up-to-six. Both mask, but the resource pattern does not match the baseline keep-two/mask-six rule. Align with the owning ingestion/UI work; no masking contract change in this audit. |
| G1 Exception payload logging | REQ-AUTH-06; privacy | Fixed | `pfc/jobs/runner.py:137`; `pfc/security/problems.py:49`; `pfc/main.py:59`; `pfc/db/connection.py:23`; `test_job_exception_payload_not_logged` | Removed exception/traceback interpolation. A reachable real job handler throwing synthetic email/password text reproduced the leak before the fix. Stable error messages and job IDs remain. |
| G2 Sensitive-flow logs | REQ-AUTH-05/06 | Pass | `test_auth_setup_unmask_logs_omit_pii` | DEBUG capture covers setup, successful login, failed login and unmask; checks email, both passwords and setup code absent. Audit action assertions prove each intended path executed. |
| G3 HTTP query logging | REQ-AUTH-06; privacy | Fixed | `pfc/launcher.py:156`; `tests/unit/test_launcher.py::test_launch` | Disables uvicorn access logging in the supported launcher so query strings cannot echo PII. Custom direct uvicorn invocations must likewise disable access logs; disabling them does not disable application security audits. |
| G4 Seed path logging | REQ-AUTH-06; privacy | Fixed | `pfc/services/occ/seed.py:159`, `:172`, `:183`, `:204`; `test_seed_logs_do_not_echo_profile_paths` | Logs only fixed seed/template basenames, not profile paths that can contain an email address. Missing/malformed file diagnostics are retained. |
| G5 Setup console output | TDD §9.1/13; privacy | Deferred | `pfc/launcher.py:144`; `pfc/security/setup.py` | Bootstrap code is intentionally printed to the launch console and saved in run/setup-code.txt per TDD; it is not sent to the application logger. If captured console stdout counts as forbidden logging, decide an alternative bootstrap channel and retention policy. Do not claim the code never appears on stdout. |
| G6 Child process log | REQ-AUTH-06; REQ-LLM-01 | Deferred | `pfc/adapters/llm/supervisor.py:298` | Raw llama-server stdout/stderr append to logs/llm.log. Parent logs do not expose its key, but real pinned-binary startup/error output was not exercised. Need native/runtime log inspection before certifying child output or deciding on redaction. |
| S1 DPAPI and development fallback | REQ-AUTH-06; TDD §9.5 | Pass | `pfc/services/packages/keys.py:97`; `pfc/security/secrets_store.py:44`; `tests/unit/security/test_secrets_store.py` | Existing package DPAPI protects signing material, not arbitrary credentials. New credential store uses current-user ctypes DPAPI with app-specific entropy/UI forbidden; non-Windows development/tests use Fernet with a persistent 0600 file key. No Windows failure silently falls back to a file key. Native validation is separate below. |
| S2 Existing secret-setting writer | REQ-AUTH-06; TDD §9.5 | Fixed | `pfc/services/occ/admin.py:106`, `:121`; `test_secret_settings_encrypted_and_write_only` | Generic OCC settings was a reachable plaintext credential writer (existing smtp.password test). Credential-looking top-level/nested keys are now encrypted, API reads are write-only [REDACTED], and resubmitting that marker preserves the existing secret. No provider or SMTP integration was invented. |
| S3 Secret error handling | REQ-AUTH-06 | Pass | `test_secret_setting_failure_is_atomic`; `test_decryption_fails_closed` | Empty updates are no-ops; credential-storage errors roll back setting/audit writes and return generic 503. Wrong key, tampered ciphertext, missing key and mismatched platform formats fail closed. |
| S4 Existing plaintext and schema-less settings | REQ-AUTH-06 | Fixed | `pfc/security/credentials.py`; `pfc/main.py` lifespan; `tests/integration/test_credential_migration.py` | `CREDENTIAL_SETTING_KEYS` is a typed registry (`Literal`) of the credential setting keys in use today (`smtp.password`); each is also matched by `sensitive_key`, and no new heuristic was added. A one-time, idempotent startup migration encrypts plaintext values under those keys with the credential store and audits each as `SETTING_CHANGE` with `[plaintext]` → `[encrypted]` markers, never values. It then runs `PRAGMA wal_checkpoint(TRUNCATE)`, `VACUUM` and another checkpoint, and the test proves the plaintext bytes are gone from the DB and WAL files. Pre-migration backups under `backups/` can still hold old plaintext; rotate or delete them under the backup/retention policy. Secrets stored under keys outside the registry are still caught by `sensitive_key` on write; add new credential keys to the registry. |
| S5 Provider tokens / SMTP integration | REQ-AUTH-06; REQ-LLM-03; REQ-EML-04 | Deferred (Phase 3) | `pfc/db/migrations/0001_initial.sql:566`; `pfc/services/packages/payloads.py:375` | Stays deferred to Phase 3, when external providers land; SMTP is Phase 2. token_blob is documented as encrypted but no active provider-token writer consumes a vault yet; package import preserves existing blobs without proving their encryption. Wire these callers explicitly to the credential store, and add their setting keys to `CREDENTIAL_SETTING_KEYS`, when implemented. |
| S6 Data-directory / DB / CV protection | REQ-AUTH-06; TDD §9.5, Q-11 | Fixed (native evidence: UAT) | `pfc/config.py` `default_data_dir`; `pfc/security/data_dir.py`; `pfc/launcher.py`; `pfc/api/routers/health.py`; `tests/unit/security/test_data_at_rest.py` | Q-11 resolved: BitLocker plus field-level encryption, no SQLCipher. The default data_dir is now `%LOCALAPPDATA%\PFC\data` on Windows (`$XDG_DATA_HOME/PFC/data` or `~/.local/share/PFC/data` elsewhere), not package-relative; `PFC_DATA_DIR` still overrides. On first start, an old package-relative `data/` that holds a database (and whose old instance is not running, checked via its `run/pfc.lock`) is copied, verified (SHA-256 per file, row counts per table for the backup-API copy of the DB) and the original renamed to `data.migrated`, never deleted; logs carry basenames only. On Windows the launcher runs `icacls` (argv lists, no shell, no admin): `/reset /T` drops explicit grants to other accounts, then only the current user and SYSTEM are granted; elsewhere the dir is `chmod 0700`. Failure warns and start-up continues. BitLocker status is read from the Shell `System.Volume.BitLockerProtection` property and shown, informational only, in `/health` details and on the OCC overview as Encrypted / Not encrypted / Unknown; it never blocks start-up. CI uses fakes; real icacls/BitLocker evidence is a UAT item (S7). SQLite/CV/upload data are not application-encrypted. |
| S7 Native Windows verification | REQ-AUTH-06/07; TDD §13 | Deferred | `pfc/security/secrets_store.py:44`; `pfc/adapters/llm/supervisor.py:40` | DPAPI protect/unprotect under the real signed-in user, Job Object orphan cleanup, file ACLs and BitLocker require Windows hardware/runtime. Mock dispatch and Linux tests are not native evidence. |

## Changes, boundaries and decisions

Three focused fixes were made: (1) exception/access-log hygiene plus security route
regressions, (2) encrypted/write-only credential settings, and (3) seed-path log
redaction. Small cross-owner edits touch main, database logging, job runner, launcher
and OCC settings/seeding. Email service, ingestion/matching response contracts,
shared build/CI/type configuration, and the parked quality branch were not changed.

The credential format is a versioned opaque string stored inside settings.value_json
(not a database schema migration). DPAPI blobs are base64 encoded; the development
fallback uses authenticated Fernet encryption. Keep the fallback key with secured
backups; loss of the key loses access. No API can retrieve plaintext credential
values. Secret-name detection includes password/secret/token/credential/api_key/apikey
and nested objects/lists; it is not a substitute for a future typed credential schema.

Decisions needed: retention of pre-migration backups that may hold old plaintext;
future SMTP/provider callers (S5, Phase 3); OCC user email/presentation policy;
resource mask-pattern ownership; setup console handling; child-log verification;
native Windows ACL and BitLocker evidence (UAT). No claim is made that
the complete sensitive local database is encrypted.

## Automated verification

From `PFC/app`, using the repository virtualenv's ruff/mypy/pytest executables:

```text
ruff check pfc tests
ruff format --check pfc tests
mypy pfc
pytest -q
```

Security-specific regressions are in `tests/integration/test_security_review.py`
and `tests/unit/security/test_secrets_store.py`; the full suite also exercises
existing authentication, OCC, PII, package, matching, email and supervisor behavior.
The API enumeration handles both flat APIRoutes and lazy effective included routes
without relying only on OpenAPI output or a hard-coded list of write endpoints.
All defined HTTP methods are enumerated for mandatory-password-change enforcement; the
CSRF test enumerates all four state-changing verbs and checks the problem code.

## OCC instance-default follow-up

The existing `PUT /api/v1/occ/llm-models/{id}` additionally accepts
`{"isDefault": true}`, optionally with `approved`. GET and PUT return model lists
with the frontend LlmModel fields (including isDefault derived from is_active);
legacy metadata fields remain available. Personal llm.active_model settings are
not updated.

The existing BEGIN IMMEDIATE transaction clears the old active flag, sets the new
one, and writes OCC_EDIT with before/after default_model_id. Clearing the old flag
first respects the database's unique active-model index; clients never observe the
intermediate state. Approval and default changes in one request are atomic.

Choices:

- Unapproved targets require approval in the same request, or get 422.
- Missing/non-GGUF filenames, absolute/traversing paths, and identifiable
  embedding models get 422. The schema lacks an authoritative purpose column:
  embedding identification currently uses embed-named models and ONNX filenames,
  not binary GGUF inspection.
- isDefault=false, explicit nulls and an empty patch get 422.
- Unapproving the current default retains the existing 409 MODEL_ACTIVE guard.
  Select a different default first; a simultaneous unapprove/make-default
  contradiction gets 422.
- After commit, the restart helper is queued only when resolve_model differs from
  the supervisor's current model name/path. An unchanged personal selection that
  still wins does not restart the shared server or reset its recovery budget.

Tests: `test_occ_make_default_transaction_audit_and_wire`,
`test_occ_default_preserves_personal_selection`,
`test_occ_invalid_default_no_changes`, and
`test_occ_default_auth_and_combined_approval` in
`tests/integration/test_occ.py`. These cover single-default uniqueness,
wire fields, audit integrity, personal-row preservation, restart/no-restart,
combined approval, invalid targets/patches, authentication, Analyst denial and CSRF.
This backend follow-up is not browser/visually verified.

Final local check output:

```text
All checks passed!
157 files already formatted
Success: no issues found in 117 source files
973 passed, 1 skipped in 48.37s
```
