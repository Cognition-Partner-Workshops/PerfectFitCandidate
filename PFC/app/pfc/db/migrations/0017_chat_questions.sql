CREATE TABLE chat_questions (
  job_id       INTEGER PRIMARY KEY REFERENCES jobs(job_id),
  question     TEXT NOT NULL,
  answer_json  TEXT CHECK (answer_json IS NULL OR json_valid(answer_json))
);
