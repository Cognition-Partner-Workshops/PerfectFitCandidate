CREATE TABLE skill_synonyms_new (
  synonym_id INTEGER PRIMARY KEY,
  skill_id INTEGER NOT NULL REFERENCES skills(skill_id) ON DELETE CASCADE,
  term TEXT NOT NULL,
  term_norm TEXT NOT NULL,
  relation TEXT NOT NULL CHECK (relation IN ('equivalent','abbreviation','misspelling','related')),
  credit REAL NOT NULL DEFAULT 1.0 CHECK (credit BETWEEN 0 AND 1),
  status TEXT NOT NULL CHECK (status IN ('suggested','approved','rejected')),
  created_by INTEGER REFERENCES users(user_id),
  updated_at TEXT NOT NULL,
  UNIQUE (skill_id, term_norm)
);
INSERT INTO skill_synonyms_new SELECT * FROM skill_synonyms;
DROP TABLE skill_synonyms;
ALTER TABLE skill_synonyms_new RENAME TO skill_synonyms;
CREATE INDEX ix_synonyms_skill ON skill_synonyms(skill_id);
