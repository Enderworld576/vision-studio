// First-run environment setup: locate the Python runtime, check whether the
// app is ready to run, and (if not) create the environment / install
// dependencies / fetch base weights — streaming progress to the setup window.
//
// Packaged builds ship a prebuilt, relocatable Python under resources/pyenv,
// so setup just verifies it and downloads base weights. In a dev checkout
// (no .venv) it creates the venv and installs requirements, like setup.sh.

const { spawn } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');

const WIN = process.platform === 'win32';

function pythonPath(app, root) {
  if (app && app.isPackaged) {
    const base = path.join(process.resourcesPath, 'pyenv');
    // python-build-standalone: python.exe is at the root on Windows, bin/ on unix.
    return WIN ? path.join(base, 'python.exe') : path.join(base, 'bin', 'python');
  }
  return WIN ? path.join(root, '.venv', 'Scripts', 'python.exe')
             : path.join(root, '.venv', 'bin', 'python');
}

function run(cmd, args, opts, onLog) {
  return new Promise((resolve, reject) => {
    const p = spawn(cmd, args, opts || {});
    const log = (b) => { if (onLog) onLog(b.toString()); };
    if (p.stdout) p.stdout.on('data', log);
    if (p.stderr) p.stderr.on('data', log);
    p.on('error', reject);
    p.on('exit', (code) => code === 0 ? resolve()
      : reject(new Error(`${path.basename(cmd)} exited with code ${code}`)));
  });
}

// Fast readiness check: python exists and the key packages are importable
// (find_spec avoids actually importing torch, so it's quick).
async function checkReady(py) {
  if (!fs.existsSync(py)) return { ready: false, reason: 'Python runtime not found' };
  try {
    await run(py, ['-c',
      "import importlib.util as u, sys;"
      + "sys.exit(0 if all(u.find_spec(m) for m in ['flask','ultralytics','cv2','numpy']) else 1)"],
      {}, null);
    return { ready: true };
  } catch {
    return { ready: false, reason: 'Python dependencies not installed' };
  }
}

async function runSetup(py, root, onLog) {
  const say = (m) => { if (onLog) onLog(m.endsWith('\n') ? m : m + '\n'); };

  // 1) Ensure a Python environment exists (dev path; packaged is prebuilt).
  if (!fs.existsSync(py)) {
    say('Creating Python environment…');
    const sys = process.env.PYTHON || 'python3';
    try {
      await run(sys, ['-m', 'venv', path.join(root, '.venv')], {}, onLog);
    } catch {
      say('ensurepip unavailable — creating venv without pip and bootstrapping…');
      await run(sys, ['-m', 'venv', '--without-pip', path.join(root, '.venv')], {}, onLog);
    }
    if (!fs.existsSync(py)) {
      throw new Error('Could not create the Python environment. Is Python 3.10+ installed?');
    }
  }

  // 2) Ensure pip.
  try {
    await run(py, ['-m', 'pip', '--version'], {}, null);
  } catch {
    say('Bootstrapping pip…');
    const gp = path.join(root, 'get-pip.py');
    await run(py, ['-c',
      `import urllib.request;urllib.request.urlretrieve('https://bootstrap.pypa.io/get-pip.py',r'${gp}')`],
      {}, onLog);
    await run(py, [gp], {}, onLog);
    try { fs.unlinkSync(gp); } catch {}
  }

  // 3) Install dependencies (only if missing).
  const ready = await checkReady(py);
  if (!ready.ready) {
    say('Installing dependencies (this downloads PyTorch — a few minutes)…');
    await run(py, ['-m', 'pip', 'install', '-r',
      path.join(root, 'backend', 'requirements.txt'), 'onnx'], { cwd: root }, onLog);
  } else {
    say('Dependencies already installed.');
  }

  // 4) Fetch base detector weights so the first training doesn't stall.
  say('Downloading base model weights…');
  await run(py, ['-c', "from ultralytics import YOLO; YOLO('yolo11n.pt')"], { cwd: root }, onLog);

  say('Setup complete.');
}

module.exports = { pythonPath, checkReady, runSetup };
