import React, { useEffect, useRef, useState } from "react";
import { api, copyText, fileUrl, openExternal, pick, tool } from "./api";
import { Markdown } from "./Markdown";

type Msg = { id: string; role: string; content: string; meta: any; created_at: string };

export function JobCard({ jobId, tick }: { jobId: string; tick: number }) {
  const [j, setJ] = useState<any>(null);
  useEffect(() => { api(`/api/jobs/${jobId}`).then(setJ).catch(() => {}); }, [jobId, tick]);
  if (!j) return <div className="card job">job {jobId}…</div>;
  const r = j.result || {};
  const pct = Math.round((j.progress || 0) * 100);
  const media = (p: string) => /\.(mp4|mov)$/i.test(p) ? <video key={p} src={fileUrl(p)} controls className="preview-video" />
    : /\.(wav|mp3|ogg|m4a)$/i.test(p) ? <audio key={p} src={fileUrl(p)} controls />
    : /\.(png|jpe?g)$/i.test(p) ? <img key={p} src={fileUrl(p)} className="thumb" /> : null;
  return (
    <div className={`card job ${j.status}`}>
      <div className="row"><b>{label(j.kind)}</b><span className={`pill ${j.status}`}>{j.status}{j.status === "running" ? ` ${pct}%` : ""}</span>
        <span className="muted small">{j.id}</span>
        {["queued", "running"].includes(j.status) && <button className="link" onClick={() => api(`/api/jobs/${j.id}/cancel`, { method: "POST" })}>Cancel</button>}
        {["failed", "cancelled"].includes(j.status) && <button className="link" onClick={() => api(`/api/jobs/${j.id}/resume`, { method: "POST" })}>Resume</button>}
      </div>
      {j.status === "running" && <div className="bar"><div style={{ width: `${pct}%` }} /></div>}
      <div className="small">{j.status === "done" ? r.summary : j.message}</div>
      {j.error && <div className="error small">{j.error}{j.error_hint ? <div className="muted">{j.error_hint}</div> : null}</div>}
      {r.preview_frames && <div className="thumbs">{r.preview_frames.map((p: string) => <img key={p} src={fileUrl(p)} className="thumb" />)}</div>}
      {r.path && /\.md$/.test(r.path) && <ReportCard path={r.path} rev={r.revision} sections={r.sections} />}
      {r.path && !/\.md$/.test(r.path) && media(r.path)}
      {r.waveform && <img src={fileUrl(r.waveform)} className="wave" />}
      {r.flags && <QcList flags={r.flags} status={r.status} />}
      {r.folder && <button className="link" onClick={() => openExternal(r.folder)}>Open package folder</button>}
      {r.studio_url && <button className="primary" onClick={() => openExternal(r.studio_url)}>Open YouTube Studio</button>}
    </div>
  );
}

function ReportCard({ path, rev, sections }: { path: string; rev?: number; sections?: number }) {
  return <div className="card artifact"><div className="row"><b>📄 {path.split("/").pop()}</b><span className="muted small">rev {rev} · {sections} sections</span></div>
    <div className="small muted">Linked to this chat. Later turns pull only the relevant sections — no re-upload needed.</div>
    <button className="link" onClick={() => openExternal(path)}>Open report</button></div>;
}

function QcList({ flags, status }: { flags: any[]; status: string }) {
  return <div className="qc"><div className="small"><b>QC {status}</b> — flags are review candidates, not proof of mistakes</div>
    <ul className="small">{flags.map((f, i) => <li key={i} className={f.severity}>[{f.severity}] {f.item}: {f.detail}{f.at != null ? ` @ ${Number(f.at).toFixed(2)}s` : ""}</li>)}</ul></div>;
}

export function ScriptCard({ projectId }: { projectId: string }) {
  const [s, setS] = useState<any>(null);
  useEffect(() => { tool("script_get", { project_id: projectId }).then((r) => setS(r.data)); }, [projectId]);
  if (!s) return null;
  return <div className="card script">
    <div className="row"><b>Script v{s.version}</b><span className={`pill ${s.status}`}>{s.status}</span><span className="muted small">{s.word_count} words · ~{Math.round(s.est_seconds)}s · {s.compat?.model}{s.compat?.model_confirmed ? "" : " (provisional)"}</span></div>
    <pre className="tagged">{s.tagged_text}</pre>
    <div className="row">
      <button className="primary" onClick={() => copyText(s.tagged_text)}>Copy for ElevenLabs</button>
      <button onClick={() => copyText(s.clean_text)}>Copy clean text</button>
      <button className="link" onClick={() => openExternal("https://elevenlabs.io/app/speech-synthesis")}>Open ElevenLabs</button>
    </div>
    {s.compat?.warnings?.length ? <ul className="small muted">{s.compat.warnings.map((w: string, i: number) => <li key={i}>{w}</li>)}</ul> : null}
  </div>;
}

function label(kind: string) {
  return ({ analysis_report: "Analysis report", analyze_video: "Video analysis", import_media: "Import", download: "Download",
    narration_align: "Narration alignment", render_preview: "Render", qc: "Quality check", upload_package: "Upload package",
    audio_preview: "Audio preview", index_audio_library: "Catalog indexing", dense_pass: "Dense frame pass", inventory: "Recording inventory",
    premiere_apply: "Premiere assembly", premiere_inspect: "Premiere inspection", transcribe: "Transcription" } as any)[kind] || kind;
}

