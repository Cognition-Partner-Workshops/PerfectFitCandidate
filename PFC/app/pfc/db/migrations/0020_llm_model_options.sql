ALTER TABLE llm_models ADD COLUMN thinking INTEGER NOT NULL DEFAULT 0;
ALTER TABLE llm_models ADD COLUMN kv_bytes_per_token INTEGER;
UPDATE llm_models SET kv_bytes_per_token=147456 WHERE name='Qwen3-4B-Instruct-2507';
