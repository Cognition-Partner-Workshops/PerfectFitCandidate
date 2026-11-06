CREATE TABLE roles (
  role_id      INTEGER PRIMARY KEY,
  name         TEXT NOT NULL UNIQUE CHECK (name IN ('Admin','Analyst'))   -- REQ-ROLE-01, D-05
);

CREATE TABLE users (
  user_id              INTEGER PRIMARY KEY,
  email                TEXT NOT NULL UNIQUE COLLATE NOCASE,              -- corporate email (REQ-AUTH-02)
  display_name         TEXT NOT NULL,
  role_id              INTEGER NOT NULL REFERENCES roles(role_id),
  persona              TEXT CHECK (persona IN ('A','B','C')),            -- Analyst persona, informational only (REQ-ROLE-02)
  password_hash        TEXT NOT NULL,                                    -- Argon2id encoded string (REQ-AUTH-04)
  must_change_password INTEGER NOT NULL DEFAULT 1,
  failed_attempts      INTEGER NOT NULL DEFAULT 0,
  locked_until         TEXT,
  is_active            INTEGER NOT NULL DEFAULT 1,
  created_at           TEXT NOT NULL,
  updated_at           TEXT NOT NULL,
  last_login_at        TEXT
);

CREATE TABLE sessions (
  session_id      INTEGER PRIMARY KEY,
  token_hash      TEXT NOT NULL UNIQUE,           -- SHA-256 of the random cookie token; raw token never stored
  csrf_hash       TEXT NOT NULL,
  user_id         INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
  role_at_login   TEXT NOT NULL,                  -- role chosen in the login dropdown (REQ-AUTH-01)
  created_at      TEXT NOT NULL,
  last_seen_at    TEXT NOT NULL,
  expires_at      TEXT NOT NULL,
  revoked_at      TEXT
);
CREATE INDEX ix_sessions_user ON sessions(user_id);
CREATE INDEX ix_sessions_expires ON sessions(expires_at);


CREATE TABLE ingestion_runs (
  ingestion_run_id INTEGER PRIMARY KEY,
  kind             TEXT NOT NULL CHECK (kind IN ('DL','CT','CV')),
  source           TEXT NOT NULL CHECK (source IN ('manual_upload','watch_folder','resume_folder')),  -- watch_folder = P2 (REQ-ING-04)
  file_name        TEXT NOT NULL,
  file_sha256      TEXT NOT NULL,
  file_size        INTEGER NOT NULL,
  sheet_name       TEXT,                            -- detected sheet (REQ-IN-01)
  header_row       INTEGER,                         -- detected header row (REQ-IN-10)
  uploaded_by      INTEGER REFERENCES users(user_id),
  status           TEXT NOT NULL CHECK (status IN ('preview','committed','no_op','discarded','failed')),
  rows_read        INTEGER, rows_new INTEGER, rows_updated INTEGER, rows_unchanged INTEGER, rows_rejected INTEGER,
  started_at       TEXT NOT NULL,
  committed_at     TEXT,
  error_summary    TEXT
);
CREATE INDEX ix_ingestion_runs_hash ON ingestion_runs(kind, file_sha256);   -- identical re-upload => no_op (REQ-ING-06)

CREATE TABLE ingestion_row_issues (
  issue_id         INTEGER PRIMARY KEY,
  ingestion_run_id INTEGER NOT NULL REFERENCES ingestion_runs(ingestion_run_id) ON DELETE CASCADE,
  row_number       INTEGER NOT NULL,
  field            TEXT,
  severity         TEXT NOT NULL CHECK (severity IN ('info','warning','error')),
  code             TEXT NOT NULL,          -- e.g. REQ_ID_FORMAT, DATE_UNPARSEABLE, EMAIL_MISSING, DUPLICATE_IN_FILE
  message          TEXT NOT NULL,
  raw_value        TEXT
);
CREATE INDEX ix_row_issues_run ON ingestion_row_issues(ingestion_run_id);


CREATE TABLE demands (
  demand_id            INTEGER PRIMARY KEY,
  code_requisition     TEXT NOT NULL UNIQUE,         -- exact source value; business key (REQ-IN-02)
  id_format_ok         INTEGER NOT NULL,             -- T1XXXX/T2XXXX check, warning only (O-02)
  position_start_date  TEXT,
  account_name         TEXT,
  code_local_grade     TEXT REFERENCES grades(code),
  demand_request_name  TEXT,                         -- role title
  region               TEXT,                         -- role location; 'Unspecified' rendered when NULL (REQ-OUT-01)
  bu                   TEXT,
  owner                TEXT,
  current_version_id   INTEGER REFERENCES demand_versions(demand_version_id),
  lifecycle            TEXT NOT NULL DEFAULT 'open' CHECK (lifecycle IN ('open','closed','archived')),
  row_hash             TEXT NOT NULL,                -- hash of normalised row; drives updated/unchanged
  first_ingestion_id   INTEGER REFERENCES ingestion_runs(ingestion_run_id),
  last_ingestion_id    INTEGER REFERENCES ingestion_runs(ingestion_run_id),
  occ_reviewed_by      INTEGER REFERENCES users(user_id),   -- OCC approve/modify (REQ-ING-03)
  occ_reviewed_at      TEXT,
  created_at           TEXT NOT NULL,
  updated_at           TEXT NOT NULL
);
CREATE INDEX ix_demands_lifecycle ON demands(lifecycle);
CREATE INDEX ix_demands_filters ON demands(bu, region, account_name, code_local_grade);
CREATE INDEX ix_demands_start ON demands(position_start_date);

