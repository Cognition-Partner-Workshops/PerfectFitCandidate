-- rebuild-table: follow_ups
CREATE TABLE follow_ups_new (
 follow_up_id INTEGER PRIMARY KEY,
 message_id INTEGER NOT NULL UNIQUE REFERENCES email_messages(message_id),
 kind TEXT NOT NULL DEFAULT 'reminder' CHECK(kind='reminder'),
 transport TEXT NOT NULL CHECK(transport IN ('outlook_com','eml_handoff')),
 due_at TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('pending','created','failed','handoff')),
 outlook_ref TEXT,
 last_error TEXT,
 job_id INTEGER,
 created_by INTEGER REFERENCES users(user_id),
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL
);
INSERT INTO follow_ups_new
 (follow_up_id,message_id,kind,transport,due_at,status,outlook_ref,created_at,updated_at)
 SELECT follow_up_id,message_id,kind,'outlook_com',due_at,
 CASE WHEN status='cancelled' THEN 'failed' ELSE status END,outlook_ref,created_at,created_at
 FROM follow_ups;
DROP TABLE follow_ups;
ALTER TABLE follow_ups_new RENAME TO follow_ups;
CREATE INDEX follow_ups_status ON follow_ups(status);
