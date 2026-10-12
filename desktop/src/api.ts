// Engine client. In Electron the preload bridge provides URL + token; for headless browser testing the same
// values can be passed as ?engine=…&token=… (loopback only).
declare global {
  interface Window {
    shiftws?: {
      engine: () => Promise<{ url: string; token: string }>;
      pick: (k: "files" | "folder") => Promise<string[]>;
      copy: (t: string) => Promise<boolean>;
      open: (t: string) => Promise<unknown>;
      pathForFile: (f: File) => string;
    };
  }
}

let ENGINE = { url: "", token: "" };

export async function connect(): Promise<typeof ENGINE> {
  if (window.shiftws) ENGINE = await window.shiftws.engine();
  else {
    const q = new URLSearchParams(location.search);
    ENGINE = { url: q.get("engine") || "http://127.0.0.1:8765", token: q.get("token") || "" };
  }
  return ENGINE;
}

export async function api<T = any>(path: string, opts: { method?: string; body?: unknown } = {}): Promise<T> {
  const r = await fetch(ENGINE.url + path, {
    method: opts.method || (opts.body ? "POST" : "GET"),
    headers: { "x-shiftws-token": ENGINE.token, "content-type": "application/json" },
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
  return r.json();
}

export const tool = (name: string, args: Record<string, unknown> = {}, scope: Record<string, unknown> = {}) =>
  api(`/api/tools/${name}`, { body: { ...args, _scope: scope } });

export function fileUrl(p: string): string {
  return `${ENGINE.url}/api/file?path=${encodeURIComponent(p)}&token=${encodeURIComponent(ENGINE.token)}`;
}

export function events(onEvent: (e: any) => void, since = 0): () => void {
  const es = new EventSource(`${ENGINE.url}/api/events?since=${since}&token=${encodeURIComponent(ENGINE.token)}`);
  es.onmessage = (m) => { try { onEvent(JSON.parse(m.data)); } catch { /* ignore */ } };
  return () => es.close();
}

export async function copyText(t: string) {
  if (window.shiftws) return window.shiftws.copy(t);
  return navigator.clipboard?.writeText(t);
}

export async function openExternal(t: string) {
  if (window.shiftws) return window.shiftws.open(t);
  window.open(t, "_blank");
}

export async function pick(kind: "files" | "folder"): Promise<string[]> {
  if (window.shiftws) return window.shiftws.pick(kind);
  const p = prompt(kind === "folder" ? "Folder path to grant:" : "File path(s), comma-separated:");
  return p ? p.split(",").map((s) => s.trim()).filter(Boolean) : [];
}
