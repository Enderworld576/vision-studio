// Thin Electron shell: ensure the Python env is ready (first-run setup screen
// if not), start the backend, then show its UI. The backend serves the whole
// app (UI + API), so the window just loads its URL.
const { app, BrowserWindow, ipcMain, dialog } = require('electron');
const { spawn } = require('node:child_process');
const path = require('node:path');
const http = require('node:http');
const net = require('node:net');
const fs = require('node:fs');

const setup = require('./setup');

const ROOT = path.resolve(__dirname, '..');
const PY = setup.pythonPath(app, ROOT);
// Port is chosen at launch: honor VS_PORT if set, else pick a free one so we
// never collide with another program (or another copy of this app) on a fixed
// port.
let PORT = Number(process.env.VS_PORT) || 0;
let URL = '';
// Packaged resources are read-only, so the backend writes data to a per-user
// writable location; in dev it uses the project's data/ folder as before.
const DATA_DIR = app.isPackaged ? path.join(app.getPath('userData'), 'data')
                                : path.join(ROOT, 'data');
const MARKER = path.join(DATA_DIR, '.setup-done');   // written after first-run setup

let backend = null;
let win = null;
let setupWin = null;

function freePort() {
  return new Promise((resolve, reject) => {
    const srv = net.createServer();
    srv.on('error', reject);
    srv.listen(0, '127.0.0.1', () => {
      const { port } = srv.address();
      srv.close(() => resolve(port));
    });
  });
}

function startBackend() {
  // Run with a writable cwd: the backend reads its own files via __file__, but
  // libraries (e.g. depthai telemetry) write caches relative to cwd, which is
  // read-only inside a packaged app.
  try { fs.mkdirSync(DATA_DIR, { recursive: true }); } catch {}
  backend = spawn(PY, [path.join(ROOT, 'backend', 'server.py'),
                       '--port', String(PORT), '--data-dir', DATA_DIR],
    { cwd: DATA_DIR, stdio: ['ignore', 'pipe', 'pipe'] });
  backend.stdout.on('data', d => process.stdout.write(`[backend] ${d}`));
  backend.stderr.on('data', d => process.stdout.write(`[backend] ${d}`));
  backend.on('exit', c => console.log(`[backend] exited (${c})`));
}

function waitForBackend(cb, tries = 60) {
  http.get(URL + '/api/health', res => { res.resume(); cb(); })
    .on('error', () => {
      if (tries <= 0) return cb(new Error('backend did not start'));
      setTimeout(() => waitForBackend(cb, tries - 1), 500);
    });
}

function createWindow() {
  win = new BrowserWindow({
    width: 1280, height: 860, minWidth: 960, minHeight: 640,
    backgroundColor: '#0d0f14', title: 'Vision Studio',
    webPreferences: { contextIsolation: true, preload: path.join(__dirname, 'preload.js') },
  });
  win.removeMenu?.();
  win.loadURL(URL);
}

function createSetupWindow() {
  setupWin = new BrowserWindow({
    width: 720, height: 580, backgroundColor: '#0d0f14', title: 'Vision Studio — Setup',
    webPreferences: { contextIsolation: true, preload: path.join(__dirname, 'preload.js') },
  });
  setupWin.removeMenu?.();
  setupWin.loadFile(path.join(ROOT, 'renderer', 'setup.html'));
}

async function launchApp() {
  if (!PORT) PORT = await freePort();
  URL = `http://127.0.0.1:${PORT}`;
  startBackend();
  waitForBackend((err) => {
    if (err) console.error(err.message);
    createWindow();
    // Close the setup window only AFTER the main window exists, otherwise there
    // would be a moment with zero windows and the app would quit itself.
    if (setupWin) { const w = setupWin; setupWin = null; w.close(); }
  });
}

// --- IPC: native pickers --------------------------------------------------
ipcMain.handle('vs:browseFolder', async () => {
  const r = await dialog.showOpenDialog(win || setupWin, { properties: ['openDirectory'] });
  return r.canceled ? null : r.filePaths[0];
});
ipcMain.handle('vs:browseFile', async (_e, filters) => {
  const r = await dialog.showOpenDialog(win || setupWin, {
    properties: ['openFile'], filters: filters || [{ name: 'All files', extensions: ['*'] }],
  });
  return r.canceled ? null : r.filePaths[0];
});

// --- IPC: first-run setup -------------------------------------------------
ipcMain.handle('setup:run', async () => {
  try {
    // Weights download must land in a writable dir; use the trainer's work_dir
    // (DATA_DIR/work) so the pre-fetched base model is reused at train time.
    const weightsDir = path.join(DATA_DIR, 'work');
    await setup.runSetup(PY, ROOT, weightsDir, (m) => { if (setupWin) setupWin.webContents.send('setup:log', m); });
    try { fs.mkdirSync(DATA_DIR, { recursive: true }); fs.writeFileSync(MARKER, new Date().toISOString()); } catch {}
    return { ok: true };
  } catch (e) {
    return { ok: false, error: String((e && e.message) || e) };
  }
});
ipcMain.handle('setup:continue', () => {
  // launchApp() creates the main window and then closes the setup window.
  launchApp();
});

app.whenReady().then(async () => {
  const status = await setup.checkReady(PY);
  if (status.ready && fs.existsSync(MARKER)) launchApp();   // skip setup once done
  else createSetupWindow();
  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0 && backend) createWindow();
  });
});

function stopBackend() { if (backend) { backend.kill('SIGTERM'); backend = null; } }
app.on('window-all-closed', () => { stopBackend(); if (process.platform !== 'darwin') app.quit(); });
app.on('before-quit', stopBackend);
