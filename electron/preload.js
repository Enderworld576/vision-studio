// Safe bridge to the main process: native file/folder pickers and the
// first-run setup controls. In plain web mode `window.vs` is undefined.
const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('vs', {
  browseFolder: () => ipcRenderer.invoke('vs:browseFolder'),
  browseFile: (filters) => ipcRenderer.invoke('vs:browseFile', filters),
  // first-run setup
  setupRun: () => ipcRenderer.invoke('setup:run'),
  setupContinue: () => ipcRenderer.invoke('setup:continue'),
  onSetupLog: (cb) => ipcRenderer.on('setup:log', (_e, m) => cb(m)),
});
