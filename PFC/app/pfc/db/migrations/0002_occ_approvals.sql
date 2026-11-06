CREATE TABLE occ_approvals (
  entry_id INTEGER PRIMARY KEY,
  entity_type TEXT NOT NULL CHECK (entity_type IN ('demand','resource')),
  entity_id INTEGER NOT NULL,
  ingestion_run_id INTEGER NOT NULL REFERENCES ingestion_runs(ingestion_run_id),
  change TEXT NOT NULL CHECK (change IN ('New','Updated','Removed')),
  state TEXT NOT NULL DEFAULT 'Pending' CHECK (state IN ('Pending','Approved','Modified','Reverted')),
  before_json TEXT CHECK (before_json IS NULL OR json_valid(before_json)),
  after_json TEXT NOT NULL CHECK (json_valid(after_json)),
  admin_values_json TEXT CHECK (admin_values_json IS NULL OR json_valid(admin_values_json)),
  conflict INTEGER NOT NULL DEFAULT 0 CHECK (conflict IN (0,1)),
  comment TEXT,
  uploaded_by INTEGER REFERENCES users(user_id),
  uploaded_at TEXT NOT NULL,
  decided_by INTEGER REFERENCES users(user_id),
  decided_at TEXT
);
CREATE UNIQUE INDEX ux_occ_one_pending ON occ_approvals(entity_type,entity_id) WHERE state='Pending';
CREATE INDEX ix_occ_entity ON occ_approvals(entity_type,entity_id,entry_id);
