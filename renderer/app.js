// Vision Studio front-end. Talks to the backend on the same origin.
const $ = (id) => document.getElementById(id);
async function api(method, path, body) {
  const opt = { method, headers: {} };
  if (body !== undefined) { opt.headers['Content-Type'] = 'application/json'; opt.body = JSON.stringify(body); }
  const r = await fetch(path, opt);
  return r.json();
}

// ---- navigation ----------------------------------------------------------
const TITLES = { home: 'Home', collect: '1 · Collect', train: '2 · Train', test: '3 · Test' };
function go(view) {
  document.querySelectorAll('.view').forEach(v => v.classList.add('hidden'));
  $('view-' + view).classList.remove('hidden');
  document.querySelectorAll('.nav button').forEach(b => b.classList.toggle('active', b.dataset.view === view));
  $('title').textContent = TITLES[view];
  if (view === 'home') refreshHome();
  if (view === 'collect') enterCollect();
  if (view === 'train') enterTrain();
  if (view === 'test') enterTest();
}
$('nav').addEventListener('click', e => { const b = e.target.closest('button'); if (b) go(b.dataset.view); });

// ---- global status -------------------------------------------------------
let lastCam = {};
async function poll() {
  try {
    const h = await api('GET', '/api/health');
    lastCam = h.camera || {};
    setDot('camDot', 'camText', lastCam.streaming, lastCam.streaming ? 'camera live' : (lastCam.error ? 'camera error' : 'camera off'));
    setDot('modelDot', 'modelText', h.model_ready, h.model_ready ? h.model : 'no model');
    const camName = lastCam.streaming ? (lastCam.label || lastCam.source || 'camera')
      : (lastCam.error ? 'camera error' : 'no camera');
    $('footStatus').textContent = `${camName} · YOLO11`;
  } catch { setDot('camDot', 'camText', false, 'backend down'); $('footStatus').textContent = 'backend down'; }
}
function setDot(dotId, textId, on, text) {
  const d = $(dotId); d.className = 'dot ' + (on ? 'on' : 'off'); $(textId).textContent = text;
}
setInterval(poll, 2500); poll();

// ---- HOME -----------------------------------------------------------------
async function refreshHome() {
  const [h, st] = await Promise.all([api('GET', '/api/health'), api('GET', '/api/dataset/stats')]);
  const cam = h.camera || {};
  $('cardCam').textContent = cam.streaming ? 'Live' : 'Off';
  $('cardCamSub').textContent = cam.error ? cam.error.slice(0, 40) : (cam.label || cam.source || '');
  $('cardImgs').textContent = st.images ?? 0;
  $('cardModel').textContent = h.model_ready ? 'Ready' : 'None';
  $('cardModelSub').textContent = h.model || 'train one first';
  loadCameras();
  loadExportModels();
}
async function loadExportModels() {
  const r = await api('GET', '/api/models');
  const sel = $('exportModelSelect');
  sel.innerHTML = (r.models && r.models.length)
    ? r.models.map(m => `<option ${m === r.active ? 'selected' : ''}>${m}</option>`).join('')
    : '<option value="">(no models yet — train one)</option>';
}
$('delModelBtn').onclick = () => {
  const m = $('exportModelSelect').value;
  if (!m) return;
  $('delModelMsg').textContent = `⚠ Permanently delete "${m}" and its orientation data?`;
  $('delModelConfirm').style.display = 'flex';
};
$('delModelNo').onclick = () => { $('delModelConfirm').style.display = 'none'; };
$('delModelYes').onclick = async () => {
  const m = $('exportModelSelect').value;
  $('delModelConfirm').style.display = 'none';
  const r = await api('DELETE', '/api/models/' + encodeURIComponent(m));
  setMsg('importMsg', r.ok ? `deleted ${m}` : (r.error || 'delete failed'), r.ok ? 'ok' : 'err');
  loadExportModels(); poll();
};
async function loadCameras() {
  const r = await api('GET', '/api/cameras');
  const sel = $('cameraSelect');
  sel.innerHTML = r.cameras.map((c, i) =>
    `<option value="${i}" data-kind="${c.kind}" data-source="${c.source}">${c.label}</option>`).join('');
  // pre-select the active camera if present
  const act = r.active || {};
  [...sel.options].forEach(o => { if (o.dataset.kind === act.kind && o.dataset.source == act.source) sel.value = o.value; });
}
$('scanBtn').onclick = async () => { $('homeMsg').textContent = 'scanning for cameras…'; await loadCameras(); $('homeMsg').textContent = 'scan complete'; };
$('useSelectedBtn').onclick = () => {
  const o = $('cameraSelect').selectedOptions[0];
  if (!o || !o.dataset.kind) { $('homeMsg').textContent = 'no camera selected — use “connect by address” below'; return; }
  connectCamera(o.dataset.kind, o.dataset.source);
};
$('connectBtn').onclick = () => connectCustom();
function connectCustom() {
  const src = $('sourceInput').value.trim();
  if (!src) { $('homeMsg').textContent = 'enter an address (IP, URL, or USB index)'; return; }
  connectCamera($('kindSelect').value, src);
}
async function connectCamera(kind, source) {
  $('homeMsg').textContent = 'connecting…';
  const r = await api('POST', '/api/camera/select', { kind, source });
  $('homeMsg').textContent = r.streaming ? `connected ✓ (${r.label})` : ('camera: ' + (r.error || 'starting…'));
  setTimeout(refreshHome, 1800);
}

