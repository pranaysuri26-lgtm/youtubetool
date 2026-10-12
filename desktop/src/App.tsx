import React, { useEffect, useState } from "react";
import { api, connect, events, tool } from "./api";
import { Chat } from "./Chat";
import { Inspector } from "./Inspector";

type Sel = { threadId: string; projectId: string | null; focusMessage?: string };
const KINDS = ["research", "script", "footage", "audio", "edit", "results"];

export function App() {
  const [ready, setReady] = useState(false);
  const [data, setData] = useState<any>(null);
  const [sel, setSel] = useState<Sel | null>(null);
  const [tick, setTick] = useState(0);
  const [q, setQ] = useState("");
  const [scope, setScope] = useState("all");
  const [results, setResults] = useState<any[] | null>(null);
  const [focus, setFocus] = useState<any>(undefined);
  const [newProj, setNewProj] = useState(false);

  useEffect(() => { connect().then(() => setReady(true)); }, []);
  const load = () => api("/api/projects").then((d) => { setData(d); if (!sel) setSel({ threadId: d.channel_thread.id, projectId: null }); });
  useEffect(() => { if (ready) load(); }, [ready, tick]);
  useEffect(() => {
    if (!ready) return;
    let t: any = null;
    return events((e) => {
      window.dispatchEvent(new CustomEvent("shiftws-event", { detail: e }));
      if (e.kind !== "chat.delta") { clearTimeout(t); t = setTimeout(() => setTick((x) => x + 1), 250); }
    });
  }, [ready]);

  const search = async () => {
    if (!q.trim()) return setResults(null);
    const r = await api(`/api/search?q=${encodeURIComponent(q)}&scope=${scope}&project_id=${sel?.projectId || ""}&thread_id=${sel?.threadId || ""}`);
    setResults(r.results);
  };
  const openResult = (r: any) => {
    const loc = r.locator || {};
    const project = data.projects.find((p: any) => p.id === r.project_id);
    if (loc.thread_id) setSel({ threadId: loc.thread_id, projectId: r.project_id, focusMessage: loc.message_id });
    else if (project) setSel({ threadId: project.threads.find((t: any) => t.kind === "main")?.id, projectId: project.id });
    if (loc.asset_id) setFocus({ asset_id: loc.asset_id, t: loc.t });
  };

  if (!ready || !data) return <div className="boot">Starting the workspace engine…</div>;
  return <div className="app">
    <aside className="sidebar">
      <div className="brand">5 to 9 Shift</div>
      <div className="search">
        <input value={q} onChange={(e) => setQ(e.target.value)} onKeyDown={(e) => e.key === "Enter" && search()} placeholder="Search everything…" />
        <select value={scope} onChange={(e) => setScope(e.target.value)}><option value="thread">this thread</option><option value="project">this project</option><option value="all">all projects</option></select>
      </div>
      {results && <div className="results"><div className="small muted">{results.length} result(s) <button className="link" onClick={() => setResults(null)}>clear</button></div>
        {results.map((r, i) => <div key={i} className="result" onClick={() => openResult(r)}><span className="pill">{r.kind}</span> <b>{r.title}</b>
          <div className="small muted">{r.project_title || "channel"}{r.locator?.t != null ? ` · @${Number(r.locator.t).toFixed(1)}s` : ""}</div>
          <div className="small">{r.snippet}</div></div>)}</div>}
      <div className={`item ${sel?.threadId === data.channel_thread.id ? "on" : ""}`} onClick={() => setSel({ threadId: data.channel_thread.id, projectId: null })}>◎ Channel workspace</div>
      <div className="section-title">Projects <button className="link" onClick={() => setNewProj(!newProj)}>+ new</button></div>
      {newProj && <NewProject onDone={(r) => { setNewProj(false); setTick(tick + 1); if (r) setSel({ threadId: r.main_thread_id, projectId: r.project_id }); }} />}
      {data.projects.map((p: any) => <div key={p.id} className="project">
        <div className={`item ${sel?.projectId === p.id ? "on" : ""}`} onClick={() => setSel({ threadId: p.threads.find((t: any) => t.kind === "main").id, projectId: p.id })}>
          <b>{p.title}</b><div className="small muted">{p.stage}</div></div>
        {sel?.projectId === p.id && <div className="threads">
          {p.threads.map((t: any) => <div key={t.id} className={`thread ${sel?.threadId === t.id ? "on" : ""}`} onClick={() => setSel({ threadId: t.id, projectId: p.id })}># {t.kind === "main" ? "project chat" : t.title}</div>)}
          <select value="" onChange={async (e) => { const t = await api("/api/threads", { body: { project_id: p.id, kind: e.target.value } }); setTick(tick + 1); setSel({ threadId: t.id, projectId: p.id }); }}>
            <option value="">+ thread…</option>{KINDS.filter((k) => !p.threads.some((t: any) => t.kind === k)).map((k) => <option key={k} value={k}>{k}</option>)}</select>
        </div>}
      </div>)}
    </aside>
    {sel && <Chat threadId={sel.threadId} projectId={sel.projectId} tick={tick} focusMessage={sel.focusMessage} />}
    <Inspector projectId={sel?.projectId || null} tick={tick} focus={focus} />
  </div>;
}

function NewProject({ onDone }: { onDone: (r: any) => void }) {
  const [title, setTitle] = useState("");
  const [preset, setPreset] = useState("short_narrated");
  const [len, setLen] = useState("");
  return <div className="card small">
    <input placeholder="Title or game" value={title} onChange={(e) => setTitle(e.target.value)} />
    <select value={preset} onChange={(e) => setPreset(e.target.value)}>
      <option value="short_narrated">Short — narrated</option><option value="short_no_commentary">Short — no commentary</option>
      <option value="long_narrated">Long-form — narrated</option><option value="long_no_commentary">Long-form — no commentary</option><option value="cinematic">Cinematic</option></select>
    <input placeholder="Length (s) — default from preset" value={len} onChange={(e) => setLen(e.target.value)} />
    <button className="primary" onClick={async () => { const r = await tool("project_create", { title, format_preset: preset, length_seconds: len ? Number(len) : null }); onDone(r.data); }}>Create</button>
  </div>;
}
