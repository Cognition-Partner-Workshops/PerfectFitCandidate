# Phase 1 MVP release notes — readiness draft

**Reviewed baseline:** merged main `6363f49` (2026-11-06), including #37.
This is an implementation inventory, **not a release approval or UAT pass**.
[PHASE1_TRACEABILITY.md](PHASE1_TRACEABILITY.md) provides requirement-by-requirement
merged PR, code/test evidence, status and remaining work.

## Available MVP implementation

- Local FastAPI/React application: first-admin setup, Admin/Analyst roles,
  password policy, lockout/session expiry, CSRF/loopback controls and audit events.
- Manual DL/CT workbook upload, header detection, preview/diff/commit,
  replacement/history, and OCC approve/modify/revert queue.
- Configurable resume folder, per-person/selected Sync, PDF/DOCX text extraction,
  CV versioning, identity review, candidate detail and CV-status links. Structured
  CV extraction is incomplete, as detailed below.
- Deterministic hybrid scoring, seeded synonyms, cached local ONNX embeddings,
  explicit JD analyze/review/edit/confirm workflow (rule-based by default),
  grade/eligibility/location checks, ranked profile shortlists, component evidence,
  persisted run metadata and audited overrides.
- Demand/bench/match dashboards, filters, run exclusions/reasons, validation panel,
  masked CSV/XLSX exports and permission-controlled/audited verbatim-email export.
- OCC reference-data CRUD, versioned scoring/email templates, users, approved-model
  list, instance-default/personal selection, signed settings packages and model
  add-ons/re-embedding. Status/synonym edits are not yet consumed by matching.
- Editable per-profile email drafts, top-four suggestion, explicit approval,
  send/failure state and duplicate-send protection; Outlook COM implementation.
- [KPI-01 CLI](KPI01.md): holdout/tune metrics, persisted and after-overrides
  shortlist variants, second analyst and masked issue reports. It does not supply
  analyst labels or establish achieved accuracy.

## Deliverable inventory — PHASES §5.4

| Deliverable | Evidence / readiness |
|---|---|
| Portable ZIP and release notes | Dispatch/tag model-complete build and offline standard-user smoke exist in `../packaging/`; execution evidence is tracked under REQ-PKG-02. Target-laptop acceptance remains open. These are draft release notes. |
| Default assets | `../templates/` and `../config/` contain the default email template, scoring-template v1, synonyms and status priorities/mapping; seed/config/CRUD tests are cited in the matrix. |
| Analyst and OCC guides | [USER_GUIDE_ANALYST.md](USER_GUIDE_ANALYST.md), [ADMIN_GUIDE_OCC.md](ADMIN_GUIDE_OCC.md). Some Pending notes predate merged fixes. |
| UAT report and KPI baselines | Not supplied by this inventory. Real KPI-01 labels/holdout measurement and KPI-02–05 baselines are required. |
| As-built designs and decisions | [FDD.md](FDD.md), [TDD.md](TDD.md), [GUI_OCC_DESIGN.md](GUI_OCC_DESIGN.md) exist. Full as-built reconciliation and explicit closure evidence for O-03/O-04/O-05/O-07 are not certified here. |

## Installing the model-complete portable ZIP

Use `pfc-windows-x64-with-models` from a manual/tag packaging run, not the
model-free PR artifact. Extract it to a user-writable folder and start `PFC.cmd`.
Embedded Python, CPU llama.cpp, the built frontend, and every GGUF/ONNX/tokenizer
file pinned in `MODELS.lock` are included; runtime installation/downloads are not
required. Complete the local first-admin setup and explicitly approve/select model
candidates. Bundling a model does not silently approve it.

See [packaging instructions](../packaging/README.md) for dispatch, hash verification,
cache behavior and offline-smoke details. The Windows CI smoke uses a disposable
standard account and synthetic re-embed data. It explicitly starts the locked GGUF
with a small context for a tiny generation; production approval defaults remain
unchanged. GitHub's `windows-latest` is not the locked-down corporate laptop.

## Known limitations and release gates

All Partial and UAT-only requirements below remain visible release work. None is
silently accepted/deferred by these notes. The matrix has no Not started rows.

- **REQ-IN-21/23, REQ-MAT-02/06 and Phase 1 REQ-LLM-02:** resume sync persists
  normalized text and email facts, not the complete structured contact/experience/
  qualifications/projects/evidence model. Default JD analysis keeps an empty LLM
  matching port and falls back to rules. A running email-drafting provider does
  not wire local-LLM JD/CV extraction or establish grounded extraction quality.
- **REQ-ELIG-03/07 and REQ-MAT-05:** matching calls file-based
  `load_domain_config`; OCC status/mapping/synonym writers update SQLite. Default
  behavior and editing are tested separately, not end-to-end propagation. Active
  scoring templates **are** loaded from SQLite; this is not a claim that all OCC
  settings are ignored.
- **REQ-MAT-07:** CV/vector reuse exists, but matching exposes full/demand scopes
  rather than dirty-pair incremental scoring/copy-forward. No verified OCC
  cached-CV-removal route was found.
- **REQ-CLS-02:** classification fields/display and domain-preview counts exist;
  domain changes do not implement candidate reclassification or an audited
  classification-override workflow.