// ---- import existing work ----
$('importDsBtn').onclick = async () => {
  const path = $('importDsPath').value.trim();
  if (!path) { setMsg('importMsg', 'enter the folder path of your dataset', 'err'); return; }
  setMsg('importMsg', 'importing images…');
  const r = await api('POST', '/api/import/dataset', { path });
  if (r.error) setMsg('importMsg', r.error, 'err');
  else { setMsg('importMsg', `imported ${r.imported} images (skipped ${r.skipped} already present). Dataset now has ${r.stats.images}.`, 'ok'); refreshHome(); loadClasses(); }
};
$('importModelBtn').onclick = async () => {
  const path = $('importModelPath').value.trim();
  if (!path) { setMsg('importMsg', 'enter the path to a .pt model file', 'err'); return; }
  setMsg('importMsg', 'importing model…');
  const r = await api('POST', '/api/import/model', { path });
  if (r.error) setMsg('importMsg', r.error, 'err');
  else { setMsg('importMsg', `model imported: ${r.model} (part "${r.target_class}") — now active.`, 'ok'); refreshHome(); }
};
$('exportBtn').onclick = async () => {
  const path = $('exportPath').value.trim();
  if (!path) { setMsg('importMsg', 'choose a destination folder to export to', 'err'); return; }
  setMsg('importMsg', 'exporting…');
  const r = await api('POST', '/api/export/dataset', { path });
  if (r.error) setMsg('importMsg', r.error, 'err');
  else setMsg('importMsg', `exported ${r.images} images to ${r.path}`, 'ok');
};
$('exportModelBtn').onclick = async () => {
  const path = $('exportModelPath').value.trim();
  if (!path) { setMsg('importMsg', 'choose a destination folder for the model', 'err'); return; }
  setMsg('importMsg', 'exporting model (building ONNX…)');
  const r = await api('POST', '/api/export/model', { path, model: $('exportModelSelect').value });
  if (r.error) setMsg('importMsg', r.error, 'err');
  else setMsg('importMsg', `model exported to ${r.path}  (ONNX: ${r.onnx ? 'yes' : 'no'}, orientation: ${r.orientation ? 'yes' : 'no'})`, 'ok');
};

// Native Browse… buttons — only in the desktop app (window.vs from preload).
(function initBrowse() {
  const has = !!window.vs;
  document.querySelectorAll('.browse').forEach(btn => {
    if (!has) { btn.style.display = 'none'; return; }
    btn.onclick = async () => {
      const target = $(btn.dataset.target);
      const p = btn.dataset.mode === 'file'
        ? await window.vs.browseFile([{ name: 'Model', extensions: ['pt'] }])
        : await window.vs.browseFolder();
      if (p) target.value = p;
    };
  });
})();

