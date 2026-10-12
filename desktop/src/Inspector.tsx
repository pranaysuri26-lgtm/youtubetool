import React, { useEffect, useState } from "react";
import { api, fileUrl, openExternal, tool } from "./api";
import { JobCard, ScriptCard } from "./Chat";

const TABS = ["Overview", "Script", "Media", "Plan", "Exports", "Memory", "Alerts", "Connections"] as const;
type Tab = typeof TABS[number];

export function Inspector({ projectId, tick, focus }: { projectId: string | null; tick: number; focus?: { asset_id?: string; t?: number } }) {
  const [tab, setTab] = useState<Tab>(projectId ? "Overview" : "Connections");
  useEffect(() => { if (focus?.asset_id) setTab("Media"); }, [focus]);
  return <aside className="inspector">
    <nav className="tabs">{TABS.map((t) => <button key={t} className={t === tab ? "on" : ""} onClick={() => setTab(t)}>{t}</button>)}</nav>
    <div className="pane">
      {tab === "Overview" && projectId && <Overview pid={projectId} tick={tick} />}
      {tab === "Script" && projectId && <ScriptCard key={tick} projectId={projectId} />}
      {tab === "Media" && projectId && <Media pid={projectId} tick={tick} focus={focus} />}
      {tab === "Plan" && projectId && <Plan pid={projectId} tick={tick} />}
      {tab === "Exports" && projectId && <Exports pid={projectId} tick={tick} />}
      {tab === "Memory" && <Memory pid={projectId} tick={tick} />}
      {tab === "Alerts" && <Alerts tick={tick} />}
      {tab === "Connections" && <Connections />}
      {!projectId && !["Memory", "Alerts", "Connections"].includes(tab) && <div className="muted">Open a project chat.</div>}
    </div>
  </aside>;
}

function Overview({ pid, tick }: { pid: string; tick: number }) {
  const [c, setC] = useState<any>(null);
  const [auto, setAuto] = useState<any>(null);
  useEffect(() => { api(`/api/projects/${pid}/context`).then(setC); api(`/api/automation?project_id=${pid}`).then(setAuto); }, [pid, tick]);
  if (!c) return null;
  return <div>
    <h3>{c.project.title}</h3>
    <div className="small muted">{c.project.format_preset} · stage <b>{c.project.stage}</b></div>
    <div className="card"><div>{c.where_we_stopped.summary}</div><div><b>Next:</b> {c.where_we_stopped.next_action}</div></div>
    <h4>Deliverables</h4><ul className="small">{c.deliverables.map((d: any) => <li key={d.id}>{d.title} — {d.format_preset}{d.length_seconds ? `, ${d.length_seconds}s` : ""}</li>)}</ul>
    <h4>Open tasks</h4><ul className="small">{c.open_tasks.map((t: any) => <li key={t.id}>{t.kind === "handoff" ? "↗ " : ""}{t.title}</li>)}</ul>
    <h4>Reports</h4><ul className="small">{c.reports.map((r: any) => <li key={r.id}><button className="link" onClick={() => openExternal(r.path)}>{r.title}</button> <span className="muted">{r.status}</span></li>)}</ul>
    <h4>Running</h4>{c.active_jobs.filter((j: any) => j.status !== "failed").map((j: any) => <JobCard key={j.id} jobId={j.id} tick={tick} />)}
    {auto && auto.steps > 0 && <><h4>Automation (measured)</h4><div className="small">{auto.totals.automated} automated steps · {auto.totals.handoff} handoffs · {auto.totals.user_decision} decisions</div>
      <div className="small muted">{auto.note}</div></>}
  </div>;
}