CREATE TABLE demand_versions (
  demand_version_id INTEGER PRIMARY KEY,
  demand_id         INTEGER NOT NULL REFERENCES demands(demand_id),
  version_no        INTEGER NOT NULL,
  jd_text_raw       TEXT NOT NULL,                   -- as uploaded
  jd_text_norm      TEXT NOT NULL,                   -- normalised (REQ-IN-03, §6.2)
  jd_sha256         TEXT NOT NULL,                   -- of jd_text_norm
  ingestion_run_id  INTEGER REFERENCES ingestion_runs(ingestion_run_id),
  created_at        TEXT NOT NULL,
  UNIQUE (demand_id, version_no),
  UNIQUE (demand_id, jd_sha256)
);

CREATE TABLE jd_requirements (
  jd_req_id          INTEGER PRIMARY KEY,
  demand_version_id  INTEGER NOT NULL REFERENCES demand_versions(demand_version_id),
  revision           INTEGER NOT NULL,                 -- edits create a new revision
  status             TEXT NOT NULL CHECK (status IN ('draft','confirmed','superseded')),
  extracted_by       TEXT,                             -- provider/model/version or 'rule_based' or 'manual'
  prompt_version     TEXT,
  extraction_json    TEXT CHECK (extraction_json IS NULL OR json_valid(extraction_json)),
  location_norm      TEXT,
  onsite_required    INTEGER,                          -- REQ-LOC-01 (NULL = not stated)
  min_years_exp      REAL,
  confirmed_by       INTEGER REFERENCES users(user_id),
  confirmed_at       TEXT,                             -- 'Confirm & Start' (REQ-MAT-02)
  created_at         TEXT NOT NULL,
  UNIQUE (demand_version_id, revision)
);
CREATE UNIQUE INDEX ux_jdreq_one_confirmed ON jd_requirements(demand_version_id) WHERE status = 'confirmed';

CREATE TABLE jd_requirement_items (
  item_id            INTEGER PRIMARY KEY,
  jd_req_id          INTEGER NOT NULL REFERENCES jd_requirements(jd_req_id) ON DELETE CASCADE,
  kind               TEXT NOT NULL CHECK (kind IN ('mandatory_skill','preferred_skill','experience','qualification','domain')),
  text               TEXT NOT NULL,
  canonical_skill_id INTEGER REFERENCES skills(skill_id),
  source_span        TEXT,                            -- evidence offsets/quote in the JD
  position           INTEGER NOT NULL
);
CREATE INDEX ix_jditems_req ON jd_requirement_items(jd_req_id, kind);


CREATE TABLE resources (                          -- one CT row
  resource_id         INTEGER PRIMARY KEY,
  short_name          TEXT NOT NULL,              -- exact source value
  email_address       TEXT,                       -- exact source value, never constructed or corrected (REQ-IN-14)
  short_name_key      TEXT NOT NULL,              -- lower(trim(short_name))
  email_key           TEXT NOT NULL DEFAULT '',   -- lower(trim(email_address)) or '' when missing
  grade               TEXT REFERENCES grades(code),
  location            TEXT,
  location_norm       TEXT,                       -- JSON array of normalised places, e.g. ["edinburgh","glasgow"]
  bench_status        TEXT,                       -- exact source value
  bench_status_code   TEXT REFERENCES bench_statuses(code),
  skills_raw          TEXT,                       -- 'Skills (comma separated)' as read
  available_capacity  TEXT,                       -- displayed only, never used for availability (REQ-ELIG-06)
  bench_start_date    TEXT,
  bench_end_date      TEXT,                        -- parsed date; NULL when blank or unparseable
  bench_end_date_parse_status TEXT NOT NULL DEFAULT 'blank'
                                                    CHECK (bench_end_date_parse_status IN ('valid','blank','unparseable')),
  tracker_text        TEXT,                       -- cached value / link text, formulas never executed (REQ-IN-13)
  tracker_url         TEXT,
  group_name          TEXT,                       -- RG1..RG5 or 'Not on Bench'
  group_owner         TEXT,
  candidate_id        INTEGER REFERENCES candidates(candidate_id),
  eligibility         TEXT NOT NULL CHECK (eligibility IN ('eligible','excluded')),
  exclusion_reason    TEXT,                       -- REQ-ELIG-08
  row_hash            TEXT NOT NULL,
  lifecycle           TEXT NOT NULL DEFAULT 'active' CHECK (lifecycle IN ('active','off_bench','archived')),
  first_ingestion_id  INTEGER REFERENCES ingestion_runs(ingestion_run_id),
  last_ingestion_id   INTEGER REFERENCES ingestion_runs(ingestion_run_id),
  occ_reviewed_by     INTEGER REFERENCES users(user_id),
  occ_reviewed_at     TEXT,
  created_at          TEXT NOT NULL,
  updated_at          TEXT NOT NULL,
  UNIQUE (short_name_key, email_key)              -- ShortName + Email business key (REQ-IN-11)
);
CREATE INDEX ix_resources_elig ON resources(eligibility, grade);
CREATE INDEX ix_resources_status ON resources(bench_status_code);
CREATE INDEX ix_resources_candidate ON resources(candidate_id);

