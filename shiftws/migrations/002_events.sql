-- Append-only event log so every process (UI API, workers, MCP server) can publish progress
-- and the desktop UI can tail it, including jobs started by Claude Code through MCP.
CREATE TABLE events (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL,
  project_id TEXT,
  thread_id TEXT,
  ref_id TEXT,
  payload_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
);
CREATE INDEX idx_events_project ON events(project_id, seq);
