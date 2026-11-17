ALTER TABLE occ_approvals ADD COLUMN reopened_from_entry_id INTEGER REFERENCES occ_approvals(entry_id);