CREATE TABLE candidates (
  candidate_id            INTEGER PRIMARY KEY,
  internal_ref            TEXT NOT NULL UNIQUE,     -- generated ID, e.g. 'PFC-C-000123' (REQ-IN-23)
  display_name            TEXT,                     -- NULL allowed when unknown
  phone                   TEXT,                     -- NULL when missing; never invented
  identity_status         TEXT NOT NULL CHECK (identity_status IN ('complete','incomplete','ambiguous','review_later')),
  classification          TEXT CHECK (classification IN ('Internal','External','Unknown')),   -- P2 (REQ-CLS-02)
  classification_override TEXT CHECK (classification_override IN ('Internal','External','Unknown')),
  override_by             INTEGER REFERENCES users(user_id),
  created_at              TEXT NOT NULL,
  updated_at              TEXT NOT NULL
);

CREATE TABLE candidate_emails (                   -- email = identity signal, not PK (REQ-IN-25)
  candidate_id  INTEGER NOT NULL REFERENCES candidates(candidate_id) ON DELETE CASCADE,
  email         TEXT NOT NULL,                    -- exact value as found (CT or CV)
  email_key     TEXT NOT NULL,
  source        TEXT NOT NULL CHECK (source IN ('CT','CV','manual')),
  confirmed     INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (candidate_id, email_key)
);
CREATE INDEX ix_candidate_emails_key ON candidate_emails(email_key);

CREATE TABLE resumes (                            -- one physical file
  resume_id     INTEGER PRIMARY KEY,
  candidate_id  INTEGER REFERENCES candidates(candidate_id),
  source_path   TEXT NOT NULL,                    -- path relative to configured resume root (REQ-IN-20)
  file_name     TEXT NOT NULL,
  file_sha256   TEXT NOT NULL UNIQUE,
  file_mtime    TEXT,
  mime_type     TEXT NOT NULL CHECK (mime_type IN ('application/pdf','application/vnd.openxmlformats-officedocument.wordprocessingml.document')),
  discovered_at TEXT NOT NULL
);

CREATE TABLE cv_versions (                        -- one extraction of one resume (REQ-IN-24)
  cv_version_id      INTEGER PRIMARY KEY,
  resume_id          INTEGER NOT NULL REFERENCES resumes(resume_id),
  candidate_id       INTEGER REFERENCES candidates(candidate_id),
  version_no         INTEGER NOT NULL,            -- per candidate, ordered by file date
  text_sha256        TEXT NOT NULL,
  text_norm          TEXT NOT NULL,               -- local only; never sent externally unmasked
  sections_json      TEXT CHECK (json_valid(sections_json)),
  facts_json         TEXT CHECK (json_valid(facts_json)),   -- name, emails, phone, skills, experience, qualifications, projects + evidence
  extracted_by       TEXT NOT NULL,               -- provider/model/version
  prompt_version     TEXT,
  identity_match     TEXT CHECK (identity_match IN ('email_exact','name_fuzzy','manual','ambiguous','none')),
  is_latest          INTEGER NOT NULL DEFAULT 0,
  extracted_at       TEXT NOT NULL,
  UNIQUE (candidate_id, version_no),
  UNIQUE (resume_id, text_sha256)
);
CREATE INDEX ix_cv_versions_candidate ON cv_versions(candidate_id, is_latest);