function Media({ pid, tick, focus }: { pid: string; tick: number; focus?: { asset_id?: string; t?: number } }) {
  const [assets, setAssets] = useState<any[]>([]);
  const [scenes, setScenes] = useState<any[]>([]);
  const [sel, setSel] = useState<string | undefined>(focus?.asset_id);
  useEffect(() => { tool("assets_list", { project_id: pid }).then((r) => { const list = r.data || []; setAssets(list);
    setSel((cur) => cur || list.find((x: any) => x.kind === "video")?.id); }); }, [pid, tick]);
  useEffect(() => { if (focus?.asset_id) setSel(focus.asset_id); }, [focus]);
  useEffect(() => { if (sel) tool("scene_frames", { asset_id: sel, limit: 60 }).then((r) => setScenes(r.data || [])); }, [sel, tick]);
  const a = assets.find((x) => x.id === sel);
  return <div>
    <ul className="small list">{assets.map((x) => <li key={x.id} className={x.id === sel ? "on" : ""} onClick={() => setSel(x.id)}>
      <b>{x.title}</b> <span className="muted">{x.role} · {x.kind} · {x.duration ? `${x.duration.toFixed(1)}s` : ""} {x.width ? `${x.width}x${x.height}` : ""} {x.rate || ""}</span>
      {x.availability !== "online" && <span className="pill failed">offline</span>} <span className="muted">· {x.license}</span></li>)}</ul>
    {a && a.kind === "video" && <video key={`${a.id}-${focus?.t}`} className="preview-video" controls src={`${fileUrl(a.current_path)}#t=${focus?.t || 0}`} />}
    {a && a.kind === "audio" && <audio controls src={fileUrl(a.current_path)} />}
    <div className="scenes">{scenes.map((s) => <figure key={s.id} className={s.confidence == null ? "uninspected" : ""}>
      <img src={fileUrl(s.frame_path)} /><figcaption className="small">{s.start.toFixed(1)}s {s.description ? `· ${s.description.slice(0, 80)} (${Math.round(s.confidence * 100)}%)` : "· not inspected"}</figcaption></figure>)}</div>
  </div>;
}

function Plan({ pid, tick }: { pid: string; tick: number }) {
  const [p, setP] = useState<any>(null);
  useEffect(() => { tool("edit_plan_get", { project_id: pid }).then((r) => setP(r.data)); }, [pid, tick]);
  if (!p) return <div className="muted">No edit plan yet.</div>;
  const plan = p.plan, fps = plan.sequence.fps.num / plan.sequence.fps.den;
  return <div className="small">
    <div><b>Plan v{p.version}</b> — {p.change_summary} · {plan.sequence.width}x{plan.sequence.height} @ {plan.sequence.fps.num}/{plan.sequence.fps.den}</div>
    <div className={p.validation.valid ? "ok" : "error"}>{p.validation.valid ? "valid" : "invalid"} {p.validation.errors.join("; ")}</div>
    {p.validation.warnings.length ? <details><summary>{p.validation.warnings.length} warnings</summary><ul>{p.validation.warnings.map((w: string, i: number) => <li key={i}>{w}</li>)}</ul></details> : null}
    <table className="md-table"><thead><tr><th>id</th><th>track</th><th>at</th><th>len</th><th>evidence</th></tr></thead><tbody>
      {plan.clips.map((c: any) => <tr key={c.id}><td>{c.id}</td><td>{c.track}</td><td>{(c.dst_in / fps).toFixed(2)}s</td><td>{((c.dst_out - c.dst_in) / fps).toFixed(2)}s</td><td>{c.evidence.basis}</td></tr>)}
      {plan.music.map((c: any) => <tr key={c.id}><td>{c.id}</td><td>{c.track}</td><td>{(c.dst_in / fps).toFixed(2)}s</td><td>{c.gain_db} dB</td><td>{c.purpose}</td></tr>)}
      {plan.sfx.map((c: any) => <tr key={c.id}><td>{c.id}</td><td>{c.track}</td><td>{(c.at_frame / fps).toFixed(2)}s</td><td>{c.gain_db} dB</td><td>{c.purpose} (hit {c.hit_offset_s}s)</td></tr>)}
    </tbody></table>
    <div className="muted">Narration: {plan.narration ? `take locked · ${plan.narration.sha256.slice(0, 10)}… on A1` : "none (no-commentary)"} · captions: {plan.captions.cues.length} cues ({plan.captions.style.style})</div>
  </div>;
}

