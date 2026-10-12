import { contextBridge, ipcRenderer, webUtils } from "electron";

// The renderer only gets these narrow capabilities.
contextBridge.exposeInMainWorld("shiftws", {
  engine: () => ipcRenderer.invoke("engine"),
  pick: (kind: "files" | "folder") => ipcRenderer.invoke("pick", kind),
  copy: (text: string) => ipcRenderer.invoke("copy", text),
  open: (target: string) => ipcRenderer.invoke("open", target),
  pathForFile: (f: File) => webUtils.getPathForFile(f),
});
