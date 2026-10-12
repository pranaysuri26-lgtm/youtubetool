// Electron main process: starts (or reuses) the local Python engine on loopback, hands the renderer the
// engine URL + session token through a narrow preload bridge, and provides OS dialogs for granting files/folders.
// No Claude credentials are ever read here; the chat uses the user's installed Claude Code CLI via the engine.
import { app, BrowserWindow, clipboard, dialog, ipcMain, shell } from "electron";
import { ChildProcess, spawn } from "child_process";
import * as fs from "fs";
import * as os from "os";
import * as path from "path";
import * as http from "http";

const REPO = path.resolve(__dirname, "..", "..");
let engine: ChildProcess | null = null;
let engineInfo: { url: string; token: string } | null = null;

function homeDir(): string {
  if (process.env.SHIFTWS_HOME) return process.env.SHIFTWS_HOME;
  if (process.platform === "darwin") return path.join(os.homedir(), "Library", "Application Support", "5to9Shift");
  if (process.platform === "win32") return path.join(process.env.APPDATA || os.homedir(), "5to9Shift");
  return path.join(process.env.XDG_DATA_HOME || path.join(os.homedir(), ".local", "share"), "shiftws");
}

function python(): string {
  const venv = process.platform === "win32" ? path.join(REPO, ".venv", "Scripts", "python.exe") : path.join(REPO, ".venv", "bin", "python");
  return fs.existsSync(venv) ? venv : "python3";
}

function ping(url: string): Promise<boolean> {
  return new Promise((resolve) => {
    const req = http.get(url + "/api/ping", (res) => { res.resume(); resolve(res.statusCode === 200); });
    req.on("error", () => resolve(false));
    req.setTimeout(800, () => { req.destroy(); resolve(false); });
  });
}

async function startEngine(): Promise<{ url: string; token: string }> {
  const home = homeDir();
  const port = Number(process.env.SHIFTWS_PORT || 8765);
  const url = `http://127.0.0.1:${port}`;
  if (!(await ping(url))) {
    // argument array, never a shell string
    engine = spawn(python(), ["-m", "shiftws.cli", "--home", home, "serve", "--port", String(port)], {
      cwd: REPO, env: { ...process.env, PYTHONPATH: REPO }, stdio: ["ignore", "pipe", "pipe"],
    });
    engine.stderr?.on("data", (d) => fs.appendFileSync(path.join(home, "logs", "engine.log"), d));
    for (let i = 0; i < 80 && !(await ping(url)); i++) await new Promise((r) => setTimeout(r, 250));
  }
  const token = fs.readFileSync(path.join(home, "api_token"), "utf8").trim();
  return { url, token };
}

async function createWindow() {
  engineInfo = await startEngine();
  const win = new BrowserWindow({
    width: 1440, height: 900, title: "5 to 9 Shift", backgroundColor: "#111214",
    webPreferences: { preload: path.join(__dirname, "preload.js"), contextIsolation: true, nodeIntegration: false, sandbox: true },
  });
  win.webContents.setWindowOpenHandler(({ url }) => { if (/^https:\/\//.test(url)) shell.openExternal(url); return { action: "deny" }; });
  await win.loadFile(path.join(REPO, "desktop", "dist", "index.html"));
}

ipcMain.handle("engine", () => engineInfo);
ipcMain.handle("pick", async (_e, kind: "files" | "folder") => {
  const r = await dialog.showOpenDialog({ properties: kind === "folder" ? ["openDirectory"] : ["openFile", "multiSelections"] });
  return r.canceled ? [] : r.filePaths;
});
ipcMain.handle("copy", (_e, text: string) => { clipboard.writeText(text); return true; });
ipcMain.handle("open", (_e, target: string) => {
  // only well-known external destinations or local folders the user is looking at
  if (/^https:\/\/(studio\.youtube\.com|elevenlabs\.io)\//.test(target)) return shell.openExternal(target);
  if (fs.existsSync(target)) return shell.openPath(target);
  return false;
});

app.whenReady().then(createWindow);
app.on("window-all-closed", () => { engine?.kill(); app.quit(); });