// ---- COLLECT --------------------------------------------------------------
const canvas = $('collectCanvas'), ctx = canvas.getContext('2d');
// Labeling state. step: idle -> box -> front -> back -> done
let frozen = null, step = 'idle', box = null, front = null, back = null, drag = null, editStem = null;
function enterCollect() {
  loadClasses(); refreshGallery();
  if (lastCam.streaming) { $('collectFeed').src = '/api/camera/stream?' + Date.now(); $('collectHint').style.display = 'none'; }
  else $('collectHint').style.display = 'grid';
}
async function loadClasses() {
  const cls = await api('GET', '/api/classes');
  const opts = cls.map(c => `<option>${c}</option>`).join('') || '<option value="">(none yet)</option>';
  const cur1 = $('classSelect').value;
  $('classSelect').innerHTML = opts;
  if (cls.includes(cur1)) $('classSelect').value = cur1;
  // Train dropdown also offers a multi-class "All parts" option.
  const cur2 = $('trainClass').value;
  const allOpt = cls.length > 1 ? '<option value="__all__">★ All parts (one multi-class model)</option>' : '';
  $('trainClass').innerHTML = allOpt + opts;
  if ([...$('trainClass').options].some(o => o.value === cur2)) $('trainClass').value = cur2;
}
// Inline add-class (window.prompt() isn't supported in Electron).
$('addClassBtn').onclick = () => { $('newClassRow').style.display = 'flex'; $('newClassInput').focus(); };
$('newClassCancel').onclick = () => { $('newClassRow').style.display = 'none'; $('newClassInput').value = ''; };
// Delete a class entirely (with inline confirm).
$('delClassBtn').onclick = () => {
  const c = $('classSelect').value;
  if (!c) return;
  $('delClassMsg').textContent = `⚠ Delete class "${c}" and ALL its images/labels? This cannot be undone.`;
  $('delClassRow').style.display = 'flex';
};
$('delClassNo').onclick = () => { $('delClassRow').style.display = 'none'; };
$('delClassYes').onclick = async () => {
  const c = $('classSelect').value;
  $('delClassRow').style.display = 'none';
  await api('DELETE', '/api/classes/' + encodeURIComponent(c));
  setMsg('collectMsg', `deleted class "${c}"`, 'ok');
  galFilter = 'all';
  await loadClasses(); refreshGallery();
};
$('newClassConfirm').onclick = addClass;
$('newClassInput').addEventListener('keydown', e => { if (e.key === 'Enter') addClass(); if (e.key === 'Escape') $('newClassCancel').onclick(); });
async function addClass() {
  const name = $('newClassInput').value.trim();
  if (!name) return;
  await api('POST', '/api/classes', { name });
  await loadClasses(); $('classSelect').value = name;
  $('newClassRow').style.display = 'none'; $('newClassInput').value = '';
  setMsg('collectMsg', `added part "${name}"`, 'ok');
}
function blobToImage(blob) { return new Promise(res => { const i = new Image(); i.onload = () => res(i); i.src = URL.createObjectURL(blob); }); }
function sizeCanvas() { const vp = $('collectVp').getBoundingClientRect(); canvas.width = vp.width; canvas.height = vp.height; }
function backToLive() {
  frozen = null; box = front = back = null; step = 'idle'; editStem = null;
  $('collectFeed').style.display = ''; $('collectFeed').src = lastCam.streaming ? '/api/camera/stream?' + Date.now() : '';
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  $('saveBtn').disabled = true; $('clearBoxBtn').disabled = true;
}
$('captureBtn').onclick = async () => {
  const r = await fetch('/api/camera/snapshot'); if (!r.ok) { setMsg('collectMsg', 'camera not streaming', 'err'); return; }
  frozen = await blobToImage(await r.blob());
  box = front = back = null; editStem = null; step = 'box';
  $('collectFeed').style.display = 'none'; sizeCanvas(); redraw();
  $('clearBoxBtn').disabled = false; updateSave();
  setMsg('collectMsg', 'Drag a box around the part.');
};
$('clearBoxBtn').onclick = () => { box = front = back = null; step = 'box'; redraw(); updateSave(); setMsg('collectMsg', 'Drag a box around the part.'); };

function redraw() {
  if (!frozen) return;
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  ctx.drawImage(frozen, 0, 0, canvas.width, canvas.height);
  if (box) {
    ctx.strokeStyle = '#4c8dff'; ctx.lineWidth = 2; ctx.strokeRect(box.x, box.y, box.w, box.h);
    ctx.fillStyle = 'rgba(76,141,255,.12)'; ctx.fillRect(box.x, box.y, box.w, box.h);
  }
  if (back && front) { ctx.strokeStyle = '#ff5c5c'; ctx.lineWidth = 3; ctx.beginPath(); ctx.moveTo(back.x, back.y); ctx.lineTo(front.x, front.y); ctx.stroke(); }
  drawPt(back, '#ff5c5c', 'B'); drawPt(front, '#3fbf7f', 'F');
}
function drawPt(p, color, label) {
  if (!p) return;
  ctx.fillStyle = color; ctx.beginPath(); ctx.arc(p.x, p.y, 6, 0, 7); ctx.fill();
  ctx.fillStyle = '#fff'; ctx.font = 'bold 13px system-ui'; ctx.fillText(label, p.x + 9, p.y - 9);
}
canvas.addEventListener('mousedown', e => {
  if (!frozen) return; const p = pos(e);
  if (step === 'box') { drag = p; }
  else if (step === 'front') { front = p; step = 'back'; redraw(); updateSave(); setMsg('collectMsg', 'Now click the BACK of the part.'); }
  else if (step === 'back') { back = p; step = 'done'; redraw(); updateSave(); setMsg('collectMsg', 'Looks good. Save, or click to re-place FRONT.'); }
  else if (step === 'done') { front = p; back = null; step = 'back'; redraw(); updateSave(); setMsg('collectMsg', 'Now click the BACK of the part.'); }
});
canvas.addEventListener('mousemove', e => {
  if (!drag || step !== 'box') return; const p = pos(e);
  box = { x: Math.min(drag.x, p.x), y: Math.min(drag.y, p.y), w: Math.abs(p.x - drag.x), h: Math.abs(p.y - drag.y) }; redraw();
});
window.addEventListener('mouseup', () => {
  if (drag && step === 'box' && box && box.w > 6 && box.h > 6) { step = 'front'; setMsg('collectMsg', 'Now click the FRONT of the part.'); updateSave(); }
  drag = null;
});
function pos(e) { const r = canvas.getBoundingClientRect(); return { x: e.clientX - r.left, y: e.clientY - r.top }; }
function updateSave() { $('saveBtn').disabled = !(frozen && box && box.w > 6 && box.h > 6); }

