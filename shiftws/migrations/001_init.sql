-- 5 to 9 Shift workspace schema v1. The database is authoritative; Markdown files are snapshots.
PRAGMA foreign_keys = ON;

CREATE TABLE channel_profile (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

-- Every preference change is a row; the newest active row per (scope, scope_id, key) wins.
-- Precedence when resolving: project > preset > channel.
CREATE TABLE preferences (
  id TEXT PRIMARY KEY,
  scope TEXT NOT NULL CHECK (scope IN ('channel','preset','project')),
  scope_id TEXT NOT NULL,
  key TEXT NOT NULL,
  value_json TEXT NOT NULL,
  provenance TEXT NOT NULL,           -- who/what set it and why
  active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL
);
CREATE INDEX idx_pref_lookup ON preferences(scope, scope_id, key, active);

CREATE TABLE format_presets (
  id TEXT PRIMARY KEY,                -- e.g. 'shorts_narrated'
  channel_id TEXT NOT NULL REFERENCES channel_profile(id),
  name TEXT NOT NULL,
  kind TEXT NOT NULL,                 -- narrated_essay | ranking_short | no_commentary_story | cinematic
  narrated INTEGER NOT NULL,
  settings_json TEXT NOT NULL
);

CREATE TABLE narration_presets (
  id TEXT PRIMARY KEY,
  channel_id TEXT NOT NULL REFERENCES channel_profile(id),
  name TEXT NOT NULL,
  model TEXT,                         -- e.g. eleven_v3 (provisional until user confirms)
  voice TEXT,
  confirmed INTEGER NOT NULL DEFAULT 0,
  settings_json TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE projects (
  id TEXT PRIMARY KEY,
  channel_id TEXT NOT NULL REFERENCES channel_profile(id),
  title TEXT NOT NULL,
  slug TEXT NOT NULL,
  format_preset TEXT NOT NULL REFERENCES format_presets(id),
  stage TEXT NOT NULL DEFAULT 'brief',
  status TEXT NOT NULL DEFAULT 'active',
  root_dir TEXT NOT NULL,
  brief_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE threads (
  id TEXT PRIMARY KEY,
  project_id TEXT REFERENCES projects(id),   -- NULL = channel workspace thread
  kind TEXT NOT NULL,                         -- main | script | footage | music | edit | channel
  title TEXT NOT NULL,
  provider TEXT,
  provider_session_id TEXT,                   -- supplementary only; never the source of truth
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE messages (
  id TEXT PRIMARY KEY,
  thread_id TEXT NOT NULL REFERENCES threads(id),
  role TEXT NOT NULL,                         -- user | assistant | system | tool | job
  content TEXT NOT NULL,
  meta_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
);
CREATE INDEX idx_messages_thread ON messages(thread_id, created_at);

-- Structured memory: facts, decisions, approvals, rejections, preferences, notes.
CREATE TABLE decisions (
  id TEXT PRIMARY KEY,
  scope TEXT NOT NULL CHECK (scope IN ('channel','project')),
  project_id TEXT REFERENCES projects(id),
  thread_id TEXT REFERENCES threads(id),
  kind TEXT NOT NULL,                         -- decision | approval | rejection | fact | preference | technique | note
  subject TEXT NOT NULL,                      -- short label: "SFX: impact for hook"
  content TEXT NOT NULL,                      -- what changed and why
  ref_type TEXT,                              -- asset | script | plan | export | sequence | ...
  ref_id TEXT,
  data_json TEXT NOT NULL DEFAULT '{}',
  status TEXT NOT NULL DEFAULT 'active',      -- active | superseded | deleted
  supersedes TEXT,
  provenance TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX idx_decisions_project ON decisions(project_id, status);

CREATE TABLE tasks (
  id TEXT PRIMARY KEY,
  project_id TEXT REFERENCES projects(id),
  title TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'open',        -- open | done | dropped
  kind TEXT NOT NULL DEFAULT 'todo',          -- todo | handoff | question
  detail TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  done_at TEXT
);

-- One row per distinct file content (sha256). Projects link to assets.
CREATE TABLE media_assets (
  id TEXT PRIMARY KEY,
  sha256 TEXT NOT NULL UNIQUE,
  kind TEXT NOT NULL,                         -- video | audio | image
  title TEXT NOT NULL,
  original_path TEXT NOT NULL,
  current_path TEXT NOT NULL,
  size INTEGER NOT NULL,
  mtime REAL NOT NULL,
  duration REAL,
  fps_num INTEGER, fps_den INTEGER,
  time_base TEXT,
  vfr INTEGER NOT NULL DEFAULT 0,
  vcodec TEXT, acodec TEXT,
  width INTEGER, height INTEGER,
  audio_streams_json TEXT NOT NULL DEFAULT '[]',
  probe_json TEXT NOT NULL DEFAULT '{}',
  tags_json TEXT NOT NULL DEFAULT '[]',
  availability TEXT NOT NULL DEFAULT 'online', -- online | offline
  library TEXT,                                -- channel library name for music/sfx
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE project_assets (
  project_id TEXT NOT NULL REFERENCES projects(id),
  asset_id TEXT NOT NULL REFERENCES media_assets(id),
  role TEXT NOT NULL,                          -- footage | narration | music | sfx | screenshot | download | thumbnail
  label TEXT,
  added_at TEXT NOT NULL,
  PRIMARY KEY (project_id, asset_id, role)
);

CREATE TABLE source_references (
  id TEXT PRIMARY KEY,
  asset_id TEXT REFERENCES media_assets(id),
  project_id TEXT REFERENCES projects(id),
  url TEXT,
  title TEXT,
  extractor TEXT,
  acquired_at TEXT NOT NULL,
  requested_range TEXT,                        -- "start-end" seconds in the source
  source_offset REAL NOT NULL DEFAULT 0,       -- file t=0 corresponds to source t=source_offset
  kind TEXT NOT NULL DEFAULT 'media',          -- media | research
  observed TEXT NOT NULL DEFAULT 'public',     -- public | private | measured | reported | inference | opinion
  notes TEXT NOT NULL DEFAULT '',
  metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE license_records (
  id TEXT PRIMARY KEY,
  asset_id TEXT NOT NULL REFERENCES media_assets(id),
  license TEXT NOT NULL,                       -- 'unverified' when there is no evidence
  author TEXT,
  source TEXT,
  attribution_text TEXT,
  evidence TEXT,
  verified INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);

CREATE TABLE transcript_segments (
  id TEXT PRIMARY KEY,
  asset_id TEXT NOT NULL REFERENCES media_assets(id),
  idx INTEGER NOT NULL,
  start REAL NOT NULL, end REAL NOT NULL,
  text TEXT NOT NULL,
  backend TEXT NOT NULL, model TEXT NOT NULL,
  confidence REAL
);
CREATE INDEX idx_tseg_asset ON transcript_segments(asset_id, start);

CREATE TABLE word_timings (
  asset_id TEXT NOT NULL REFERENCES media_assets(id),
  idx INTEGER NOT NULL,
  start REAL NOT NULL, end REAL NOT NULL,
  word TEXT NOT NULL,
  confidence REAL,
  PRIMARY KEY (asset_id, idx)
);

CREATE TABLE scenes (
  id TEXT PRIMARY KEY,
  asset_id TEXT NOT NULL REFERENCES media_assets(id),
  idx INTEGER NOT NULL,
  start REAL NOT NULL, end REAL NOT NULL,
  frame_path TEXT,
  description TEXT NOT NULL DEFAULT '',
  tags_json TEXT NOT NULL DEFAULT '[]',
  confidence REAL,                             -- of the description; NULL = not inspected
  described_by TEXT,                           -- model/agent that inspected the frame
  metrics_json TEXT NOT NULL DEFAULT '{}',     -- luma, black, motion
  uncertain INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX idx_scenes_asset ON scenes(asset_id, start);

CREATE TABLE evidence_claims (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id),
  script_version_id TEXT,
  section_id TEXT,
  claim TEXT NOT NULL,
  category TEXT NOT NULL,                      -- measured | reported | inference | opinion
  asset_id TEXT, start REAL, end REAL,
  source_ref_id TEXT,
  status TEXT NOT NULL DEFAULT 'unverified',   -- supported | unverified | contradicted
  notes TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);

CREATE TABLE script_versions (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id),
  version INTEGER NOT NULL,
  parent_id TEXT,
  status TEXT NOT NULL DEFAULT 'draft',        -- draft | approved | superseded
  title TEXT NOT NULL DEFAULT '',
  doc_json TEXT NOT NULL,                      -- canonical sections w/ tagged + clean text
  tagged_text TEXT NOT NULL,
  clean_text TEXT NOT NULL,
  word_count INTEGER NOT NULL,
  est_seconds REAL NOT NULL,
  target_model TEXT,
  compat_json TEXT NOT NULL DEFAULT '{}',
  change_note TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  UNIQUE (project_id, version)
);

CREATE TABLE narration_takes (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id),
  asset_id TEXT NOT NULL REFERENCES media_assets(id),
  script_version_id TEXT REFERENCES script_versions(id),
  take_no INTEGER NOT NULL,
  original_sha256 TEXT NOT NULL,
  locked_path TEXT,                            -- read-only copy that the edit uses
  duration REAL NOT NULL,
  generation_settings_json TEXT NOT NULL DEFAULT '{}',   -- user-provided only
  is_synthetic_standin INTEGER NOT NULL DEFAULT 0,       -- local TTS used for tests
  status TEXT NOT NULL DEFAULT 'imported',     -- imported | aligned | locked | replaced
  alignment_json TEXT NOT NULL DEFAULT '{}',
  metrics_json TEXT NOT NULL DEFAULT '{}',
  locked_at TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE audio_analysis (
  asset_id TEXT PRIMARY KEY REFERENCES media_assets(id),
  integrated_lufs REAL, true_peak_db REAL, lra REAL, peak_db REAL, rms_db REAL,
  hit_s REAL,                                  -- strongest transient (impact point)
  onset_s REAL,                                -- first non-silent sample
  spectral_json TEXT NOT NULL DEFAULT '{}',    -- band energy: low/mid/high
  sections_json TEXT NOT NULL DEFAULT '[]',
  classification_json TEXT NOT NULL DEFAULT '{}',  -- label + confidence (heuristic)
  waveform_path TEXT,
  waveform_json TEXT NOT NULL DEFAULT '[]',
  tool_version TEXT NOT NULL,
  analyzed_at TEXT NOT NULL
);

CREATE TABLE audio_usage (
  id TEXT PRIMARY KEY,
  asset_id TEXT NOT NULL REFERENCES media_assets(id),
  project_id TEXT REFERENCES projects(id),
  status TEXT NOT NULL,                        -- approved | rejected | favorite | used
  purpose TEXT NOT NULL DEFAULT '',
  note TEXT NOT NULL DEFAULT '',
  decision_id TEXT REFERENCES decisions(id),
  created_at TEXT NOT NULL
);

CREATE TABLE edit_plan_versions (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id),
  version INTEGER NOT NULL,
  parent_id TEXT,
  plan_json TEXT NOT NULL,
  validation_json TEXT NOT NULL DEFAULT '{}',
  change_summary TEXT NOT NULL DEFAULT '',
  changed_ids_json TEXT NOT NULL DEFAULT '[]',
  status TEXT NOT NULL DEFAULT 'draft',        -- draft | valid | invalid | applied
  created_at TEXT NOT NULL,
  UNIQUE (project_id, version)
);

-- Materialized from plan_json for querying; plan_json is canonical.
CREATE TABLE timeline_clips (plan_id TEXT NOT NULL, clip_id TEXT NOT NULL, track TEXT NOT NULL, asset_id TEXT NOT NULL,
  src_in_frames INTEGER, src_out_frames INTEGER, dst_frames INTEGER, data_json TEXT NOT NULL, PRIMARY KEY (plan_id, clip_id));
CREATE TABLE music_cues (plan_id TEXT NOT NULL, cue_id TEXT NOT NULL, asset_id TEXT NOT NULL, data_json TEXT NOT NULL, PRIMARY KEY (plan_id, cue_id));
CREATE TABLE sfx_cues (plan_id TEXT NOT NULL, cue_id TEXT NOT NULL, asset_id TEXT NOT NULL, data_json TEXT NOT NULL, PRIMARY KEY (plan_id, cue_id));
CREATE TABLE caption_cues (plan_id TEXT NOT NULL, cue_id TEXT NOT NULL, data_json TEXT NOT NULL, PRIMARY KEY (plan_id, cue_id));

-- Cached derived outputs. Keyed by input hash + settings + tool version; marked stale when inputs change.
CREATE TABLE artifacts (
  id TEXT PRIMARY KEY,
  project_id TEXT,
  asset_id TEXT,
  kind TEXT NOT NULL,                          -- proxy | transcript | crv | audio_analysis | preview | captions | export | ...
  cache_key TEXT NOT NULL,
  path TEXT NOT NULL,
  depends_json TEXT NOT NULL DEFAULT '[]',     -- upstream ids (asset:<id>, script:<id>, plan:<id>, take:<id>)
  stale INTEGER NOT NULL DEFAULT 0,
  stale_reason TEXT,
  job_id TEXT,
  created_at TEXT NOT NULL,
  UNIQUE (kind, cache_key)
);

CREATE TABLE jobs (
  id TEXT PRIMARY KEY,
  project_id TEXT,
  thread_id TEXT,
  kind TEXT NOT NULL,
  lane TEXT NOT NULL DEFAULT 'media',          -- media (parallel) | premiere (serialized)
  status TEXT NOT NULL DEFAULT 'queued',       -- queued | running | done | failed | cancelled
  stage TEXT NOT NULL DEFAULT '',
  params_json TEXT NOT NULL DEFAULT '{}',
  input_hash TEXT,
  result_json TEXT NOT NULL DEFAULT '{}',
  outputs_json TEXT NOT NULL DEFAULT '[]',
  checkpoint_json TEXT NOT NULL DEFAULT '{}',
  attempts INTEGER NOT NULL DEFAULT 0,
  max_attempts INTEGER NOT NULL DEFAULT 2,
  progress REAL NOT NULL DEFAULT 0,
  message TEXT NOT NULL DEFAULT '',
  error TEXT,
  error_hint TEXT,
  cancel_requested INTEGER NOT NULL DEFAULT 0,
  origin TEXT NOT NULL DEFAULT 'ui',           -- ui | chat | mcp | cli
  created_at TEXT NOT NULL,
  started_at TEXT, finished_at TEXT, heartbeat_at TEXT
);
CREATE INDEX idx_jobs_status ON jobs(status, lane, created_at);

CREATE TABLE exports (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id),
  plan_version_id TEXT,
  kind TEXT NOT NULL,                          -- preview | youtube | master | short
  path TEXT NOT NULL,
  sha256 TEXT,
  probe_json TEXT NOT NULL DEFAULT '{}',
  qc_json TEXT NOT NULL DEFAULT '{}',
  status TEXT NOT NULL DEFAULT 'rendered',     -- rendered | qc_passed | qc_flagged | published
  created_at TEXT NOT NULL
);

CREATE TABLE performance_imports (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id),
  export_id TEXT REFERENCES exports(id),
  csv_path TEXT NOT NULL,
  report_kind TEXT NOT NULL,                   -- retention | overview | unknown
  mapping_json TEXT NOT NULL,
  rows_json TEXT NOT NULL,
  missing_json TEXT NOT NULL,
  findings_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
);

CREATE TABLE integrations (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  role TEXT NOT NULL,
  status TEXT NOT NULL,                        -- implemented | tested_here | awaiting_mac | unavailable | failed
  version TEXT, detail_json TEXT NOT NULL DEFAULT '{}',
  checked_at TEXT
);

CREATE TABLE premiere_ops (
  id TEXT PRIMARY KEY,                         -- operation id (idempotency key)
  project_id TEXT NOT NULL,
  plan_version_id TEXT,
  sequence_ref TEXT,
  op TEXT NOT NULL,
  params_json TEXT NOT NULL,
  precondition_json TEXT NOT NULL DEFAULT '{}',
  postcondition_json TEXT NOT NULL DEFAULT '{}',
  status TEXT NOT NULL DEFAULT 'pending',      -- pending | sent | verified | failed | skipped
  attempts INTEGER NOT NULL DEFAULT 0,
  result_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);

-- Measures the automation goal: which repetitive steps ran without the user, which needed a handoff.
CREATE TABLE automation_events (
  id TEXT PRIMARY KEY,
  project_id TEXT,
  task TEXT NOT NULL,
  mode TEXT NOT NULL,                          -- automated | handoff | user_decision
  detail TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);

-- Unified full-text index maintained by the application.
CREATE VIRTUAL TABLE search_index USING fts5(
  kind UNINDEXED, ref_id UNINDEXED, project_id UNINDEXED, thread_id UNINDEXED, locator UNINDEXED,
  title, body, tokenize = 'porter unicode61'
);