- **REQ-OUT-03:** resources/results use different mask patterns; OCC user-list
  email policy needs a decision. Candidate detail routes now exist (#18), making
  the older security-review statement that they are absent stale.
- **REQ-AUTH-06:** encrypted/write-only credential settings are not whole-DB or
  CV-cache encryption. Legacy plaintext needs resave/migration; native DPAPI/
  storage controls, real child logs and residual decisions remain in
  [SECURITY_REVIEW.md](SECURITY_REVIEW.md). That review did not perform enterprise
  SonarQube SAST.
- **REQ-PKG-02:** PR packages remain model-free. Dispatch/tag packaging builds a
  separate model-complete artifact and runs an offline standard-user Windows smoke.
  [Dispatch 42203458140 passed](https://github.com/Cognition-Partner-Workshops/PerfectFitCandidate/actions/runs/42203458140)
  at implementation `e6596e1`: ZIP 2,718,697,267 bytes, startup 0.548344 s, sampled
  aggregate peak RSS 4,716,417,024 bytes. This is Windows CI, not target-laptop UAT.
- **REQ-LLM-01, REQ-PKG-01 and Phase 1 REQ-EML-04:** real bundled GGUF quality/RAM,
  locked-down launch, and corporate Outlook approval/send/reconciliation need
  Phase 0 laptop UAT. **REQ-OUT-11** professional-look acceptance is also UAT-only;
  no visual check was performed for this document.
- **Linux CI/VM, not target laptop:** [PERFORMANCE.md](PERFORMANCE.md) records
  500 × 500 matching at 25.070 s VM / 43.365 s GitHub; optimized API medians are
  0.054–0.557 s VM / 0.076–0.810 s GitHub. These use synthetic cached vectors;
  separate real ONNX validation is also Linux-only. No real-laptop timing,
  browser-interaction acceptance is established. Offline model-complete first run
  is now separately evidenced on hosted Windows above, not on the target laptop.
  **P1-EX-06/07 remain open**.
- **Masked-snapshot email check — REQ-OUT-10 / P1-EX-04:** the panel compares the
  persisted masked scoring email exactly and case-sensitively to the mask of
  current `resources.email_address`; legacy rows use live CT serialization.
  Historical raw email bytes are not stored. A pass cannot prove equality of
  hidden characters or raw historical byte-for-byte equality. Ingestion/export
  have separate verbatim-email tests; the panel is not a substitute for UAT
  source checks.
- **P1-EX-01/02/03/09:** requirement UAT, representative blind KPI-01 labels,
  holdout measurement/leadership target, real KPI baselines, and the **2–4 week,
  ~5-user pilot** with Phase 2 go decision remain outstanding.

PDF reports, auto-watch pickup, live external providers, model-benchmark lab,
calendar follow-ups and archival/housekeeping are later-phase work in
[PHASES §10](PHASES.md#10-requirement-traceability-req-id--phase), not features
implicitly supplied by Phase 1 placeholders.

## Installation and first run

**Sources:** the Admin guide exists and §2 supplies the first-run sequence. It
does not describe ZIP extraction/upgrade; those steps come from
[packaging/README.md](../packaging/README.md), checked against
`../packaging/build_zip.py` and the launcher. The stale “documentation only”
application README is not used as installation authority.

1. Obtain the approved Windows x64 portable ZIP from the release owner. Confirm
   its approved local model/tokenizer assets: the default build omits them.
   The documented `--with-models` option is a **release-builder** action requiring
   downloads, not an analyst installation step.
2. Extract to a user-writable location. The package provides embedded Python,
   vendored dependencies, llama.cpp, web assets and `PFC.cmd`; no Python/Ollama
   installer should be needed. Corporate execution-policy/AV/EDR approval is
   still required; target-laptop approval is not claimed.
3. For upgrades, extract alongside the existing package and preserve/reuse or copy
   the old `data/` directory. Do not replace it with an empty folder.
4. Run `PFC.cmd`; the launcher starts the loopback app and opens the browser.
   A new data folder opens first-run setup.
5. Follow [Admin guide §2](ADMIN_GUIDE_OCC.md#2-first-run-setup): retrieve the
   **one-time setup code** from the console or `data\run\setup-code.txt`. Enter
   full name, corporate email, password and confirmation. Meet the complete
   checklist (at least 14 characters, character types, no name/email/common password).
6. Choose **Create admin**, optionally confirm Data folder and Resume folder path,
   then **Next**. Review the summary and choose **Go to sign in**. Setup does not
   reappear unless the data folder is reset.
7. Sign in as **Admin**. Follow the guide to configure resource team/templates/
   reference data, create Analysts with one-time passwords, approve/select local
   models and distribute signed settings packages. Analysts import in Settings
   and must change their one-time password. Validate on the corporate laptop
   before using real candidate data.

The Admin guide's Pending DL/CT backend routes are superseded by #18; global model
default by #23; sign-in lockout messaging by #31. Those fixes do not certify the
remaining signing-key screen/security/installation checks. The guides are linked,
not rewritten by this task.

## Validation record

This is a docs-only review of merged code and existing test assertions. No backend,
native laptop, real Outlook/LLM, browser or UAT run was performed for these notes.
Neither existing CI nor this draft opens the release gate.