async function editSample(stem) {
  const s = galAll.find(x => x.stem === stem); if (!s) return;
  go('collect');
  frozen = await blobToImage(await (await fetch('/api/samples/' + stem + '/image')).blob());
  editStem = stem; $('collectFeed').style.display = 'none'; sizeCanvas();
  const b = s.boxes[0];
  box = { x: (b.cx - b.w / 2) * canvas.width, y: (b.cy - b.h / 2) * canvas.height, w: b.w * canvas.width, h: b.h * canvas.height };
  if (b.keypoints) {
    front = { x: b.keypoints[0][0] * canvas.width, y: b.keypoints[0][1] * canvas.height };
    back = { x: b.keypoints[1][0] * canvas.width, y: b.keypoints[1][1] * canvas.height };
    step = 'done';
  } else { front = back = null; step = 'front'; }
  if ([...$('classSelect').options].some(o => o.value === b.className)) $('classSelect').value = b.className;
  redraw(); $('clearBoxBtn').disabled = false; updateSave();
  setMsg('collectMsg', b.keypoints ? 'Editing — adjust, then Save.' : 'Editing — click FRONT then BACK to add direction, then Save.');
}

$('saveBtn').onclick = async () => {
  const cls = $('classSelect').value; if (!cls) { setMsg('collectMsg', 'add a part/class first', 'err'); return; }
  if (!box || !frozen) return;
  const norm = { className: cls,
    cx: (box.x + box.w / 2) / canvas.width, cy: (box.y + box.h / 2) / canvas.height,
    w: box.w / canvas.width, h: box.h / canvas.height };
  if (front && back) norm.keypoints = [
    [front.x / canvas.width, front.y / canvas.height],
    [back.x / canvas.width, back.y / canvas.height]];
  const body = { boxes: [norm] };
  if (editStem) body.stem = editStem;
  else {
    const c = document.createElement('canvas'); c.width = frozen.naturalWidth || frozen.width; c.height = frozen.naturalHeight || frozen.height;
    c.getContext('2d').drawImage(frozen, 0, 0);
    body.image_b64 = c.toDataURL('image/jpeg', 0.92).split(',')[1];
  }
  const r = await api('POST', '/api/samples', body);
  if (r.error) { setMsg('collectMsg', r.error, 'err'); return; }
  setMsg('collectMsg', `${editStem ? 'updated' : 'saved'} ✓ — ${r.stats.images} images${front && back ? ' · front/back set' : ' (no direction yet)'}`, 'ok');
  backToLive(); refreshGallery();
};
let galAll = [], galFilter = 'all', galPage = 0;
const GAL_PAGE = 24;
async function refreshGallery() {
  galAll = await api('GET', '/api/samples');
  $('dsCount').textContent = `(${galAll.length})`;
  renderFilters();
  renderGallery();
}
function galCounts() {
  const c = {};
  galAll.forEach(s => s.boxes.forEach(b => { c[b.className] = (c[b.className] || 0) + 1; }));
  return c;
}
function renderFilters() {
  const counts = galCounts();
  const labels = Object.keys(counts).sort();
  if (galFilter !== 'all' && !labels.includes(galFilter)) galFilter = 'all';
  const chip = (f, txt) => `<span class="chip ${galFilter === f ? 'active' : ''}" data-f="${f}">${txt}</span>`;
  $('filterTabs').innerHTML = chip('all', `All (${galAll.length})`) + labels.map(l => chip(l, `${l} (${counts[l]})`)).join('');
  $('filterTabs').querySelectorAll('[data-f]').forEach(c => c.onclick = () => {
    galFilter = c.dataset.f; galPage = 0; renderFilters(); renderGallery();
  });
}
function galFiltered() {
  return galFilter === 'all' ? galAll : galAll.filter(s => s.boxes.some(b => b.className === galFilter));
}
function renderGallery() {
  const items = galFiltered();
  const pages = Math.max(1, Math.ceil(items.length / GAL_PAGE));
  galPage = Math.min(Math.max(0, galPage), pages - 1);
  const slice = items.slice(galPage * GAL_PAGE, galPage * GAL_PAGE + GAL_PAGE);
  $('gallery').innerHTML = slice.map(x => {
    const b = x.boxes[0] || {};
    const dir = b.keypoints ? '<span title="has front/back">↳</span> ' : '';
    return `<div class="thumb"><img src="/api/samples/${x.stem}/image" loading="lazy" data-edit="${x.stem}" title="click to edit" style="cursor:pointer">
      <button data-del="${x.stem}" title="delete">×</button>
      <div class="tag">${dir}${b.className || ''}</div></div>`;
  }).join('') || '<div class="muted" style="grid-column:1/-1">no images for this filter</div>';
  $('gallery').querySelectorAll('[data-edit]').forEach(im => im.onclick = () => editSample(im.dataset.edit));
  $('gallery').querySelectorAll('[data-del]').forEach(b => b.onclick = async (e) => {
    e.stopPropagation();
    await api('DELETE', '/api/samples/' + b.dataset.del); refreshGallery();
  });
  $('pageInfo').textContent = `page ${galPage + 1} of ${pages} · ${items.length} image${items.length === 1 ? '' : 's'}`;
  $('prevPage').disabled = galPage <= 0;
  $('nextPage').disabled = galPage >= pages - 1;
}
$('prevPage').onclick = () => { if (galPage > 0) { galPage--; renderGallery(); } };
$('nextPage').onclick = () => { galPage++; renderGallery(); };
$('clearDsBtn').onclick = () => {
  if (galFilter === '__neg__') { setMsg('collectMsg', 'pick "All" or a part chip to clear', 'err'); return; }
  const count = galFiltered().length;
  const what = galFilter === 'all'
    ? `ALL ${count} images`
    : `all ${count} image${count === 1 ? '' : 's'} of "${galFilter}"`;
  $('clearDsMsg').textContent = `⚠ Delete ${what}? This cannot be undone.`;
  $('clearDsConfirm').style.display = 'flex';
};
$('clearDsNo').onclick = () => { $('clearDsConfirm').style.display = 'none'; };
$('clearDsYes').onclick = async () => {
  const target = galFilter;
  $('clearDsConfirm').style.display = 'none';
  await api('POST', '/api/dataset/clear', { class_name: target === 'all' ? 'all' : target });
  setMsg('collectMsg', target === 'all' ? 'dataset cleared' : `deleted "${target}" images`, 'ok');
  galFilter = 'all';
  refreshGallery(); loadClasses();
};