CREATE TABLE skills (                              -- canonical skill dictionary
  skill_id     INTEGER PRIMARY KEY,
  name         TEXT NOT NULL UNIQUE COLLATE NOCASE,   -- e.g. 'Business Analysis'
  category     TEXT,                                  -- e.g. 'Data', 'Delivery', 'Cloud'
  created_by   TEXT NOT NULL CHECK (created_by IN ('seed','system','admin')),
  is_active    INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE skill_synonyms (                      -- synonym/abbreviation tracker (REQ-MAT-05)
  synonym_id         INTEGER PRIMARY KEY,
  skill_id           INTEGER NOT NULL REFERENCES skills(skill_id) ON DELETE CASCADE,
  term               TEXT NOT NULL,                  -- as entered, e.g. 'BA', 'DataBricks', 'Bussiness Analysis'
  term_norm          TEXT NOT NULL UNIQUE,           -- casefolded, punctuation-stripped
  relation           TEXT NOT NULL CHECK (relation IN ('equivalent','abbreviation','misspelling','related')),
  credit             REAL NOT NULL DEFAULT 1.0 CHECK (credit BETWEEN 0 AND 1),   -- 'related' < 1.0
  status             TEXT NOT NULL CHECK (status IN ('suggested','approved','rejected')),  -- system suggests, OCC approves
  created_by         INTEGER REFERENCES users(user_id),
  updated_at         TEXT NOT NULL
);
CREATE INDEX ix_synonyms_skill ON skill_synonyms(skill_id);

CREATE TABLE resource_skills (                     -- skills per resource from CT and/or CV
  resource_skill_id  INTEGER PRIMARY KEY,
  resource_id        INTEGER NOT NULL REFERENCES resources(resource_id) ON DELETE CASCADE,
  cv_version_id      INTEGER REFERENCES cv_versions(cv_version_id),   -- NULL => from CT skills
  source             TEXT NOT NULL CHECK (source IN ('CT','CV')),
  skill_raw          TEXT NOT NULL,
  skill_norm         TEXT NOT NULL,
  canonical_skill_id INTEGER REFERENCES skills(skill_id),
  evidence           TEXT,                           -- quote/section for CV-derived skills
  years              REAL
);
CREATE INDEX ix_resource_skills_res ON resource_skills(resource_id, source, cv_version_id);
CREATE INDEX ix_resource_skills_canon ON resource_skills(canonical_skill_id);


CREATE TABLE grades (                              -- configurable in OCC; default A3..E1 (nine grades)
  code     TEXT PRIMARY KEY,
  ordinal  INTEGER NOT NULL UNIQUE,                -- A3=1, A4=2, B1=3, B2=4, C1=5, C2=6, D1=7, D2=8, E1=9, E1=9
  active   INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE bench_statuses (                      -- REQ-ELIG-02/03, OCC-configurable
  code          TEXT PRIMARY KEY,                  -- e.g. 'AVAILABLE'
  label         TEXT NOT NULL UNIQUE COLLATE NOCASE,   -- exact CT text, e.g. 'Available'
  is_excluded   INTEGER NOT NULL,                  -- 1 for Selected, Unknown, CV with Client - Sold - Pending Allocation
  priority      INTEGER,                           -- 1..6 for eligible statuses; NULL when excluded
  flag_in_gaps  INTEGER NOT NULL DEFAULT 0,        -- 1 for CV-with-client / proposed statuses (REQ-ELIG-04)
  available_from_bench_start INTEGER NOT NULL DEFAULT 0   -- 1 for 'Expected on Bench Soon'
);

CREATE TABLE status_map (                           -- REQ-ELIG-07, OCC-editable source-to-canonical map
  source_value       TEXT PRIMARY KEY COLLATE NOCASE, -- normalised source status from CT
  canonical_code     TEXT REFERENCES bench_statuses(code),
  priority           INTEGER NOT NULL DEFAULT 2,      -- unmapped values default to priority 2
  is_excluded        INTEGER NOT NULL DEFAULT 0,
  mapped_by          INTEGER REFERENCES users(user_id),
  updated_at         TEXT NOT NULL
);


CREATE TABLE embedding_models (
  embedding_model_id INTEGER PRIMARY KEY,
  name               TEXT NOT NULL,                 -- default 'nomic-embed-text-v1.5'
  runtime            TEXT NOT NULL CHECK (runtime IN ('onnx','llamacpp')),
  file_sha256        TEXT NOT NULL,
  dim                INTEGER NOT NULL,
  normalised         INTEGER NOT NULL DEFAULT 1,    -- L2-normalised vectors => cosine = dot product
  pipeline_version   TEXT NOT NULL,                 -- normalisation + chunking version
  is_active          INTEGER NOT NULL DEFAULT 0,
  UNIQUE (name, file_sha256, pipeline_version)
);
CREATE UNIQUE INDEX ux_embedding_models_active ON embedding_models(is_active) WHERE is_active = 1;

CREATE TABLE embedding_chunks (
  chunk_id            INTEGER PRIMARY KEY,           -- also the rowid in vec_chunks
  owner_type          TEXT NOT NULL CHECK (owner_type IN ('jd','jd_item','cv','ct_skills','skill')),
  owner_id            INTEGER NOT NULL,              -- demand_version_id / item_id / cv_version_id / resource_id / skill_id
  section             TEXT NOT NULL,                 -- e.g. 'responsibilities', 'experience', 'skills'
  chunk_no            INTEGER NOT NULL,
  text                TEXT NOT NULL,
  text_sha256         TEXT NOT NULL,
  token_count         INTEGER NOT NULL,
  embedding_model_id  INTEGER NOT NULL REFERENCES embedding_models(embedding_model_id),
  created_at          TEXT NOT NULL,
  UNIQUE (owner_type, owner_id, section, chunk_no, embedding_model_id)
);
CREATE INDEX ix_chunks_hash ON embedding_chunks(text_sha256, embedding_model_id);   -- reuse identical text
CREATE INDEX ix_chunks_owner ON embedding_chunks(owner_type, owner_id);

-- sqlite-vec virtual table (default store, §6.5); dimension fixed per active model
-- vec_chunks is managed by the vector adapter.

-- FTS5 keyword index for BM25 (hybrid retrieval, §6.7)
CREATE VIRTUAL TABLE fts_chunks USING fts5(text, owner_type UNINDEXED, owner_id UNINDEXED,
                                           content='embedding_chunks', content_rowid='chunk_id',
                                           tokenize='unicode61 remove_diacritics 2');

-- REQ-MAT-05: keep hybrid keyword retrieval aligned with embedding content.
CREATE TRIGGER embedding_chunks_fts_insert AFTER INSERT ON embedding_chunks
BEGIN
  INSERT INTO fts_chunks(rowid, text, owner_type, owner_id)
  VALUES (new.chunk_id, new.text, new.owner_type, new.owner_id);
END;
CREATE TRIGGER embedding_chunks_fts_delete AFTER DELETE ON embedding_chunks
BEGIN
  INSERT INTO fts_chunks(fts_chunks, rowid, text, owner_type, owner_id)
  VALUES ('delete', old.chunk_id, old.text, old.owner_type, old.owner_id);
END;
CREATE TRIGGER embedding_chunks_fts_update AFTER UPDATE ON embedding_chunks
BEGIN
  INSERT INTO fts_chunks(fts_chunks, rowid, text, owner_type, owner_id)
  VALUES ('delete', old.chunk_id, old.text, old.owner_type, old.owner_id);
  INSERT INTO fts_chunks(rowid, text, owner_type, owner_id)
  VALUES (new.chunk_id, new.text, new.owner_type, new.owner_id);
END;


CREATE TABLE scoring_templates (                   -- admin-only, versioned (REQ-SCR-01, REQ-ROLE-04)
  template_id     INTEGER PRIMARY KEY,
  name            TEXT NOT NULL,                   -- 'default'; role-specific templates in P3
  version         INTEGER NOT NULL,
  role_family     TEXT,                            -- P3: e.g. 'Data Engineer'
  weights_json    TEXT NOT NULL CHECK (json_valid(weights_json)),   -- {"skills":70,"grade":10,"location":5,"bench_start":5,"bench_end":5,"status":5}
  rules_json      TEXT NOT NULL CHECK (json_valid(rules_json)),     -- sub-weights, thresholds, status_score_mode, bands, top_n
  status          TEXT NOT NULL CHECK (status IN ('draft','active','retired')),
  created_by      INTEGER REFERENCES users(user_id),
  created_at      TEXT NOT NULL,
  activated_at    TEXT,
  UNIQUE (name, version)
);
CREATE UNIQUE INDEX ux_template_active ON scoring_templates(name, IFNULL(role_family,'')) WHERE status = 'active';

CREATE TABLE match_runs (                          -- REQ-MAT-08
  run_id               INTEGER PRIMARY KEY,
  scope                TEXT NOT NULL CHECK (scope IN ('full','demands','resources','incremental')),
  scope_json           TEXT,
  template_id          INTEGER NOT NULL REFERENCES scoring_templates(template_id),
  embedding_model_id   INTEGER NOT NULL REFERENCES embedding_models(embedding_model_id),
  llm_model            TEXT,                        -- model used for JD/CV extraction of inputs (recorded per input too)
  engine_version       TEXT NOT NULL,               -- scoring engine semantic version
  synonyms_version     TEXT NOT NULL,               -- hash of approved synonym set
  config_hash          TEXT NOT NULL,               -- hash of every input parameter => reproducibility
  triggered_by         INTEGER REFERENCES users(user_id),
  status               TEXT NOT NULL CHECK (status IN ('queued','running','completed','failed','cancelled')),
  started_at           TEXT, finished_at TEXT,
  demands_assessed     INTEGER, pairs_scored INTEGER, roles_without_candidate INTEGER,   -- dashboard header (REQ-OUT-04)
  dq_summary_json      TEXT
);

CREATE TABLE eligibility_exclusions (              -- 'Excluded from matching' view (REQ-ELIG-07)
  run_id       INTEGER NOT NULL REFERENCES match_runs(run_id) ON DELETE CASCADE,
  resource_id  INTEGER NOT NULL REFERENCES resources(resource_id),
  demand_id    INTEGER REFERENCES demands(demand_id),     -- NULL => global exclusion; set => pair-level (grade >1 away)
  reason_code  TEXT NOT NULL,       -- STATUS_EXCLUDED, NOT_ON_BENCH, BENCH_END_SET, NO_GRADE, NO_SKILLS, GRADE_GAP_GT1
  detail       TEXT
);
CREATE INDEX ix_excl_run ON eligibility_exclusions(run_id, reason_code);

CREATE TABLE match_results (                       -- one row per requisition-person (REQ-MAT-03)
  result_id            INTEGER PRIMARY KEY,
  run_id               INTEGER NOT NULL REFERENCES match_runs(run_id) ON DELETE CASCADE,
  demand_id            INTEGER NOT NULL REFERENCES demands(demand_id),
  demand_version_id    INTEGER NOT NULL REFERENCES demand_versions(demand_version_id),
  jd_req_id            INTEGER NOT NULL REFERENCES jd_requirements(jd_req_id),
  resource_id          INTEGER NOT NULL REFERENCES resources(resource_id),
  candidate_id         INTEGER REFERENCES candidates(candidate_id),
  best_cv_version_id   INTEGER REFERENCES cv_versions(cv_version_id),   -- NULL => scored on CT skills only
  score                REAL NOT NULL CHECK (score BETWEEN 0 AND 100),   -- rounded to 1 dp
  band                 TEXT NOT NULL CHECK (band IN ('Strong','Potential','Avg','Weak','Insufficient data')),
  rank                 INTEGER NOT NULL,
  shortlisted          INTEGER NOT NULL,            -- in top-N for the requisition
  email_preselected    INTEGER NOT NULL DEFAULT 0,  -- top 4 default (REQ-EML-01, O-07)
  grade_relation       TEXT NOT NULL CHECK (grade_relation IN ('exact','adjacent_above','adjacent_below')),
  location_assessment  TEXT NOT NULL CHECK (location_assessment IN ('Aligned','Remote/unspecified','Potential travel','Location check')),
  status_priority      INTEGER,
  mandatory_missing    INTEGER NOT NULL DEFAULT 0,  -- REQ-SCR-03
  matching_evidence    TEXT NOT NULL,
  gaps_checks          TEXT NOT NULL,
  eligibility_evidence TEXT NOT NULL,
  UNIQUE (run_id, demand_id, resource_id)
);
CREATE INDEX ix_results_demand ON match_results(run_id, demand_id, rank);
CREATE INDEX ix_results_resource ON match_results(run_id, resource_id);
CREATE INDEX ix_results_band ON match_results(run_id, band, score);

CREATE TABLE match_cv_scores (                     -- per-CV-version scores for drill-down (REQ-IN-24)
  result_id      INTEGER NOT NULL REFERENCES match_results(result_id) ON DELETE CASCADE,
  cv_version_id  INTEGER REFERENCES cv_versions(cv_version_id),   -- NULL => CT-only basis
  score          REAL NOT NULL,
  band           TEXT NOT NULL,
  PRIMARY KEY (result_id, cv_version_id)
);

CREATE TABLE score_breakdowns (                    -- explainability (REQ-SCR-04)
  result_id    INTEGER NOT NULL REFERENCES match_results(result_id) ON DELETE CASCADE,
  component    TEXT NOT NULL CHECK (component IN ('skills','grade','location','bench_start','bench_end','status')),
  points       REAL NOT NULL,
  max_points   REAL NOT NULL,
  inputs_json  TEXT NOT NULL CHECK (json_valid(inputs_json)),   -- e.g. matched items, similarities, day gap, status label
  PRIMARY KEY (result_id, component)
);

CREATE TABLE overrides (                           -- REQ-MAT-09
  override_id  INTEGER PRIMARY KEY,
  demand_id    INTEGER NOT NULL REFERENCES demands(demand_id),
  resource_id  INTEGER NOT NULL REFERENCES resources(resource_id),
  result_id    INTEGER REFERENCES match_results(result_id),
  action       TEXT NOT NULL CHECK (action IN ('pin','unpin','exclude','include','adjust_band')),
  new_band     TEXT,
  reason       TEXT NOT NULL,
  created_by   INTEGER NOT NULL REFERENCES users(user_id),
  created_at   TEXT NOT NULL,
  revoked_by   INTEGER REFERENCES users(user_id),
  revoked_at   TEXT
);
CREATE INDEX ix_overrides_pair ON overrides(demand_id, resource_id) WHERE revoked_at IS NULL;


CREATE TABLE email_templates (                     -- REQ-EML-03
  email_template_id INTEGER PRIMARY KEY,
  name              TEXT NOT NULL,
  version           INTEGER NOT NULL,
  subject           TEXT NOT NULL,                 -- e.g. 'Opportunity {requisition} – {role}'
  body_html         TEXT NOT NULL,                 -- placeholders {candidate_name} {requisition} {role} {match_pct} {gaps} {pointers} {spoc_1} {spoc_2}
  is_default        INTEGER NOT NULL DEFAULT 0,
  created_by        INTEGER REFERENCES users(user_id),
  created_at        TEXT NOT NULL,
  UNIQUE (name, version)
);

CREATE TABLE resource_team_recipients (            -- CC list maintained in OCC (REQ-EML-05)
  recipient_id  INTEGER PRIMARY KEY,
  email         TEXT NOT NULL UNIQUE COLLATE NOCASE,
  display_name  TEXT,
  is_active     INTEGER NOT NULL DEFAULT 1,
  added_by      INTEGER REFERENCES users(user_id),
  added_at      TEXT NOT NULL
);

CREATE TABLE email_messages (                      -- REQ-EML-01/02/06
  message_id         INTEGER PRIMARY KEY,
  demand_id          INTEGER NOT NULL REFERENCES demands(demand_id),
  resource_id        INTEGER NOT NULL REFERENCES resources(resource_id),
  result_id          INTEGER REFERENCES match_results(result_id),
  send_seq           INTEGER NOT NULL DEFAULT 1,       -- >1 only for explicit re-send
  email_template_id  INTEGER NOT NULL REFERENCES email_templates(email_template_id),
  to_address         TEXT NOT NULL,                    -- copied exactly from CT (REQ-IN-14)
  cc_json            TEXT NOT NULL CHECK (json_valid(cc_json)),
  subject            TEXT NOT NULL,
  body_html          TEXT NOT NULL,
  jd_attachment_path TEXT,
  status             TEXT NOT NULL CHECK (status IN ('Draft','Approved','Sending','Sent','Failed','Discarded')),
  idempotency_key    TEXT NOT NULL UNIQUE,             -- sha256(demand_id|resource_id|send_seq|install_id)
  transport          TEXT CHECK (transport IN ('outlook_com','smtp')),
  transport_ref      TEXT,                             -- Outlook EntryID / SMTP Message-ID
  drafted_by_model   TEXT,
  created_by         INTEGER NOT NULL REFERENCES users(user_id),
  approved_by        INTEGER REFERENCES users(user_id), approved_at TEXT,
  sent_by            INTEGER REFERENCES users(user_id), sent_at TEXT,
  last_error         TEXT,
  attempts           INTEGER NOT NULL DEFAULT 0,
  created_at         TEXT NOT NULL,
  updated_at         TEXT NOT NULL,
  UNIQUE (demand_id, resource_id, send_seq)            -- prevents duplicate sends for the same pair
);
CREATE INDEX ix_email_status ON email_messages(status, updated_at);

CREATE TABLE follow_ups (                          -- P2 (REQ-EML-08)
  follow_up_id   INTEGER PRIMARY KEY,
  message_id     INTEGER NOT NULL REFERENCES email_messages(message_id),
  kind           TEXT NOT NULL CHECK (kind IN ('reminder','meeting')),
  due_at         TEXT NOT NULL,
  outlook_ref    TEXT,                             -- AppointmentItem EntryID
  status         TEXT NOT NULL CHECK (status IN ('pending','created','failed','cancelled')),
  created_at     TEXT NOT NULL,
  UNIQUE (message_id, kind)
);

CREATE TABLE internal_email_domains (              -- P2 (REQ-CLS-01)
  domain     TEXT PRIMARY KEY,                     -- normalised, '@' stripped, lower-case
  added_by   INTEGER REFERENCES users(user_id),
  added_at   TEXT NOT NULL
);


CREATE TABLE settings (
  setting_id  INTEGER PRIMARY KEY,
  scope       TEXT NOT NULL CHECK (scope IN ('system','user')),
  user_id     INTEGER REFERENCES users(user_id),       -- NULL for system scope
  key         TEXT NOT NULL,                           -- e.g. 'resume.root_path', 'email.transport', 'matching.top_n'
  value_json  TEXT NOT NULL CHECK (json_valid(value_json)),
  admin_only  INTEGER NOT NULL DEFAULT 0,
  updated_by  INTEGER REFERENCES users(user_id),
  updated_at  TEXT NOT NULL
);
CREATE UNIQUE INDEX ux_settings_key ON settings(scope, IFNULL(user_id,0), key);

CREATE TABLE settings_packages (
  package_id         INTEGER PRIMARY KEY,
  package_version    INTEGER NOT NULL UNIQUE,       -- monotonic integer
  schema_version     TEXT NOT NULL,
  app_version        TEXT NOT NULL,
  package_sha256     TEXT NOT NULL,
  signing_key_id     TEXT NOT NULL,
  imported_by        INTEGER NOT NULL REFERENCES users(user_id),
  imported_at        TEXT NOT NULL,
  previous_package_id INTEGER REFERENCES settings_packages(package_id),
  is_active          INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX ux_settings_packages_active
  ON settings_packages(is_active) WHERE is_active = 1;

CREATE TABLE llm_models (                          -- approved model list (REQ-LLM-06)
  model_id         INTEGER PRIMARY KEY,
  name             TEXT NOT NULL UNIQUE,           -- e.g. 'qwen2.5-1.5b-instruct-q4_k_m'
  file_name        TEXT NOT NULL,
  file_sha256      TEXT NOT NULL,
  size_mb          INTEGER NOT NULL,
  ram_estimate_mb  INTEGER NOT NULL,
  context_tokens   INTEGER NOT NULL,
  licence          TEXT NOT NULL,
  approved         INTEGER NOT NULL DEFAULT 0,
  benchmark_json   TEXT,                           -- P3 benchmark lab
  is_active        INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX ux_llm_models_active ON llm_models(is_active) WHERE is_active = 1;   -- one active model (REQ-LLM-01)

CREATE TABLE llm_providers (                       -- REQ-LLM-03/04
  provider_id       INTEGER PRIMARY KEY,
  name              TEXT NOT NULL UNIQUE,          -- 'local', 'openai', 'devin'
  kind              TEXT NOT NULL CHECK (kind IN ('llamacpp','openai_compatible','devin')),
  enabled           INTEGER NOT NULL DEFAULT 0,    -- external disabled by default; P1 = placeholder config only
  fallback_order    INTEGER NOT NULL,              -- external requests fall back directly to local; no external-provider chain
  base_url          TEXT,
  api_version       TEXT,                          -- preferred API version
  compatible_versions_json TEXT,                   -- ordered fallbacks for version-conflict retry
  model             TEXT,
  token_blob        BLOB,                          -- DPAPI-protected user token (REQ-AUTH-06)
  owner_user_id     INTEGER REFERENCES users(user_id),   -- user-supplied token
  capabilities_json TEXT,
  timeout_s         INTEGER NOT NULL DEFAULT 60,
  max_concurrency   INTEGER NOT NULL DEFAULT 1,
  updated_at        TEXT NOT NULL
);

CREATE TABLE llm_calls (                           -- telemetry + audit for every LLM request
  call_id         INTEGER PRIMARY KEY,
  provider_id     INTEGER REFERENCES llm_providers(provider_id),
  model           TEXT NOT NULL,
  purpose         TEXT NOT NULL CHECK (purpose IN ('jd_extract','cv_extract','email_draft','summary','chat','embed','benchmark')),
  prompt_version  TEXT,
  request_sha256  TEXT NOT NULL,
  masked          INTEGER NOT NULL DEFAULT 0,      -- 1 if PII guard applied (external)
  status          TEXT NOT NULL CHECK (status IN ('ok','error','fallback','timeout','rejected_pii')),
  fallback_from   INTEGER REFERENCES llm_calls(call_id),
  latency_ms      INTEGER,
  tokens_in       INTEGER, tokens_out INTEGER,
  user_id         INTEGER REFERENCES users(user_id),
  created_at      TEXT NOT NULL,
  error           TEXT
);
CREATE INDEX ix_llm_calls_time ON llm_calls(created_at);

CREATE TABLE jobs (                                -- background jobs (§11)
  job_id        INTEGER PRIMARY KEY,
  kind          TEXT NOT NULL,                     -- 'ingest_commit','jd_analyze','cv_sync','embed','match_run','pdf_report','archive','housekeeping'
  params_json   TEXT,
  status        TEXT NOT NULL CHECK (status IN ('queued','running','completed','failed','cancelled')),
  progress      REAL NOT NULL DEFAULT 0,
  message       TEXT,
  created_by    INTEGER REFERENCES users(user_id),
  created_at    TEXT NOT NULL, started_at TEXT, finished_at TEXT
);

CREATE TABLE audit_log (                           -- REQ-AUTH-05, append-only
  audit_id     INTEGER PRIMARY KEY,
  ts           TEXT NOT NULL,
  user_id      INTEGER REFERENCES users(user_id),
  action       TEXT NOT NULL,      -- LOGIN_OK, LOGIN_FAIL, LOCKOUT, UNMASK, OCC_EDIT, TEMPLATE_ACTIVATE, EMAIL_APPROVE, EMAIL_SEND, ARCHIVE_RUN, SETTING_CHANGE, LLM_EXTERNAL_CALL, OVERRIDE, LICENCE_IMPORT
  entity_type  TEXT,
  entity_id    TEXT,
  details_json TEXT CHECK (details_json IS NULL OR json_valid(details_json)),   -- never contains unmasked PII values
  prev_hash    TEXT NOT NULL,
  row_hash     TEXT NOT NULL       -- sha256(prev_hash || canonical row) => tamper evidence
);
CREATE INDEX ix_audit_ts ON audit_log(ts);
CREATE INDEX ix_audit_action ON audit_log(action, ts);
-- triggers reject UPDATE and DELETE on audit_log (except retention purge by the housekeeping job, which itself is audited)

-- REQ-ROLE-01
INSERT INTO roles(name) VALUES ('Admin'), ('Analyst');
-- REQ-AUTH-05: audit history is retained (REQ-ARC-03).
CREATE TRIGGER audit_log_no_update BEFORE UPDATE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
CREATE TRIGGER audit_log_no_delete BEFORE DELETE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
