ALTER TABLE match_runs ADD COLUMN config_key TEXT;
ALTER TABLE match_runs ADD COLUMN input_fingerprints_json TEXT
  CHECK (input_fingerprints_json IS NULL OR json_valid(input_fingerprints_json));
