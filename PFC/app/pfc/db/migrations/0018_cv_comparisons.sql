-- REQ-IN-24 / FR-ING-52: read-only CV version comparison what-if scores.
-- A comparison never changes match results; it only stores the scored snapshot
-- of two CV versions of one candidate against that candidate's current demands.
CREATE TABLE cv_comparisons (
  comparison_id        INTEGER PRIMARY KEY,
  candidate_id         INTEGER NOT NULL REFERENCES candidates(candidate_id) ON DELETE CASCADE,
  left_cv_version_id   INTEGER NOT NULL REFERENCES cv_versions(cv_version_id) ON DELETE CASCADE,
  right_cv_version_id  INTEGER NOT NULL REFERENCES cv_versions(cv_version_id) ON DELETE CASCADE,
  job_id               INTEGER REFERENCES jobs(job_id) ON DELETE SET NULL,
  status               TEXT NOT NULL CHECK (status IN ('queued','running','completed','failed','cancelled')),
  result_json          TEXT CHECK (result_json IS NULL OR json_valid(result_json)),
  created_by           INTEGER REFERENCES users(user_id),
  created_at           TEXT NOT NULL,
  finished_at          TEXT
);
CREATE INDEX ix_cv_comparisons_pair ON cv_comparisons(candidate_id, left_cv_version_id, right_cv_version_id, comparison_id);