// ---- TRAIN ----------------------------------------------------------------
let trainTimer = null;
async function enterTrain() { await loadClasses(); pollTrain(); updateTrainUI(); loadHealth(); loadGpu(); }
async function loadHealth() {
  const h = await api('GET', '/api/dataset/health');
  if (h.warnings && h.warnings.length) {
    $('healthPanel').innerHTML = '⚠ ' + h.warnings.join('<br>⚠ ');
    $('healthPanel').style.color = 'var(--warn)';
  } else {
    $('healthPanel').innerHTML = '✓ Dataset looks healthy.';
    $('healthPanel').style.color = 'var(--good)';
  }
}
async function loadGpu() {
  const g = await api('GET', '/api/gpu');
  if (g.available) { $('gpuLine').innerHTML = `⚡ GPU detected — training will use <b>${g.name}</b>.`; return; }
  $('gpuLine').innerHTML = 'Training on <b>CPU</b>. <a href="#" id="gpuEnable">Enable NVIDIA GPU</a> (only if you have one).';
  const a = $('gpuEnable');
  if (a) a.onclick = (e) => {
    e.preventDefault();
    $('gpuLine').innerHTML = '⚠ This downloads the ~2.5 GB CUDA build of PyTorch. '
      + '<button class="btn" id="gpuYes" style="padding:3px 10px">Install</button> '
      + '<button class="btn ghost" id="gpuNo" style="padding:3px 10px">Cancel</button>';
    $('gpuNo').onclick = loadGpu;
    $('gpuYes').onclick = async () => {
      $('gpuLine').textContent = 'Installing CUDA build (~2.5 GB)… keep working; restart the app when it finishes.';
      await api('POST', '/api/gpu/install'); pollGpuInstall();
    };
  };
}
async function pollGpuInstall() {
  const s = await api('GET', '/api/gpu/install/status');
  if (s.state === 'installing') { setTimeout(pollGpuInstall, 4000); return; }
  $('gpuLine').innerHTML = s.state === 'done'
    ? '✓ CUDA build installed — <b>restart the app</b> to use the GPU.'
    : '✗ CUDA install failed (see console). Still using CPU.';
}
function trainCls() { return $('trainClass').value; }
function updateTrainUI() {
  const multi = $('trainClass').value === '__all__';
  $('dsNegRow').style.display = multi ? 'none' : 'flex';   // "other parts as negatives" is moot in multi-class
  $('multiNote').style.display = multi ? 'block' : 'none';
  loadNegatives();
}
$('trainClass').onchange = updateTrainUI;
$('negCapture').onclick = async () => {
  const cls = trainCls(); if (!cls) { setMsg('negMsg', 'pick a part first', 'err'); return; }
  setMsg('negMsg', 'capturing…');
  const r = await api('POST', '/api/negatives', { className: cls });
  if (r.error) setMsg('negMsg', r.error, 'err');
  else { setMsg('negMsg', 'added a negative ✓', 'ok'); loadNegatives(); }
};
$('negUpload').onclick = () => $('negFile').click();
$('negFile').onchange = async () => {
  const cls = trainCls(); if (!cls) { setMsg('negMsg', 'pick a part first', 'err'); return; }
  const files = [...$('negFile').files]; if (!files.length) return;
  setMsg('negMsg', `uploading ${files.length}…`);
  for (const f of files) {
    const b64 = await new Promise(res => { const rd = new FileReader(); rd.onload = () => res(rd.result.split(',')[1]); rd.readAsDataURL(f); });
    await api('POST', '/api/negatives', { className: cls, image_b64: b64 });
  }
  $('negFile').value = '';
  setMsg('negMsg', `added ${files.length} negative(s) ✓`, 'ok'); loadNegatives();
};
async function loadNegatives() {
  const cls = trainCls();
  const items = (await api('GET', '/api/negatives')).filter(n => n.className === cls);
  $('negCount').textContent = cls ? `· ${items.length} for "${cls}"` : '';
  $('negGallery').innerHTML = items.slice(0, 60).map(n =>
    `<div class="thumb"><img src="/api/negatives/${encodeURIComponent(n.className)}/${n.stem}/image" loading="lazy">
      <button data-delneg="${n.className}::${n.stem}" title="delete">×</button>
      <div class="tag">✕ negative</div></div>`).join('')
    || '<div class="muted" style="grid-column:1/-1">no negatives yet</div>';
  $('negGallery').querySelectorAll('[data-delneg]').forEach(b => b.onclick = async () => {
    const [c, s] = b.dataset.delneg.split('::');
    await api('DELETE', `/api/negatives/${encodeURIComponent(c)}/${s}`); loadNegatives();
  });
}
$('trainBtn').onclick = async () => {
  const v = $('trainClass').value, multi = v === '__all__';
  const r = await api('POST', '/api/train', { class_name: multi ? '' : v, epochs: +$('epochs').value, dataset_negatives: $('dsNeg').checked, multiclass: multi, model_size: $('modelSize').value });
  if (r.error) { $('trainState').textContent = r.error; return; }
  startTrainPolling();
};
function startTrainPolling() { if (trainTimer) clearInterval(trainTimer); trainTimer = setInterval(pollTrain, 1200); }
async function pollTrain() {
  const st = await api('GET', '/api/train/status');
  $('trainState').textContent = st.state
    + (st.state === 'training' ? ` · epoch ${st.epoch}/${st.total} · ${(st.device || 'cpu').toUpperCase()}` : '');
  $('trainMap').textContent = st.map50 != null
    ? `mAP50 ${st.map50}` + (st.map5095 != null ? ` · mAP50-95 ${st.map5095}` : '') : '';
  $('trainBar').style.width = (st.total ? Math.min(100, 100 * st.epoch / st.total) : 0) + '%';
  $('trainLog').textContent = (st.log || []).join('\n'); $('trainLog').scrollTop = 1e9;
  $('trainBtn').disabled = (st.state === 'training' || st.state === 'preparing');
  if (st.state === 'done') {
    $('trainResult').style.display = 'block';
    $('trainMetrics').innerHTML = [
      `<b>mAP50</b> ${st.map50 ?? '—'}`, `<b>mAP50-95</b> ${st.map5095 ?? '—'}`,
      `<b>held-out val</b> ${st.n_val} imgs`, `<b>device</b> ${(st.device || 'cpu').toUpperCase()}`,
    ].map(x => `<span class="muted">${x}</span>`).join('');
    if (st.has_preview && !$('valPreviews').dataset.loaded) loadPreviews();
  } else {
    $('trainResult').style.display = 'none'; $('valPreviews').dataset.loaded = '';
  }
  if (trainTimer && (st.state === 'done' || st.state === 'error' || st.state === 'idle')) { clearInterval(trainTimer); trainTimer = null; }
}
async function loadPreviews() {
  const r = await api('GET', '/api/train/preview');
  $('valPreviews').innerHTML = (r.images || []).map(b =>
    `<img class="shot" src="data:image/jpeg;base64,${b}" style="margin-bottom:8px">`).join('')
    || '<div class="muted">no preview available</div>';
  $('valPreviews').dataset.loaded = '1';
}