export function Chat({ threadId, projectId, tick, focusMessage }: { threadId: string; projectId: string | null; tick: number; focusMessage?: string }) {
  const [msgs, setMsgs] = useState<Msg[]>([]);
  const [resume, setResume] = useState<any>(null);
  const [text, setText] = useState("");
  const [attach, setAttach] = useState<string[]>([]);
  const [busy, setBusy] = useState<string | null>(null);
  const [live, setLive] = useState("");
  const bottom = useRef<HTMLDivElement>(null);

  useEffect(() => { api(`/api/threads/${threadId}/resume`).then(setResume); setLive(""); }, [threadId]);
  useEffect(() => { api(`/api/threads/${threadId}/messages`).then((r) => setMsgs(r.messages)); }, [threadId, tick]);
  useEffect(() => {
    if (focusMessage) document.getElementById(focusMessage)?.scrollIntoView({ block: "center" });
    else bottom.current?.scrollIntoView();
  }, [msgs.length, focusMessage]);
  useEffect(() => {
    const h = (e: any) => {
      const d = e.detail;
      if (d.thread_id !== threadId) return;
      if (d.kind === "chat.delta") setLive((l) => (l ? l + "\n\n" : "") + d.payload.text);
      if (d.kind === "chat.turn_done") { setBusy(null); setLive(""); }
    };
    window.addEventListener("shiftws-event", h);
    return () => window.removeEventListener("shiftws-event", h);
  }, [threadId]);

  const send = async () => {
    if (!text.trim() && !attach.length) return;
    const r = await api(`/api/threads/${threadId}/send`, { body: { text: text || "(files attached)", attachments: attach } });
    setBusy(r.turn_id); setText(""); setAttach([]);
  };
  const onDrop = (e: React.DragEvent) => {
    e.preventDefault();
    const files = Array.from(e.dataTransfer.files).map((f) => window.shiftws?.pathForFile(f) || (f as any).path).filter(Boolean);
    setAttach((a) => [...a, ...files]);
  };

  return (
    <div className="chat" onDragOver={(e) => e.preventDefault()} onDrop={onDrop}>
      {resume && <div className="resume card"><div className="small muted">Where we stopped</div><div>{resume.summary}</div>
        <div><b>Next:</b> {resume.next_action}</div>
        {resume.open_tasks?.length ? <div className="small muted">Open: {resume.open_tasks.map((t: any) => t.title).join(" · ")}</div> : null}</div>}
      <div className="messages">
        {msgs.map((m) => <Message key={m.id} m={m} projectId={projectId} tick={tick} />)}
        {live && <div className="msg assistant live"><Markdown text={live} /></div>}
        {busy && !live && <div className="msg system small muted">Working… <button className="link" onClick={() => api(`/api/turns/${busy}/cancel`, { method: "POST" })}>Stop</button></div>}
        <div ref={bottom} />
      </div>
      <div className="composer">
        {attach.length > 0 && <div className="attach small">{attach.map((a) => <span key={a} className="chip">{a.split("/").pop()} <button className="link" onClick={() => setAttach(attach.filter((x) => x !== a))}>×</button></span>)}</div>}
        <textarea value={text} onChange={(e) => setText(e.target.value)} placeholder="Ask for work: “analyze these recordings”, “write the script with my ElevenLabs tags”, “replace the music without touching narration”…"
          onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); } }} />
        <div className="row">
          <button onClick={async () => setAttach([...attach, ...(await pick("files"))])}>Add files</button>
          <button onClick={async () => setAttach([...attach, ...(await pick("folder"))])}>Grant folder</button>
          <span className="spacer" />
          <button className="primary" onClick={send} disabled={!!busy}>Send</button>
        </div>
      </div>
    </div>
  );
}

function Message({ m, projectId, tick }: { m: Msg; projectId: string | null; tick: number }) {
  if (m.role === "job") return <div id={m.id}><JobCard jobId={m.meta.job_id} tick={tick} /></div>;
  if (m.role === "tool") {
    const t = m.meta.tool;
    return <div id={m.id} className="toolchip small">⚙ {t}{t === "script_save" && projectId ? <ScriptCard projectId={projectId} /> : null}</div>;
  }
  return <div id={m.id} className={`msg ${m.role}`}>
    {m.role === "assistant" && m.meta?.provider && <div className="small muted">{m.meta.provider === "claude_code" ? "Claude Code" : m.meta.provider}{m.meta.seconds ? ` · ${m.meta.seconds}s` : ""}</div>}
    <Markdown text={m.content} />
    {m.meta?.artifact && <ReportCard path={m.meta.artifact} />}
    {m.meta?.choices && <div className="row">{m.meta.choices.map((c: any) => <button key={c.project_id} onClick={() => tool("narration_import", { path: m.meta.file, project_id: c.project_id })}>{c.title}</button>)}</div>}
    {m.meta?.attachments?.length ? <div className="small muted">📎 {m.meta.attachments.map((a: string) => a.split("/").pop()).join(", ")}</div> : null}
  </div>;
}
