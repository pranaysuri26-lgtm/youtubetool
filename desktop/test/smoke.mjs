// Headless UI smoke test: launches the real Electron app (engine auto-started), drives the chat-first UI,
// and saves screenshots. Usage: SHIFTWS_HOME=... OUT=dir node test/smoke.mjs
import { _electron as electron } from "playwright-core";
import path from "path";
import fs from "fs";
const out = process.env.OUT || "screenshots";
fs.mkdirSync(out, { recursive: true });
const app = await electron.launch({
  executablePath: path.resolve("node_modules/electron/dist/electron"),
  args: [".", "--no-sandbox", "--disable-gpu"],
  env: { ...process.env },
});
const win = await app.firstWindow();
await win.setViewportSize?.({ width: 1440, height: 900 });
await win.waitForSelector(".brand", { timeout: 60000 });
console.log("brand ok"); await win.screenshot({ path: `${out}/01-channel.png` }); console.log("shot 1");
const proj = process.env.PROJECT_TITLE || "";
if (proj) {
  await win.click(`text=${proj}`);
  await win.waitForSelector(".resume", { timeout: 20000 });
  await win.waitForTimeout(1500);
  await win.screenshot({ path: `${out}/02-project-chat.png` });
  for (const tab of ["Script", "Media", "Plan", "Exports", "Memory", "Connections"]) {
    await win.click(`.tabs >> text=${tab}`);
    await win.waitForTimeout(1200);
    await win.screenshot({ path: `${out}/03-inspector-${tab.toLowerCase()}.png` });
  }
}
if (process.env.SEARCH) {
  await win.fill(".search input", process.env.SEARCH);
  await win.press(".search input", "Enter");
  await win.waitForSelector(".result", { timeout: 15000 });
  await win.screenshot({ path: `${out}/04-search.png` });
}
console.log("ok", fs.readdirSync(out));
await app.close();
