-- Revised spec: deliverables, analysis reports, coverage accounting, attachments, tool events, corrections,
-- lessons, audio catalog folders/notifications.

-- One project (source footage + analysis) can produce several deliverables (Shorts, long-form).
CREATE TABLE deliverables (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id),
  title TEXT NOT NULL,
  format_preset TEXT NOT NULL REFERENCES format_presets(id),
  length_seconds INTEGER,
  status TEXT NOT NULL DEFAULT 'active',
  thread_id TEXT,
  created_at TEXT NOT NULL
);
ALTER TABLE script_versions ADD COLUMN deliverable_id TEXT;
ALTER TABLE edit_plan_versions ADD COLUMN deliverable_id TEXT;
ALTER TABLE exports ADD COLUMN deliverable_id TEXT;
ALTER TABLE narration_takes ADD COLUMN deliverable_id TEXT;

-- Versioned Markdown analysis reports, tied to project + originating thread + exact source hashes/settings.
CREATE TABLE reports (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id),
  thread_id TEXT,
  kind TEXT NOT NULL,                          -- analysis | deep_dive | inventory | mechanic
  title TEXT NOT NULL,
  revision INTEGER NOT NULL,
  parent_id TEXT,
  path TEXT NOT NULL,
  content_sha256 TEXT NOT NULL,                -- of the file as written/reconciled; external edits are detected
  source_hashes_json TEXT NOT NULL,            -- {asset_id: sha256}
  settings_json TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'current',      -- current | stale | superseded
  stale_reason TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE report_sections (
  report_id TEXT NOT NULL REFERENCES reports(id),
  idx INTEGER NOT NULL,
  heading TEXT NOT NULL,
  body TEXT NOT NULL,
  asset_id TEXT, start REAL, end REAL,
  PRIMARY KEY (report_id, idx)
);

-- Which frames / contact sheets / intervals were actually reviewed, by whom, with what result.
CREATE TABLE coverage_records (
  id TEXT PRIMARY KEY,
  asset_id TEXT NOT NULL REFERENCES media_assets(id),
  project_id TEXT,
  kind TEXT NOT NULL,                          -- frame | sheet | interval | dense_pass
  ref TEXT NOT NULL,                           -- frame path / sheet path / "start-end"
  start REAL, end REAL,
  reviewed_by TEXT,                            -- NULL = generated but not yet reviewed
  result TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  reviewed_at TEXT
);
CREATE INDEX idx_cov_asset ON coverage_records(asset_id, kind);

-- Mechanic / claim investigations with explicit evidence status.
CREATE TABLE investigations (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  question TEXT NOT NULL,
  status TEXT NOT NULL,                        -- confirmed | suggestive | speculation | no_evidence_in_inspected
  evidence_json TEXT NOT NULL DEFAULT '[]',    -- [{asset_id, t, frame, note}]
  inspected_json TEXT NOT NULL DEFAULT '{}',   -- coverage summary at the time of the verdict
  note TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);

CREATE TABLE attachments (
  id TEXT PRIMARY KEY,
  message_id TEXT NOT NULL REFERENCES messages(id),
  path TEXT NOT NULL,
  asset_id TEXT,
  handled_as TEXT NOT NULL,                    -- media_import | narration | folder | report
  created_at TEXT NOT NULL
);

CREATE TABLE tool_events (
  id TEXT PRIMARY KEY,
  thread_id TEXT NOT NULL,
  turn_id TEXT NOT NULL,
  tool TEXT NOT NULL,
  args_json TEXT NOT NULL,
  result_json TEXT,
  job_ids_json TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL
);

-- Corrections: a claim that was wrong, what replaced it, so it does not reappear after reopening/compaction.
CREATE TABLE corrections (
  id TEXT PRIMARY KEY,
  project_id TEXT,
  wrong TEXT NOT NULL,
  correct TEXT NOT NULL,
  decision_id TEXT,
  superseded_decision_id TEXT,
  source_message_id TEXT,
  created_at TEXT NOT NULL
);

-- Audio catalog folders (channel-wide) and change notifications.
CREATE TABLE catalog_folders (
  id TEXT PRIMARY KEY,
  path TEXT NOT NULL UNIQUE,
  library TEXT NOT NULL,
  role TEXT,                                   -- music | sfx | NULL (infer)
  last_scan_at TEXT,
  available INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE notifications (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,                          -- catalog.added | catalog.removed | catalog.offline | reminder | ...
  title TEXT NOT NULL,
  body TEXT NOT NULL,
  ref_id TEXT,
  project_id TEXT,
  read INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);
ALTER TABLE media_assets ADD COLUMN catalog_state TEXT;   -- present | offline (drive) | removed (file gone, drive present)
