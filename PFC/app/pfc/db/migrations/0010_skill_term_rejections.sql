CREATE TABLE skill_term_rejections (
  term_norm   TEXT PRIMARY KEY,
  term        TEXT NOT NULL,
  rejected_by INTEGER REFERENCES users(user_id),
  rejected_at TEXT NOT NULL
);
