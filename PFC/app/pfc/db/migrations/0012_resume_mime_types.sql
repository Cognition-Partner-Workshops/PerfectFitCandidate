-- rebuild-table: resumes
CREATE TABLE resumes_new (
  resume_id INTEGER PRIMARY KEY,
  candidate_id INTEGER REFERENCES candidates(candidate_id),
  source_path TEXT NOT NULL,
  file_name TEXT NOT NULL,
  file_sha256 TEXT NOT NULL UNIQUE,
  file_mtime TEXT,
  mime_type TEXT NOT NULL CHECK (mime_type IN (
    'application/pdf',
    'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    'text/plain',
    'application/vnd.openxmlformats-officedocument.presentationml.presentation',
    'application/vnd.oasis.opendocument.text',
    'application/rtf',
    'application/msword',
    'application/vnd.ms-powerpoint'
  )),
  discovered_at TEXT NOT NULL
);
INSERT INTO resumes_new SELECT * FROM resumes;
DROP TABLE resumes;
ALTER TABLE resumes_new RENAME TO resumes;
