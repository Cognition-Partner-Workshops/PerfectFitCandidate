CREATE TABLE summaries (
  summary_id INTEGER PRIMARY KEY,
  owner_kind TEXT NOT NULL CHECK (owner_kind IN ('candidate','demand')),
  owner_id INTEGER NOT NULL,
  source_version_id INTEGER NOT NULL,
  source_sha256 TEXT NOT NULL,
  model TEXT NOT NULL,
  template_id TEXT NOT NULL,
  template_version TEXT NOT NULL,
  sentences_json TEXT NOT NULL CHECK (json_valid(sentences_json)),
  dropped_ungrounded INTEGER NOT NULL CHECK (dropped_ungrounded >= 0),
  created_by INTEGER NOT NULL REFERENCES users(user_id),
  created_at TEXT NOT NULL
);
CREATE INDEX ix_summaries_latest
  ON summaries(owner_kind, owner_id, created_at DESC, summary_id DESC);