// ---- TEST -----------------------------------------------------------------
function enterTest() {
  if (lastCam.streaming) { $('testFeed').src = '/api/camera/stream?' + Date.now(); $('testHint').style.display = 'none'; }
  else $('testHint').style.display = 'grid';
  loadModels();
}
async function loadModels() {
  const r = await api('GET', '/api/models');
  const sel = $('modelSelect');
  sel.innerHTML = r.models && r.models.length
    ? r.models.map(m => `<option ${m === r.active ? 'selected' : ''}>${m}</option>`).join('')
    : '<option value="">(no models — train or import one)</option>';
}
$('modelSelect').onchange = async () => {
  const name = $('modelSelect').value;
  if (!name) return;
  setMsg('testMsg', 'switching model…');
  const r = await api('POST', '/api/models/select', { name });
  setMsg('testMsg', r.ok ? `now testing: ${r.model}  (part "${r.target_class}")` : (r.error || 'switch failed'), r.ok ? 'ok' : 'err');
  poll();
};
$('detectBtn').onclick = () => runDetect({ source: 'camera' });
$('uploadBtn').onclick = () => $('fileInput').click();
$('fileInput').onchange = () => { const f = $('fileInput').files[0]; if (!f) return;
  const rd = new FileReader(); rd.onload = () => runDetect({ source: 'image', data: rd.result.split(',')[1] }); rd.readAsDataURL(f); };
