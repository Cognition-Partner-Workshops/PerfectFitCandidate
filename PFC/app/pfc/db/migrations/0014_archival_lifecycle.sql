CREATE TABLE archive_batches (
  batch_id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL CHECK(kind IN ('archive','purge')),
  trigger TEXT NOT NULL CHECK(trigger='manual'),
  status TEXT NOT NULL CHECK(status IN ('previewed','running','completed','failed','discarded','expired')),
  criteria_json TEXT, preview_json TEXT, preview_hash TEXT,
  created_by INTEGER REFERENCES users(user_id), created_at TEXT NOT NULL,
  job_id INTEGER REFERENCES jobs(job_id), completed_at TEXT, result_json TEXT
);
CREATE TABLE archive_batch_items (
  batch_id INTEGER NOT NULL REFERENCES archive_batches(batch_id),
  entity_type TEXT NOT NULL CHECK(entity_type IN ('demand','resource')),
  entity_id INTEGER NOT NULL, label TEXT NOT NULL, reason TEXT NOT NULL,
  fingerprint TEXT NOT NULL, included INTEGER NOT NULL DEFAULT 1 CHECK(included IN (0,1)),
  outcome TEXT CHECK(outcome IN ('archived','purged','skipped')),
  PRIMARY KEY(batch_id,entity_type,entity_id)
);
ALTER TABLE demands ADD COLUMN end_outcome TEXT;
ALTER TABLE demands ADD COLUMN end_source TEXT CHECK(end_source IN ('manual','upload','occ','status'));
ALTER TABLE demands ADD COLUMN ended_at TEXT;
ALTER TABLE demands ADD COLUMN archived_at TEXT;
ALTER TABLE demands ADD COLUMN archive_batch_id INTEGER REFERENCES archive_batches(batch_id);
ALTER TABLE resources ADD COLUMN end_outcome TEXT;
ALTER TABLE resources ADD COLUMN end_source TEXT CHECK(end_source IN ('manual','upload','occ','status'));
ALTER TABLE resources ADD COLUMN ended_at TEXT;
ALTER TABLE resources ADD COLUMN archived_at TEXT;
ALTER TABLE resources ADD COLUMN archive_batch_id INTEGER REFERENCES archive_batches(batch_id);
CREATE TABLE lifecycle_events (
  event_id INTEGER PRIMARY KEY,
  entity_type TEXT NOT NULL CHECK(entity_type IN ('demand','resource')),
  entity_id INTEGER NOT NULL, from_state TEXT, to_state TEXT NOT NULL,
  outcome TEXT, reason_code TEXT, comment TEXT,
  source TEXT NOT NULL CHECK(source IN ('baseline','upload','manual','occ_approval','archive','purge','restore')),
  ingestion_run_id INTEGER REFERENCES ingestion_runs(ingestion_run_id),
  archive_batch_id INTEGER REFERENCES archive_batches(batch_id),
  user_id INTEGER REFERENCES users(user_id), created_at TEXT NOT NULL
);
CREATE INDEX ix_lifecycle_entity ON lifecycle_events(entity_type,entity_id,created_at);
CREATE INDEX ix_lifecycle_created ON lifecycle_events(created_at);
CREATE TRIGGER lifecycle_events_no_delete BEFORE DELETE ON lifecycle_events
BEGIN SELECT RAISE(ABORT,'Lifecycle events are append-only'); END;
CREATE TRIGGER lifecycle_events_no_update BEFORE UPDATE ON lifecycle_events
WHEN NEW.comment IS NOT NULL OR NEW.event_id IS NOT OLD.event_id
 OR NEW.entity_type IS NOT OLD.entity_type OR NEW.entity_id IS NOT OLD.entity_id
 OR NEW.from_state IS NOT OLD.from_state OR NEW.to_state IS NOT OLD.to_state
 OR NEW.outcome IS NOT OLD.outcome OR NEW.reason_code IS NOT OLD.reason_code
 OR NEW.source IS NOT OLD.source OR NEW.ingestion_run_id IS NOT OLD.ingestion_run_id
 OR NEW.archive_batch_id IS NOT OLD.archive_batch_id OR NEW.user_id IS NOT OLD.user_id
 OR NEW.created_at IS NOT OLD.created_at
BEGIN SELECT RAISE(ABORT,'Lifecycle events are append-only'); END;
INSERT INTO lifecycle_events(entity_type,entity_id,to_state,source,created_at)
 SELECT 'demand',demand_id,lifecycle,'baseline',created_at FROM demands;
INSERT INTO lifecycle_events(entity_type,entity_id,to_state,source,created_at)
 SELECT 'resource',resource_id,lifecycle,'baseline',created_at FROM resources;
INSERT OR IGNORE INTO settings(scope,key,value_json,admin_only,updated_at) VALUES
 ('system','archive.retention_months','12',1,strftime('%Y-%m-%dT%H:%M:%SZ','now')),
 ('system','housekeeping.temp_days','7',1,strftime('%Y-%m-%dT%H:%M:%SZ','now')),
 ('system','housekeeping.log_days','45',1,strftime('%Y-%m-%dT%H:%M:%SZ','now')),
 ('system','housekeeping.backups_keep','3',1,strftime('%Y-%m-%dT%H:%M:%SZ','now'));
