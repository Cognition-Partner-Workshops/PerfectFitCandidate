ALTER TABLE jd_requirement_items ADD COLUMN source_jd_req_id INTEGER
  REFERENCES jd_requirement_items(item_id) ON DELETE SET NULL;