let lastDetect = null;   // last detection source, to re-run when sliders change
$('confSlider').oninput = () => $('confVal').textContent = (+$('confSlider').value).toFixed(2);
$('iouSlider').oninput = () => $('iouVal').textContent = (+$('iouSlider').value).toFixed(2);
$('confSlider').onchange = $('iouSlider').onchange = () => { if (lastDetect) runDetect(lastDetect); };
$('calibrateBtn').onclick = async () => {
  setMsg('testMsg', 'calibrating…'); const r = await api('POST', '/api/calibrate', { source: 'camera' });
  setMsg('testMsg', r.ok ? `default set: 0° = ${r.zero.toFixed(1)}°` : ('calibrate failed: ' + r.error), r.ok ? 'ok' : 'err');
};
async function runDetect(body) {
  lastDetect = body;
  setMsg('testMsg', 'detecting…'); $('feedbackRow').style.display = 'none'; hideObjectViews(); exitDraw(); $('detList').innerHTML = '';
  const r = await api('POST', '/api/detect', { ...body, conf: +$('confSlider').value, iou: +$('iouSlider').value });
  if (r.error) { setMsg('testMsg', r.error, 'err'); }
  else if (!r.found) { setMsg('testMsg', 'No part found in the frame.', 'err'); $('resultImg').style.display = 'none'; $('resultInfo').innerHTML = ''; return; }
  else setMsg('testMsg', `${r.count} instance${r.count === 1 ? '' : 's'} found` + (r.classes.length > 1 ? ` · ${r.classes.length} classes` : ''), 'ok');
  if (!r.found) return;
  $('resultImg').src = 'data:image/jpeg;base64,' + r.image_b64; $('resultImg').style.display = 'block';
  const ang = r.directed ? `${r.angle_rel.toFixed(1)}°` : `${r.angle_rel.toFixed(1)}° (direction unknown)`;
  const src = r.method === 'template' ? 'shape axis + appearance template'
    : r.method === 'hybrid' ? 'shape axis + learned front/back'
    : r.method === 'keypoints' ? 'learned front/back'
    : 'estimated (image processing)';
  $('resultInfo').innerHTML = [
    ['instances found', r.count + (r.count > 1 ? '  (details = top match)' : '')],
    ['part / class', r.class || '—'],
    ['orientation', `<span class="big">${ang}</span>`],
    ['orientation from', src],
    ['center pixel', `x = ${r.center.x}, y = ${r.center.y}`],
    ['bounding box', r.bbox.map(v => v | 0).join(', ')],
    ['confidence', (r.conf * 100).toFixed(1) + '%'],
    ['default (0°)', r.zero.toFixed(1) + '°'],
  ].map(([k, v]) => `<tr><td>${k}</td><td>${v}</td></tr>`).join('');
  $('detList').innerHTML = (r.detections && r.detections.length > 1)
    ? '<b>All detections:</b><br>' + r.detections.map((d, i) =>
        `#${i + 1} ${d.class} · ${(d.conf * 100).toFixed(0)}%` + (d.directed ? ` · ${d.angle_rel.toFixed(0)}°` : '')).join('<br>')
    : '';
  $('feedbackRow').style.display = (r.angle_raw != null) ? 'block' : 'none';
  setMsg('fbMsg', '');
  if (r.crop_b64) {
    $('zoomImg').src = 'data:image/jpeg;base64,' + r.crop_b64; $('zoomImg').style.display = 'block'; $('zoomHint').style.display = 'none';
    $('grayImg').src = 'data:image/jpeg;base64,' + r.gray_b64; $('grayImg').style.display = 'block'; $('grayHint').style.display = 'none';
    $('histCanvas').style.display = 'block'; drawHist(r.hist || []);
  }
}
function hideObjectViews() {
  for (const id of ['zoomImg', 'grayImg', 'histCanvas']) $(id).style.display = 'none';
  $('zoomHint').style.display = ''; $('grayHint').style.display = '';
}
function drawHist(hist) {
  const c = $('histCanvas'), x = c.getContext('2d');
  x.clearRect(0, 0, c.width, c.height);
  const max = Math.max(1, ...hist), bw = c.width / hist.length;
  x.fillStyle = '#4c8dff';
  hist.forEach((v, i) => { const h = (v / max) * (c.height - 4); x.fillRect(i * bw, c.height - h, Math.max(1, bw), h); });
}
$('fbYes').onclick = () => sendCorrection({ flip: false }, 'saved ✓');
$('fbNo').onclick = () => sendCorrection({ flip: true }, 'saved ✓ (flipped)');
async function sendCorrection(body, label) {
  setMsg('fbMsg', 'saving…');
  const r = await api('POST', '/api/correct', body);
  if (r.error) { setMsg('fbMsg', r.error, 'err'); return; }
  setMsg('fbMsg', `${label} — dataset now ${r.stats.images} images. Retrain to apply.`, 'ok');
}

