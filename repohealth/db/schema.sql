PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS repos (
    repo_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    path        TEXT NOT NULL UNIQUE,
    name        TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS commits (
    commit_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_id     INTEGER NOT NULL REFERENCES repos(repo_id) ON DELETE CASCADE,
    sha         TEXT NOT NULL,
    author_name TEXT,
    author_email TEXT,
    authored_at TEXT,
    message     TEXT,
    UNIQUE (repo_id, sha)
);
CREATE INDEX IF NOT EXISTS idx_commits_repo ON commits(repo_id);

CREATE TABLE IF NOT EXISTS file_changes (
    change_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    commit_id   INTEGER NOT NULL REFERENCES commits(commit_id) ON DELETE CASCADE,
    path        TEXT NOT NULL,
    lines_added   INTEGER DEFAULT 0,
    lines_deleted INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_changes_path ON file_changes(path);
CREATE INDEX IF NOT EXISTS idx_changes_commit ON file_changes(commit_id);

CREATE TABLE IF NOT EXISTS runs (
    run_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_id      INTEGER NOT NULL REFERENCES repos(repo_id) ON DELETE CASCADE,
    commit_sha   TEXT,
    started_at   TEXT NOT NULL,
    completed_at TEXT,
    status       TEXT NOT NULL DEFAULT 'running',
    kind         TEXT NOT NULL DEFAULT 'full'
);
CREATE INDEX IF NOT EXISTS idx_runs_repo ON runs(repo_id);

CREATE TABLE IF NOT EXISTS file_metrics (
    metric_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        INTEGER NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    path          TEXT NOT NULL,
    language      TEXT DEFAULT 'Python',
    cc_total      INTEGER DEFAULT 0,
    cc_max        INTEGER DEFAULT 0,
    cc_avg        REAL DEFAULT 0,
    loc           INTEGER DEFAULT 0,
    num_functions INTEGER DEFAULT 0,
    num_classes   INTEGER DEFAULT 0,
    max_class_loc INTEGER DEFAULT 0,
    max_nesting   INTEGER DEFAULT 0,
    parsed        INTEGER NOT NULL DEFAULT 1,
    parse_error   TEXT
);
CREATE INDEX IF NOT EXISTS idx_metrics_path ON file_metrics(path);
CREATE INDEX IF NOT EXISTS idx_metrics_run ON file_metrics(run_id);

CREATE TABLE IF NOT EXISTS dependencies (
    dep_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id     INTEGER NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    from_path  TEXT NOT NULL,
    to_path    TEXT NOT NULL,
    import_kind TEXT
);
CREATE INDEX IF NOT EXISTS idx_dep_run ON dependencies(run_id);

CREATE TABLE IF NOT EXISTS cycles (
    cycle_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id    INTEGER NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    path_json TEXT NOT NULL,
    length    INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS risk_analysis (
    risk_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       INTEGER NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    path         TEXT NOT NULL,
    churn        INTEGER DEFAULT 0,
    blast_radius INTEGER DEFAULT 0,
    risk_score   REAL DEFAULT 0,
    risk_level   TEXT
);
CREATE INDEX IF NOT EXISTS idx_risk_run ON risk_analysis(run_id);

CREATE TABLE IF NOT EXISTS health_scores (
    score_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id         INTEGER NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    score          REAL NOT NULL,
    churn_penalty  REAL DEFAULT 0,
    cc_penalty     REAL DEFAULT 0,
    cycle_penalty  REAL DEFAULT 0,
    computed_at    TEXT NOT NULL
);