function Exports({ pid, tick }: { pid: string; tick: number }) {
  const [c, setC] = useState<any>(null);
  useEffect(() => { api(`/api/projects/${pid}/context`).then(setC); }, [pid, tick]);
  const e = c?.latest_export;
  if (!e) return <div className="muted">Nothing rendered yet.</div>;
  return <div><div className="small"><b>{e.kind}</b> · {e.status}</div><video className="preview-video" controls src={fileUrl(e.path)} />
    <button className="primary" onClick={() => openExternal("https://studio.youtube.com/")}>Open YouTube Studio</button></div>;
}

function Memory({ pid, tick }: { pid: string | null; tick: number }) {
  const [items, setItems] = useState<any[]>([]);
  const [edit, setEdit] = useState<{ id: string; text: string } | null>(null);
  const load = () => api(`/api/memory${pid ? `?project_id=${pid}` : ""}`).then((r) => setItems(r.items));
  useEffect(() => { load(); }, [pid, tick]);
  return <div className="small"><div className="muted">Structured memory (decisions, approvals, rejections, facts). Correct or delete anything.</div>
    <ul className="list">{items.map((d) => <li key={d.id}><span className={`pill ${d.kind}`}>{d.kind}</span> <b>{d.subject}</b> <span className="muted">{d.scope}{d.created_at ? ` · ${d.created_at.slice(0, 10)}` : ""}</span>
      {edit?.id === d.id ? <div><textarea value={edit?.text || ""} onChange={(e) => setEdit({ id: d.id, text: e.target.value })} />
        <button onClick={async () => { await api(`/api/memory/${d.id}`, { body: { content: edit?.text } }); setEdit(null); load(); }}>Save correction</button></div>
        : <div>{d.content}</div>}
      <button className="link" onClick={() => setEdit({ id: d.id, text: d.content })}>Correct</button>
      <button className="link" onClick={async () => { await api(`/api/memory/${d.id}`, { method: "DELETE" }); load(); }}>Delete</button></li>)}</ul></div>;
}

function Alerts({ tick }: { tick: number }) {
  const [n, setN] = useState<any[]>([]);
  useEffect(() => { api("/api/notifications").then((r) => setN(r.items)); }, [tick]);
  return <ul className="small list">{n.map((x) => <li key={x.id}><b>{x.title}</b><div className="muted">{x.body}</div></li>)}</ul>;
}

function Connections() {
  const [c, setC] = useState<any>(null);
  const [pr, setPr] = useState<any>(null);
  const [doc, setDoc] = useState<string>("");
  useEffect(() => { api("/api/connections").then(setC); }, []);
  if (!c) return null;
  return <div className="small">
    <h4>AI provider</h4>
    {Object.entries(c.providers).map(([k, v]: any) => <div key={k}><span className={`pill ${v.ok ? "done" : "failed"}`}>{v.ok ? "ok" : "off"}</span> {k === "claude_code" ? "Claude Code (your installed CLI)" : k} {k === c.active_provider ? "· active" : ""} <span className="muted">{v.version || v.reason || v.note || ""}</span></div>)}
    <div className="muted">The app never reads or stores Claude credentials; it runs your logged-in CLI in its documented non-interactive mode.</div>
    <h4>Premiere</h4>
    {pr ? <div>{["server", "host", "panel", "export_backend"].map((k) => <div key={k}>{k}: <b>{String(pr[k])}</b></div>)}{pr.fix && <div className="muted">{pr.fix}</div>}</div>
      : <button onClick={() => api("/api/premiere").then(setPr)}>Check Premiere (read-only)</button>}
    <h4>Integrations</h4>
    <table className="md-table"><tbody>{c.integrations.map((i: any) => <tr key={i.id}><td>{i.id}</td><td><span className={`pill ${i.status}`}>{i.status}</span></td><td className="muted">{i.package || i.version || ""}</td></tr>)}</tbody></table>
    <h4>Health</h4>{doc ? <pre className="doctor">{doc}</pre> : <button onClick={() => api("/api/doctor").then((r) => setDoc(r.markdown))}>Run health report</button>}
  </div>;
}