// --- draw the correct facing (precise angle) ---
const fixCanvas = $('fixCanvas'), fixCtx = fixCanvas.getContext('2d');
let fixDrawing = false, fixStart = null, fixAngle = null;
function fixPos(e) { const r = fixCanvas.getBoundingClientRect(); return { x: e.clientX - r.left, y: e.clientY - r.top }; }
function fixArrow(a, b) {
  fixCtx.clearRect(0, 0, fixCanvas.width, fixCanvas.height);
  fixCtx.strokeStyle = '#4c8dff'; fixCtx.fillStyle = '#4c8dff'; fixCtx.lineWidth = 3;
  fixCtx.beginPath(); fixCtx.moveTo(a.x, a.y); fixCtx.lineTo(b.x, b.y); fixCtx.stroke();
  const ang = Math.atan2(b.y - a.y, b.x - a.x), h = 13;
  fixCtx.beginPath(); fixCtx.moveTo(b.x, b.y);
  fixCtx.lineTo(b.x - h * Math.cos(ang - 0.4), b.y - h * Math.sin(ang - 0.4));
  fixCtx.lineTo(b.x - h * Math.cos(ang + 0.4), b.y - h * Math.sin(ang + 0.4));
  fixCtx.closePath(); fixCtx.fill();
}
function exitDraw() { fixCanvas.style.display = 'none'; $('fbDrawRow').style.display = 'none'; fixDrawing = false; fixStart = null; }
$('fbDraw').onclick = () => {
  const img = $('resultImg');
  fixCanvas.width = img.clientWidth; fixCanvas.height = img.clientHeight;
  fixCtx.clearRect(0, 0, fixCanvas.width, fixCanvas.height);
  fixCanvas.style.display = 'block'; $('fbDrawRow').style.display = 'flex';
  $('fbDrawSave').disabled = true; fixAngle = null;
  setMsg('fbMsg', 'Drag on the image in the direction the part should face (back → front).');
};
$('fbDrawCancel').onclick = () => { exitDraw(); setMsg('fbMsg', ''); };
fixCanvas.addEventListener('mousedown', e => { fixDrawing = true; fixStart = fixPos(e); });
fixCanvas.addEventListener('mousemove', e => { if (fixDrawing) fixArrow(fixStart, fixPos(e)); });
window.addEventListener('mouseup', e => {
  if (!fixDrawing) return;
  fixDrawing = false; const p = fixPos(e);
  const dx = p.x - fixStart.x, dy = p.y - fixStart.y;
  if (Math.hypot(dx, dy) > 8) {           // uniform scale -> display angle == image angle
    fixAngle = (Math.atan2(dy, dx) * 180 / Math.PI + 360) % 360;
    $('fbDrawSave').disabled = false; fixArrow(fixStart, p);
    setMsg('fbMsg', `facing ${fixAngle.toFixed(0)}° — Save drawn facing to add it.`);
  }
});
$('fbDrawSave').onclick = async () => {
  if (fixAngle == null) return;
  await sendCorrection({ angle_deg: fixAngle }, 'saved drawn facing ✓');
  exitDraw();
};

function setMsg(id, text, kind) { const e = $(id); e.textContent = text; e.className = 'status-msg' + (kind ? ' ' + kind : ''); }
