import * as THREE from 'three';
import {OrbitControls} from 'three/addons/controls/OrbitControls.js';
import {CSS2DRenderer, CSS2DObject} from 'three/addons/renderers/CSS2DRenderer.js';

/* ============================== utilities ============================== */
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
const api = (u, o) => fetch(u, o).then(r => { if (!r.ok) throw new Error(`${r.status} ${u}`); return r.json(); });
const enc = encodeURIComponent;
const DAY = 864e5;
const T = iso => (iso ? Date.parse(iso) : NaN);
const debounce = (f, ms) => { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => f(...a), ms); }; };
const store = {get(k, d) { try { const v = localStorage.getItem('pensieve.' + k); return v == null ? d : JSON.parse(v); } catch { return d; } },
               set(k, v) { try { localStorage.setItem('pensieve.' + k, JSON.stringify(v)); } catch {} }};
const person = a => (a || '').replace(/\s*<.*>$/, '') || 'unknown';
const plural = (n, w) => `${n.toLocaleString()} ${w}${n === 1 ? '' : 's'}`;
const fmtD = t => new Date(t).toLocaleDateString(undefined, {month: 'short', day: 'numeric'});
const fmtDY = t => new Date(t).toLocaleDateString(undefined, {month: 'short', day: 'numeric', year: 'numeric'});
function rel(t) {
  if (!t || isNaN(t)) return '';
  const s = (Date.now() - t) / 1000;
  if (s < 90) return 'just now';
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  if (s < 86400) return `${Math.round(s / 3600)}h ago`;
  if (s < 86400 * 14) return `${Math.round(s / 86400)}d ago`;
  return new Date(t).getFullYear() === new Date().getFullYear() ? fmtD(t) : fmtDY(t);
}
function toast(msg) {
  const d = document.createElement('div'); d.className = 'toast'; d.textContent = msg;
  $('#toasts').append(d); setTimeout(() => d.remove(), 3500);
}
const tipEl = $('#tip');
function tip(html, x, y) {
  if (!html) { tipEl.style.display = 'none'; return; }
  tipEl.innerHTML = html; tipEl.style.display = 'block';
  const r = tipEl.getBoundingClientRect();
  tipEl.style.left = Math.min(x + 14, innerWidth - r.width - 8) + 'px';
  tipEl.style.top = (y + 16 + r.height > innerHeight ? y - r.height - 12 : y + 16) + 'px';
}

/* ============================== palette ============================== */
// Validated 8-slot categorical palette (light / dark steps), fixed order; overflow folds into "Other".
const PAL = {
  light: {cat: ['#2a78d6', '#eb6834', '#1baf7a', '#eda100', '#e87ba4', '#008300', '#4a3aa7', '#e34948'], other: '#a3a29d',
          seq: ['#86b6ef', '#6da7ec', '#5598e7', '#3987e5', '#2a78d6', '#256abf', '#1c5cab', '#184f95', '#104281', '#0d366b'],
          ring: '#fcfcfb', heat0: '#e9e8e3'},
  dark: {cat: ['#3987e5', '#d95926', '#199e70', '#c98500', '#d55181', '#008300', '#9085e9', '#e66767'], other: '#5d5c57',
         seq: ['#184f95', '#1c5cab', '#256abf', '#2a78d6', '#3987e5', '#5598e7', '#6da7ec', '#86b6ef', '#9ec5f4', '#b7d3f6'],
         ring: '#1a1a19', heat0: '#2a2a27'},
};
const theme = () => document.documentElement.dataset.theme || (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
const pal = () => PAL[theme()];
const slotColor = s => (s == null || s < 0 || s > 7 ? pal().other : pal().cat[s]);
const seqColor = f => { const q = pal().seq; return q[Math.max(0, Math.min(q.length - 1, Math.round(f * (q.length - 1))))]; };
const AGENT_SLOT = {qwen: 0, claude: 1, codex: 2};
const ALL = '__all__';  // pseudo-repo: every repo in one joint layout
const repoLabel = n => n === ALL ? 'All repositories' : n;
const AGENT_NAME = {claude: 'Claude Code', qwen: 'Qwen Code', codex: 'Codex'};

/* ============================== state ============================== */
const S = {
  view: 'data', level: 'session', colorBy: store.get('mapColor', 'area'), areaSlot: new Map(),
  sessions: [], byId: new Map(), chunks: null, topics: [], topicById: new Map(), projects: [], projectSlot: new Map(),
  f: {projects: new Set(), sources: new Set(), range: null, topic: null},
  highlight: null, pulse: new Set(), selected: null, status: {},
  code: {repos: [], repo: store.get('repo', null), points: [], owners: [], people: [], colorBy: 'dir', focus: null, dirSlot: new Map(), authorSlot: new Map(), highlight: null,
         treeSel: new Set(), treeOpen: new Set(), ctree: null},
  files: {points: [], byId: new Map(), colorBy: store.get('filesColor', 'folder'), folderSlot: new Map(), kinds: new Set(),
          folders: new Set(), treeOpen: new Set(store.get('filesTreeOpen', [])), tree: null, highlight: null, loaded: false, stale: false},
  set: null,
  insights: null,
  insAuto: new Set(),
  scope: null,                 // {key, sids, topics, topicById, assign} when a repo / directory filter is active
  tree: {children: new Map(), roots: []},
  treeOpen: new Set(store.get('treeOpen', [])),
  ins: {repo: null, team: null, terr: null, key: null},
};

/* Topics are global, or re-clustered within the current repo/directory scope. */
const TOPICS = () => S.scope ? S.scope.topics : S.topics;
const topicOf = id => (S.scope ? S.scope.topicById : S.topicById).get(id);
const cl = s => S.scope ? (S.scope.assign.get(s.id) ?? -1) : s.cluster;
/* Project filter keys are a repo/folder name ("databricks-bfp") or a directory inside a repo ("databricks-bfp/core/models"). */
const keyLabel = k => k.includes('/') ? k.split('/')[0] + ' › ' + k.split('/').slice(1).join('/') : k;
function matchKey(s, k) {
  if (!k.includes('/')) return s.project === k || (s.dirs || []).some(d => d.startsWith(k + '/'));
  return (s.dirs || []).some(d => d === k || d.startsWith(k + '/'));
}
const matchProj = s => [...S.f.projects].some(k => matchKey(s, k));

/* ============================== 3D engine ============================== */
class Scene3D {
  constructor(el) {
    this.el = el;
    this.renderer = new THREE.WebGLRenderer({antialias: true, alpha: true});
    this.renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
    el.append(this.renderer.domElement);
    this.labels = new CSS2DRenderer(); this.labels.domElement.className = 'labels'; el.append(this.labels.domElement);
    this.scene = new THREE.Scene();
    this.camera = new THREE.PerspectiveCamera(48, 1, 0.01, 60);
    this.camera.position.set(2.4, 1.5, 2.8);
    this.controls = new OrbitControls(this.camera, this.renderer.domElement);
    Object.assign(this.controls, {enableDamping: true, dampingFactor: 0.08, rotateSpeed: 0.7, zoomSpeed: 0.9, autoRotateSpeed: 0.35});
    this.controls.autoRotate = store.get('autorotate', true);
    this.mat = new THREE.ShaderMaterial({
      transparent: true, depthWrite: false,
      uniforms: {uPR: {value: this.renderer.getPixelRatio()}, uScale: {value: 300}, uRing: {value: new THREE.Color(pal().ring)}},
      vertexShader: `attribute vec3 aColor;attribute float aSize;attribute float aState;uniform float uPR;uniform float uScale;
        varying vec3 vC;varying float vS;
        void main(){vC=aColor;vS=aState;vec4 mv=modelViewMatrix*vec4(position,1.);
          float k=aState<.25?.55:aState<.75?.8:aState>1.5?1.75:1.;
          gl_PointSize=max(2.,aSize*k*uPR*uScale/-mv.z);gl_Position=projectionMatrix*mv;}`,
      fragmentShader: `uniform vec3 uRing;varying vec3 vC;varying float vS;
        void main(){float r=length(gl_PointCoord-.5);if(r>.5)discard;
          float a=vS<.25?.07:vS<.75?.2:vS>1.5?1.:.9;
          vec3 c=mix(vC,uRing,smoothstep(.34,.44,r)*.9);
          gl_FragColor=vec4(c,a*(1.-smoothstep(.45,.5,r)));}`,
    });
    this.ringMat = new THREE.ShaderMaterial({
      transparent: true, depthTest: false,
      uniforms: {uPR: {value: this.renderer.getPixelRatio()}, uCol: {value: new THREE.Color('#ffffff')}},
      vertexShader: `attribute float aSize;uniform float uPR;void main(){vec4 mv=modelViewMatrix*vec4(position,1.);gl_PointSize=aSize*uPR;gl_Position=projectionMatrix*mv;}`,
      fragmentShader: `uniform vec3 uCol;void main(){float r=length(gl_PointCoord-.5);float a=smoothstep(.36,.4,r)*(1.-smoothstep(.46,.5,r));if(a<.01)discard;gl_FragColor=vec4(uCol,a);}`,
    });
    const rg = new THREE.BufferGeometry();
    rg.setAttribute('position', new THREE.BufferAttribute(new Float32Array(6), 3));
    rg.setAttribute('aSize', new THREE.BufferAttribute(new Float32Array([0, 0]), 1));
    this.rings = new THREE.Points(rg, this.ringMat); this.rings.frustumCulled = false; this.rings.renderOrder = 2;
    this.scene.add(this.rings);
    this.points = null; this.n = 0; this.labelObjs = []; this.fly = null;
    new ResizeObserver(() => this.resize()).observe(el);
    this.resize();
    const loop = t => { this.tick(t); requestAnimationFrame(loop); };
    requestAnimationFrame(loop);
  }
  resize() {
    const {clientWidth: w, clientHeight: h} = this.el;
    if (!w || !h) return;
    this.renderer.setSize(w, h, false); this.labels.setSize(w, h);
    this.camera.aspect = w / h; this.camera.updateProjectionMatrix();
    this.mat.uniforms.uScale.value = h / 3.2;
  }
  setTheme() {
    this.mat.uniforms.uRing.value.set(pal().ring);
    this.ringMat.uniforms.uCol.value.set(theme() === 'dark' ? '#f4f3ee' : '#0b0b0b');
  }
  setData(pos, colors, sizes, states) {
    if (this.points) { this.scene.remove(this.points); this.points.geometry.dispose(); }
    const g = new THREE.BufferGeometry();
    g.setAttribute('position', new THREE.BufferAttribute(pos, 3));
    g.setAttribute('aColor', new THREE.BufferAttribute(colors, 3));
    g.setAttribute('aSize', new THREE.BufferAttribute(sizes, 1));
    g.setAttribute('aState', new THREE.BufferAttribute(states, 1));
    this.points = new THREE.Points(g, this.mat); this.points.frustumCulled = false;
    this.scene.add(this.points); this.n = pos.length / 3; this.pos = pos; this.states = states;
  }
  attr(name) { return this.points?.geometry.attributes[name]; }
  setRings(hoverIdx, selIdx) {
    const p = this.rings.geometry.attributes.position, s = this.rings.geometry.attributes.aSize;
    [[hoverIdx, 0], [selIdx, 1]].forEach(([i, k]) => {
      if (i == null || i < 0 || !this.pos) { s.array[k] = 0; return; }
      p.array.set(this.pos.subarray(i * 3, i * 3 + 3), k * 3);
      s.array[k] = k ? 30 : 22;
    });
    p.needsUpdate = s.needsUpdate = true;
  }
  pick(cx, cy, maxPx = 14) {
    if (!this.points) return -1;
    const r = this.renderer.domElement.getBoundingClientRect(), mx = cx - r.left, my = cy - r.top;
    const m = new THREE.Matrix4().multiplyMatrices(this.camera.projectionMatrix, this.camera.matrixWorldInverse).elements;
    const P = this.pos, st = this.states, W = r.width, H = r.height;
    let best = -1, bd = maxPx * maxPx, bz = Infinity;
    for (let i = 0; i < this.n; i++) {
      if (st[i] < 0.25) continue;
      const x = P[i * 3], y = P[i * 3 + 1], z = P[i * 3 + 2];
      const w = m[3] * x + m[7] * y + m[11] * z + m[15];
      if (w <= 0) continue;
      const sx = ((m[0] * x + m[4] * y + m[8] * z + m[12]) / w * 0.5 + 0.5) * W;
      const sy = (1 - ((m[1] * x + m[5] * y + m[9] * z + m[13]) / w * 0.5 + 0.5)) * H;
      const d = (sx - mx) ** 2 + (sy - my) ** 2;
      if (d < bd - 4 || (d < bd + 4 && w < bz)) { best = i; bd = Math.min(bd, d); bz = w; }
    }
    return best;
  }
  setLabels(items) {
    this.labelObjs.forEach(o => this.scene.remove(o)); this.labelObjs = [];
    for (const it of items) {
      const el = document.createElement('div'); el.className = 'lbl' + (it.dim ? ' dim' : '') + (it.sel ? ' sel' : '');
      el.innerHTML = `<i class="sw" style="background:${it.color}"></i>${it.pre ? `<span class="lp">${esc(it.pre)}</span>` : ''}${esc(it.text)}${it.badge ? `<span class="cbadge">${esc(it.badge)}</span>` : ''}`;
      el.title = it.title || '';
      el.addEventListener('pointerdown', e => e.stopPropagation());
      el.addEventListener('click', e => { e.stopPropagation(); it.onClick?.(); });
      const o = new CSS2DObject(el); o.position.set(...it.pos); o.userData.prio = (it.sel ? 2e9 : it.dim ? 0 : 1e9) + (it.n || 0);
      this.scene.add(o); this.labelObjs.push(o);
    }
    this.labelObjs.sort((a, b) => b.userData.prio - a.userData.prio);  // biggest (and undimmed) clusters claim space first
  }
  // Greedy screen-space collision avoidance: walk labels in priority order and fade out any that would overlap one
  // already placed. Sizes are measured once per label; positions come from projecting their 3D anchors.
  declutter() {
    const W = this.el.clientWidth, H = this.el.clientHeight, pad = 4, placed = [], v = new THREE.Vector3();
    if (!W || !this.labelObjs.length) return;
    for (const o of this.labelObjs) {
      const el = o.element;
      if (el.style.display === 'none') continue;  // behind the camera (CSS2DRenderer hides it)
      if (!o.userData.w) { o.userData.w = el.offsetWidth; o.userData.h = el.offsetHeight; if (!o.userData.w) continue; }
      v.copy(o.position).project(this.camera);
      const x = (v.x * .5 + .5) * W, y = (-v.y * .5 + .5) * H, hw = o.userData.w / 2 + pad, hh = o.userData.h / 2 + pad;
      const r = [x - hw, y - hh, x + hw, y + hh];
      const hit = placed.some(p => r[0] < p[2] && r[2] > p[0] && r[1] < p[3] && r[3] > p[1]);
      if (!hit) placed.push(r);
      if (el.classList.contains('occl') !== hit) el.classList.toggle('occl', hit);
    }
  }
  fit(indices, instant = false) {
    const idx = indices?.length ? indices : [...Array(this.n).keys()];
    if (!idx.length) return;
    const c = new THREE.Vector3(); idx.forEach(i => c.add(new THREE.Vector3().fromArray(this.pos, i * 3))); c.divideScalar(idx.length);
    const d = idx.map(i => c.distanceTo(new THREE.Vector3().fromArray(this.pos, i * 3))).sort((a, b) => a - b);
    const rad = Math.max(0.25, d[Math.floor(d.length * 0.92)] || 0.25);
    const dist = rad / Math.sin(THREE.MathUtils.degToRad(this.camera.fov / 2)) * 1.05;
    const dir = this.camera.position.clone().sub(this.controls.target).normalize();
    this.flyTo(c, dist, dir, instant);
  }
  focusPoint(i, dist = 0.75) {
    const c = new THREE.Vector3().fromArray(this.pos, i * 3);
    const dir = this.camera.position.clone().sub(this.controls.target).normalize();
    this.flyTo(c, Math.min(dist, this.camera.position.distanceTo(this.controls.target)), dir);
  }
  flyTo(target, dist, dir, instant) {
    const endPos = target.clone().add(dir.multiplyScalar(dist));
    if (instant) { this.controls.target.copy(target); this.camera.position.copy(endPos); return; }
    this.fly = {t0: performance.now(), dur: 700, fromT: this.controls.target.clone(), toT: target.clone(), fromP: this.camera.position.clone(), toP: endPos};
  }
  tick(now) {
    if (this.fly) {
      const f = this.fly, k = Math.min(1, (now - f.t0) / f.dur), e = k < .5 ? 4 * k * k * k : 1 - (-2 * k + 2) ** 3 / 2;
      this.controls.target.lerpVectors(f.fromT, f.toT, e); this.camera.position.lerpVectors(f.fromP, f.toP, e);
      if (k >= 1) this.fly = null;
    }
    this.controls.update();
    this.renderer.render(this.scene, this.camera);
    this.labels.render(this.scene, this.camera);
    if ((this.frame = (this.frame || 0) + 1) % 3 === 0 && !document.body.matches('[data-view=insights],[data-view=settings]')) this.declutter();
  }
  saveCam() { return {p: this.camera.position.clone(), t: this.controls.target.clone()}; }
  loadCam(c) { if (c) { this.camera.position.copy(c.p); this.controls.target.copy(c.t); } }
}

const gl = new Scene3D($('#gl'));
gl.setTheme();

/* ============================== filters ============================== */
function passSession(s, skip) {
  const f = S.f;
  if (skip !== 'projects' && f.projects.size && !matchProj(s)) return false;
  if (skip !== 'sources' && f.sources.size && !f.sources.has(s.source)) return false;
  if (skip !== 'topic' && f.topic != null && cl(s) !== f.topic) return false;
  if (skip !== 'range' && f.range && !(s.t1 >= f.range[0] && s.t0 <= f.range[1])) return false;
  return true;
}
const filtered = skip => S.sessions.filter(s => passSession(s, skip));
const anyFilter = () => S.f.projects.size || S.f.sources.size || S.f.topic != null || S.f.range;
function clearFilters() { S.f.projects.clear(); S.f.sources.clear(); S.f.topic = null; S.f.range = null; S.highlight = null; refresh(); }

/* ============================== map: build + refresh ============================== */
let mapPts = [];      // current map items (sessions or chunks)
let hoverIdx = -1, selIdx = -1, firstFit = true;
const cams = {};

function sessionColorSlot(s) {
  switch (S.colorBy) {
    case 'topic': return cl(s);
    case 'project': return S.projectSlot.has(s.project) ? S.projectSlot.get(s.project) : -1;
    case 'agent': return AGENT_SLOT[s.source] ?? -1;
    case 'area': return S.areaSlot.has(s.area) ? S.areaSlot.get(s.area) : -1;
  }
  return null;
}
function itemColor(it) {
  if (S.view === 'code') return codeColor(it);
  if (S.view === 'files') return fileColor(it);
  if (S.view === 'data') return dataColor(it);
  const s = S.byId.get(it.session) || it;
  if (S.colorBy === 'recency') { const [a, b] = S.tRange; return seqColor(b > a ? (s.t1 - a) / (b - a) : 1); }
  return slotColor(sessionColorSlot(s));
}

function buildMap() {
  mapPts = S.view === 'data' ? S.data.points : S.view === 'code' ? S.code.points : S.view === 'files' ? S.files.points : S.level === 'chunk' ? (S.chunks || []) : S.sessions;
  const n = mapPts.length, pos = new Float32Array(n * 3), col = new Float32Array(n * 3), size = new Float32Array(n), st = new Float32Array(n);
  const c = new THREE.Color();
  mapPts.forEach((p, i) => {
    pos.set(p.p, i * 3); c.set(itemColor(p)); col.set([c.r, c.g, c.b], i * 3);
    size[i] = S.view === 'data' ? (p.type === 'session' ? 0.05 : p.type === 'doc' ? 0.038 : 0.026 + Math.min(0.02, Math.sqrt(p.chunks || 1) * 0.003)) : S.view === 'code' ? 0.028 : S.view === 'files' ? 0.034 + Math.min(0.03, Math.sqrt(p.n || 1) * 0.004) : S.level === 'chunk' ? 0.03 : 0.045 + Math.min(0.06, Math.sqrt(p.n || 1) * 0.008);
  });
  gl.setData(pos, col, size, st);
  selIdx = S.selected ? mapPts.findIndex(p => matchSel(p)) : -1;
  hoverIdx = -1;
  refreshStates(); renderLabels(); renderLegend();
  $('#loading').classList.toggle('gone', n > 0);
  if (n && (firstFit || cams[camKey()] == null)) { gl.fit(null, true); firstFit = false; }
  gl.setRings(-1, selIdx);
}
const camKey = () => S.view === 'data' ? 'data' : S.view === 'code' ? 'code:' + S.code.repo : S.view === 'files' ? 'files' : 'map:' + S.level;
function matchSel(p) {
  const sel = S.selected; if (!sel) return false;
  if (S.view === 'data') return p.open === sel.open || (sel.kind === 'session' && p.open === 'session:' + sel.id) || (sel.kind === 'file' && p.open === sel.id) || (sel.kind === 'code' && (p.open === 'code:' + sel.id || (sel.path && p.path === sel.path)));
  if (S.view === 'code') return sel.kind === 'code' && p.id === sel.id;
  if (S.view === 'files') return sel.kind === 'file' && p.id === sel.id;
  return sel.kind === 'session' && (S.level === 'chunk' ? (sel.chunk ? p.id === sel.chunk : p.session === sel.id) : p.id === sel.id);
}

function recolor() {
  const a = gl.attr('aColor'); if (!a) return;
  const c = new THREE.Color();
  mapPts.forEach((p, i) => { c.set(itemColor(p)); a.array.set([c.r, c.g, c.b], i * 3); });
  a.needsUpdate = true; renderLabels(); renderLegend();
}

function refreshStates() {
  const a = gl.attr('aState'); if (!a) return;
  if (S.view === 'data') {
    const {highlight: hl, search: sr} = S.data;
    mapPts.forEach((p, i) => { let v = dataPass(p) ? 1 : 0; if (v && (hl || sr)) v = (!hl || hl.has(p.id)) && (!sr || sr.has(p.id)) ? 2 : 0.5; a.array[i] = v; });
    a.needsUpdate = true;
    if (dataContextSlots()) recolor(); else renderLegend();  // colors and legend follow what's in view
    return;
  }
  if (S.view === 'files') {
    const hl = S.files.highlight;
    mapPts.forEach((p, i) => { let v = filePass(p) ? 1 : 0; if (v && hl) v = hl.has(p.id) ? 2 : 0.5; a.array[i] = v; });
    a.needsUpdate = true; return;
  }
  const code = S.view === 'code';
  const hl = code ? S.code.highlight : S.highlight;
  mapPts.forEach((p, i) => {
    let v;
    if (code) v = 1;
    else { const s = S.byId.get(p.session); v = s && passSession(s) ? 1 : 0; }
    if (v && hl) v = (hl.has(code ? p.id : p.session) ? 2 : 0.5);
    if (!code && v && S.pulse.has(p.session)) v = 2;
    a.array[i] = v;
  });
  a.needsUpdate = true;
}

function refresh() {
  // re-apply filters everywhere; a changed repo/folder filter also re-clusters topics (async)
  if ((S.scope?.key || '') !== scopeKey()) applyScope();  // sets a loading placeholder synchronously
  if (S.view !== 'insights') { refreshStates(); renderLabels(); }
  renderSidebar(); renderTimeline();
  if (S.view === 'insights') renderInsights();
}

function centroid(idx) {
  const c = [0, 0, 0]; idx.forEach(i => { c[0] += mapPts[i].p[0]; c[1] += mapPts[i].p[1]; c[2] += mapPts[i].p[2]; });
  return c.map(v => v / idx.length);
}

// the folder you zoomed into keeps a label, so clicking it again zooms back out
function pinFolderLabel(items) {
  const D = S.data, sk = D.sel?.kind === 'folder' ? D.sel.key : null; if (!sk) return null;
  const idx = []; mapPts.forEach((p, i) => { if (dataPass(p)) idx.push(i); });
  const r = repoOfKey(sk);
  if (idx.length) items.push({n: idx.length, text: r && r.key === sk ? r.name : sk.split('/').pop(), pre: r && r.key !== sk ? r.name + ' /' : '', sel: true,
    pos: dataAnchor(idx), color: 'transparent', title: `~/${sk}\nclick again to zoom back out`, onClick: () => selectLabel('folder', sk)});
  return sk;
}
function renderLabels() {
  const items = [];
  if (S.view === 'data' && S.data.colorBy !== 'folder') {  // topics: what what's on screen is about, wherever it lives
    const D = S.data, by = new Map(), basis = D.view?.basis;
    pinFolderLabel(items);
    if (D.sel?.kind === 'topic') {  // the topic you zoomed into keeps a label, so clicking it again steps back out
      const idx = []; mapPts.forEach((p, i) => { if (D.sel.members.has(p.id) && dataPass(p)) idx.push(i); });
      if (idx.length) items.push({n: idx.length, text: D.sel.name, sel: true, pos: dataAnchor(idx), color: D.colorBy === 'topic' ? slotColor(D.sel.slot) : 'transparent',
        title: `${plural(idx.length, 'item')} · click to step back out`, onClick: () => deselectLabel()});
    }
    mapPts.forEach((p, i) => { const c = dataTopicOf(p); if (dataInView(p) && c != null && (!basis || basis.has(i))) { if (!by.has(c)) by.set(c, []); by.get(c).push(i); } });
    [...by].filter(([, idx]) => idx.length >= 3).forEach(([id, idx]) => {
      const t = dataTopics().get(id);
      if (D.sel?.kind === 'topic' && t?.name === D.sel.name && by.size === 1) return;  // the same topic again: the pinned label says it
      items.push({n: idx.length, text: t?.name || `Topic ${id + 1}`, pos: dataAnchor(idx),
        color: D.colorBy === 'topic' ? slotColor(topicSlot(id)) : 'transparent',
        title: `${t?.description || (t?.keywords ? 'About: ' + t.keywords : '')}\n${plural(idx.length, 'item')} · click to zoom in`,
        onClick: () => selectLabel('topic', id)});
    });
  } else if (S.view === 'data') {
    const D = S.data, typeN = {doc: 'document', code: 'code file', session: 'agent session'};
    const sk = pinFolderLabel(items);
    dataLabelGroups().filter(g => g.key !== sk).forEach(({key, idx}) => {
      const repo = repoOfKey(key), parts = key.split('/');
      // repos read as "repo / area"; other folders as "parent / name" so "medrec" keeps its "Documents" context
      const [pre, name] = repo ? (repo.key === key ? ['', repo.name] : [repo.name + ' /', tail(key.slice(repo.key.length + 1))])
                               : parts.length > 1 ? [parts.at(-2) + ' /', parts.at(-1)] : ['', key || '~'];
      const tc = {}; idx.forEach(i => { const t = mapPts[i].type; tc[t] = (tc[t] || 0) + 1; });
      const major = Object.keys(tc).sort((a, b) => tc[b] - tc[a])[0];
      items.push({n: idx.length, text: name, pre, badge: repo && repo.key === key ? 'code' : '', pos: dataAnchor(idx),
        color: slotColor(D.folderSlot.get(placeOf(key)) ?? -1),
        title: `~/${key}${repo ? ` · in repository ${repo.name}` : ''}\n${Object.entries(tc).map(([t, c]) => plural(c, typeN[t])).join(', ')}`,
        onClick: () => selectLabel('folder', key)});
    });
  } else if (S.view === 'files') {
    if (S.files.colorBy === 'folder') for (const [k, slot] of S.files.folderSlot) {
      const idx = []; mapPts.forEach((p, i) => { if (p.top === k) idx.push(i); });
      if (idx.length) items.push({n: idx.length, text: k, color: slotColor(slot), pos: centroid(idx), title: `Files in ${k}`,
        dim: S.files.folders.size && ![...S.files.folders].some(f => k === f || k.startsWith(f + '/') || f.startsWith(k + '/')), onClick: () => toggleFolder(k)});
    }
  } else if (S.view === 'code') {
    const by = new Map();
    mapPts.forEach((p, i) => { const k = p.area; if (!by.has(k)) by.set(k, []); by.get(k).push(i); });
    [...by.entries()].sort((a, b) => b[1].length - a[1].length).slice(0, 10).forEach(([k, idx]) => {
      items.push({n: idx.length, text: k, color: S.code.colorBy === 'dir' ? codeColorFor('dir', k) : 'transparent', pos: centroid(idx),
                  dim: S.code.focus && !(S.code.focus.type === 'dir' && S.code.focus.key === k), onClick: () => focusArea(k)});
    });
  } else if (S.colorBy === 'area') {
    for (const [a, slot] of S.areaSlot) {
      const idx = []; mapPts.forEach((p, i) => { const s = S.byId.get(p.session); if (s && s.area === a) idx.push(i); });
      if (!idx.length) continue;
      const k = a;
      items.push({n: idx.length, text: keyLabel(a), color: slotColor(slot), pos: centroid(idx), title: `Sessions that mostly worked in ${a}`,
                  dim: !filtered().some(s => s.area === a), onClick: () => toggleProj(k)});
    }
  } else if (S.level === 'session' || S.level === 'chunk') {
    for (const t of TOPICS()) {
      const idx = []; mapPts.forEach((p, i) => { const s = S.byId.get(p.session); if (s && cl(s) === t.id) idx.push(i); });
      if (!idx.length) continue;
      const vis = filtered().some(s => cl(s) === t.id);
      items.push({n: idx.length, text: t.name, color: slotColor(t.id), pos: centroid(idx), title: t.description,
                  dim: !vis || (S.f.topic != null && S.f.topic !== t.id), onClick: () => toggleTopic(t.id)});
    }
  }
  gl.setLabels(items);
}

function renderLegend() {
  const L = $('#legend'); let html = '';
  if (S.view === 'data') {
    const D = S.data, cb = D.colorBy, sw = (c, l) => `<span><i class="sw" style="background:${c}"></i>${esc(l)}</span>`, ctx = D.ctx || D.points;
    const rest = (slots, key) => ctx.some(p => !slots.has(key(p)));
    if (cb === 'type') html = Object.entries(TYPE_SLOT).filter(([t]) => ctx.some(p => p.type === t)).map(([t, v]) => sw(slotColor(v), TYPE_NAME[t])).join('');
    else if (cb === 'folder') html = [...D.folderSlot].map(([k, v]) => sw(slotColor(v), placeName(k))).join('') + (rest(D.folderSlot, p => p.place) ? sw(pal().other, 'Other') : '');
    else if (cb === 'author') html = [...D.authorSlot].map(([k, v]) => sw(slotColor(v), k)).join('') + (rest(D.authorSlot, p => p.who) ? sw(pal().other, 'Others, bots and agents') : '');
    else if (cb === 'recency') { const [a, b] = D.tRange; html = legendRamp(`Oldest · ${esc(monY(a / 1000) || '?')}`, `Newest · ${esc(monY(b / 1000) || '?')}`, 'Last changed (code: last commit)'); }
    else if (cb === 'risk') html = legendRamp('Authors still active here', 'Authors no longer active', 'Knowledge risk') + sw(pal().other, 'Not code in this repo');
    else if (dataTopics().size) { const n = new Map(); ctx.forEach(p => { const c = dataTopicOf(p); if (c != null) n.set(c, (n.get(c) || 0) + 1); });
      html = [...n].sort((a, b) => b[1] - a[1]).map(([id]) => sw(slotColor(topicSlot(id)), dataTopics().get(id)?.name || `Topic ${id + 1}`)).join(''); }
    else html = '<span class="muted">Colors group items with similar content</span>';
  } else if (S.view === 'files') {
    const cb = S.files.colorBy;
    if (cb === 'recency') html = legendRamp('Older', 'Recently modified');
    else if (cb === 'folder') html = [...S.files.folderSlot].map(([k, v]) => `<span><i class="sw" style="background:${slotColor(v)}"></i>${esc(k)}</span>`).join('') + `<span><i class="sw" style="background:${pal().other}"></i>Other</span>`;
    else if (cb === 'author') html = S.files.authorSlot?.size ? [...S.files.authorSlot].map(([a, v]) => `<span><i class="sw" style="background:${slotColor(v)}"></i>${esc(a)}</span>`).join('') + `<span><i class="sw" style="background:${pal().other}"></i>Other / unknown</span>` : '<span class="muted">No authors found in these files yet</span>';
    else if (cb === 'kind') html = Object.entries(KIND_SLOT).filter(([k]) => S.files.points.some(p => p.kind === k)).map(([k, v]) => `<span><i class="sw" style="background:${slotColor(v)}"></i>${KIND_NAME[k]}</span>`).join('');
    else if (cb === 'topic') html = `<span class="muted">Colors group files with similar content</span>`;
  } else if (S.view === 'code') {
    const cb = S.code.colorBy;
    if (cb === 'recency') html = legendRamp('Older', 'Recently changed');
    else if (cb === 'repo') html = [...(S.code.repoSlot || [])].map(([r, v]) => `<span><i class="sw" style="background:${slotColor(v)}"></i>${esc(r)}</span>`).join('');
    else if (cb === 'linked') html = `<span><i class="sw" style="background:${slotColor(1)}"></i>Touched in my sessions</span><span><i class="sw" style="background:${pal().other}"></i>Not touched</span>`;
    else if (cb === 'author') html = [...S.code.authorSlot].map(([a, s]) => `<span><i class="sw" style="background:${slotColor(s)}"></i>${esc(person(a))}</span>`).join('') + `<span><i class="sw" style="background:${pal().other}"></i>Other</span>`;
  } else if (S.colorBy === 'recency') html = legendRamp('Older', 'Recent');
  else if (S.colorBy === 'area') html = [...S.areaSlot].map(([a, v]) => `<span><i class="sw" style="background:${slotColor(v)}"></i>${esc(keyLabel(a))}</span>`).join('') + `<span><i class="sw" style="background:${pal().other}"></i>Other / no code</span>`;
  else if (S.colorBy === 'agent') html = Object.entries(AGENT_SLOT).filter(([k]) => S.sessions.some(s => s.source === k)).map(([k, v]) => `<span><i class="sw" style="background:${slotColor(v)}"></i>${AGENT_NAME[k]}</span>`).join('');
  else if (S.colorBy === 'project') html = [...S.projectSlot].map(([p, v]) => `<span><i class="sw" style="background:${slotColor(v)}"></i>${esc(p)}</span>`).join('') + (S.projects.length > S.projectSlot.size ? `<span><i class="sw" style="background:${pal().other}"></i>Other</span>` : '');
  L.innerHTML = html;
}
const legendRamp = (a, b, title) => `${title ? `<b class="lg-h">${title}</b>` : ''}<span>${a}</span><span style="display:inline-block;width:110px;height:8px;border-radius:4px;background:linear-gradient(90deg,${pal().seq.join(',')})"></span><span>${b}</span>`;

/* ============================== pointer interaction ============================== */
const canvas = gl.renderer.domElement;
let downAt = null, moveRaf = 0;
canvas.addEventListener('pointerdown', e => {
  downAt = [e.clientX, e.clientY];
  if (gl.controls.autoRotate) { gl.controls.autoRotate = false; syncRotateBtn(); }
  $('#hint').classList.add('gone'); store.set('hinted', true);
});
canvas.addEventListener('pointermove', e => {
  if (moveRaf) return;
  moveRaf = requestAnimationFrame(() => {
    moveRaf = 0;
    if (e.buttons) { tip(null); return; }
    const i = gl.pick(e.clientX, e.clientY);
    if (i !== hoverIdx) { hoverIdx = i; gl.setRings(i, selIdx); }
    canvas.style.cursor = i >= 0 ? 'pointer' : '';
    tip(i >= 0 ? hoverHtml(mapPts[i]) : null, e.clientX, e.clientY);
  });
});
canvas.addEventListener('pointerleave', () => { tip(null); hoverIdx = -1; gl.setRings(-1, selIdx); });
canvas.addEventListener('pointerup', e => {
  if (!downAt || Math.hypot(e.clientX - downAt[0], e.clientY - downAt[1]) > 5) return;
  const i = gl.pick(e.clientX, e.clientY);
  if (i < 0) return;
  const p = mapPts[i];
  if (S.view === 'data') openItem(p.open); else if (S.view === 'code') openCode(p.id); else if (S.view === 'files') openFile(p.path); else openSession(p.session, S.level === 'chunk' ? p.id : null);
});
canvas.addEventListener('dblclick', () => gl.fit(visibleIdx()));
const visibleIdx = () => { const out = []; gl.states?.forEach((v, i) => { if (v >= 0.75) out.push(i); }); return out.length ? out : null; };

function hoverHtml(p) {
  if (S.view === 'data') return `<b>${esc(p.title)}</b><div class="m"><span class="badge"><i class="sw" style="background:${slotColor(TYPE_SLOT[p.type])}"></i>${TYPE_ONE[p.type]}</span>${p.repo ? esc(p.repo) : ''}</div><div class="m">${esc(p.type === 'session' ? (p.repo ? 'in ' + p.repo : p.display) : p.display.replace(/\/[^/]*$/, ''))}</div><div class="m">${p.author ? `by ${esc(p.author)} · ` : ''}${isNaN(p.t) ? '' : rel(p.t)}</div>${p.summary ? `<p>${esc(p.summary.slice(0, 220))}</p>` : ''}`;
  if (S.view === 'files') return `<b>${esc(p.name)}</b><div class="m">${esc(p.dir)}</div><div class="m">${KIND_NAME[p.kind] || p.kind} · ${fmtSize(p.size)} · modified ${rel(p.mtime * 1000)}</div>${p.author ? `<div class="m">by ${esc(p.author)}</div>` : ''}`;
  if (S.view === 'code') {
    return `<b>${esc(p.path)}</b><div class="m">lines ${p.start}–${p.end} · ${esc(person(p.author))}${p.ts ? ' · ' + rel(p.ts * 1000) : ''}</div>` +
      (p.sessions.length ? `<p>Touched in ${plural(p.sessions.length, 'session')}</p>` : '');
  }
  const s = S.byId.get(p.session) || p, t = topicOf(cl(s));
  const sum = S.level === 'chunk' ? p.summary : s.summary;
  return `<b>${esc(s.title)}</b><div class="m"><span class="badge">${esc(AGENT_NAME[s.source] || s.source)}</span>${esc(s.project)} · ${rel(s.t1)}${t ? ` · <i class="sw" style="background:${slotColor(t.id)}"></i>${esc(t.name)}` : ''}</div>` +
    (sum ? `<p>${esc(sum)}</p>` : '');
}

/* ============================== toolbar ============================== */
function renderToolbar() {
  const tb = $('#toolbar');
  if (S.view === 'data') {
    const D = S.data;
    const fr = focusedRepo();
    if (D.colorBy === 'risk' && !fr) D.colorBy = 'type';
    tb.innerHTML = `<div class="seg" id="dcolor"><span class="seg-label">Color</span>${[['type', 'Type'], ['folder', 'Folder'], ['author', 'Author'], ['recency', 'Recency'], ['topic', 'Topic'], ...(fr ? [['risk', 'Knowledge risk']] : [])].map(([k, l]) => `<button data-v="${k}" class="${D.colorBy === k ? 'on' : ''}">${l}</button>`).join('')}</div>
      ${D.highlight ? `<button class="btn on" data-act="cleardhl">✕ Clear highlight</button>` : ''}`;
    $$('#dcolor button', tb).forEach(b => b.onclick = async () => {
      D.colorBy = b.dataset.v; if (D.colorBy !== 'risk') store.set('dataColor', D.colorBy);
      if (D.colorBy === 'risk') await loadGaps(focusedRepo()?.name);
      renderToolbar(); recolor();
    });
  } else if (S.view === 'files') {
    tb.innerHTML = `<button class="btn only-xs" data-act="side">☰ Folders</button>
      <div class="seg" id="fcolor"><span class="seg-label">Color</span>${[['folder', 'Folder'], ['kind', 'Kind'], ['author', 'Author'], ['topic', 'Topic'], ['recency', 'Recency']].map(([k, l]) => `<button data-v="${k}" class="${S.files.colorBy === k ? 'on' : ''}">${l}</button>`).join('')}</div>
      <span style="flex:1"></span>
      ${S.files.highlight ? `<button class="btn on" data-act="clearfhl">✕ Clear highlight</button>` : ''}`;
    $$('#fcolor button', tb).forEach(b => b.onclick = () => { S.files.colorBy = b.dataset.v; store.set('filesColor', S.files.colorBy); renderToolbar(); recolor(); });
  } else if (S.view === 'code') {
    tb.innerHTML = `<button class="btn only-xs" data-act="side">☰ Panel</button>
      <div class="seg" id="ccolor"><span class="seg-label">Color</span>${[...(S.code.repo === ALL ? [['repo', 'Repo']] : []), ['dir', 'Area'], ['author', 'Author'], ['recency', 'Recency'], ['linked', 'My sessions']].map(([k, l]) => `<button data-v="${k}" class="${S.code.colorBy === k ? 'on' : ''}">${l}</button>`).join('')}</div>
      <button class="btn" data-act="who">Who knows about…</button>
      <button class="btn" data-act="overlaps">Overlaps</button>
      <span style="flex:1"></span>`;
    $$('#ccolor button', tb).forEach(b => b.onclick = () => { S.code.colorBy = b.dataset.v; renderToolbar(); recolor(); });
  } else {
    tb.innerHTML = `<button class="btn only-xs" data-act="side">☰ Filters</button>
      <div class="seg" id="lvl"><button data-v="session" class="${S.level === 'session' ? 'on' : ''}" title="One point per session">Sessions</button><button data-v="chunk" class="${S.level === 'chunk' ? 'on' : ''}" title="One point per conversation moment">Moments</button></div>
      <div class="seg" id="mcolor"><span class="seg-label">Color</span>${[['area', 'Code area'], ['topic', 'Topic'], ['project', 'Project'], ['agent', 'Agent'], ['recency', 'Recency']].map(([k, l]) => `<button data-v="${k}" class="${S.colorBy === k ? 'on' : ''}">${l}</button>`).join('')}</div>
      <span style="flex:1"></span>
      ${S.highlight ? `<button class="btn on" data-act="clearhl">✕ Clear highlight (${S.highlight.size})</button>` : ''}`;
    $$('#lvl button', tb).forEach(b => b.onclick = () => setLevel(b.dataset.v));
    $$('#mcolor button', tb).forEach(b => b.onclick = () => { S.colorBy = b.dataset.v; store.set('mapColor', S.colorBy); renderToolbar(); recolor(); renderTimeline(); });
  }
  // the type switch lives under the search field; with the panel collapsed a compact picker stands in here
  $$('[data-act]', tb).forEach(b => b.onclick = () => ({
    fit: () => gl.fit(visibleIdx()),
    clearhl: () => { S.highlight = null; refreshStates(); renderToolbar(); },
    clearfhl: () => { S.files.highlight = null; refreshStates(); renderToolbar(); },
    cleardhl: () => { S.data.highlight = null; clearSel(); refreshStates(); renderToolbar(); renderLabels(); },
    side: () => toggleSide(),
    who: () => openPalette('who: '),
    overlaps: showOverlaps,
  })[b.dataset.act]());
}

async function setLevel(l) {
  cams[camKey()] = gl.saveCam();
  S.level = l;
  if (l === 'chunk' && !S.chunks) {
    $('#loading').classList.remove('gone'); $('#loadingtext').textContent = 'Loading conversation moments…';
    S.chunks = (await api('/api/points?level=chunk')).points;
  }
  renderToolbar(); buildMap(); gl.loadCam(cams[camKey()]);
}

/* ============================== sidebar ============================== */
function renderSidebar() {
  const side = $('#side');
  if (S.view === 'code') return renderCodeSidebar(side);
  if (S.view === 'files') return renderFilesSidebar(side);
  if (S.view === 'data') return renderDataSidebar(side);
  if (S.view === 'settings') { side.innerHTML = settingsNav(); bindSettingsNav(side); return; }
  const all = S.sessions, fs = filtered();
  const parts = [];
  // active filters summary
  const af = [];
  S.f.sources.forEach(v => af.push(['sources', v, AGENT_NAME[v] || v]));
  S.f.projects.forEach(v => af.push(['projects', v, keyLabel(v)]));
  if (S.f.topic != null) af.push(['topic', S.f.topic, topicOf(S.f.topic)?.name || 'Topic']);
  if (S.f.range) af.push(['range', 0, `${fmtD(S.f.range[0])} – ${fmtD(S.f.range[1])}`]);
  if (af.length) parts.push(`<div class="active-filters">${af.map(([k, v, l]) => `<span class="chip">${esc(l)}<button class="x" data-rm="${k}" data-v="${esc(v)}">✕</button></span>`).join('')}<button class="chip" data-clear>Clear all</button></div>`);
  parts.push(`<div class="sec"><div class="sec-h"><span>Showing</span></div><div style="padding:0 4px;font-size:13px"><b>${fs.length}</b> <span class="muted">of ${plural(all.length, 'session')}</span></div></div>`);
  // agents
  const ag = {}; filtered('sources').forEach(s => ag[s.source] = (ag[s.source] || 0) + 1);
  const agents = [...new Set(all.map(s => s.source))];
  parts.push(`<div class="sec"><div class="sec-h"><span>Agents</span></div><div class="chips">${agents.map(a => `<button class="chip ${S.f.sources.has(a) ? 'on' : ''}" data-src="${a}"><i class="sw" style="background:${slotColor(AGENT_SLOT[a])}"></i>${AGENT_NAME[a] || a}<span class="n">${ag[a] || 0}</span></button>`).join('')}</div></div>`);
  // topics (re-clustered when a project filter is active)
  const tc = {}; filtered('topic').forEach(s => { const c = cl(s); tc[c] = (tc[c] || 0) + 1; });
  const tmax = Math.max(1, ...Object.values(tc));
  const scoped = !!S.scope, naming = scoped ? S.scope.topics.some(t => !t.named) : S.status.topics_named < S.status.topics;
  parts.push(`<div class="sec"><div class="sec-h"><span>${scoped ? 'Topics in this scope' : 'Topics'}</span>${S.scope?.loading ? '<span class="spinner" style="width:11px;height:11px;border-width:2px"></span>' : naming ? '<span class="muted" style="text-transform:none;letter-spacing:0">naming…</span>' : ''}</div>
    ${scoped ? `<div class="sec-note">Re-clustered from the ${plural(S.scope.sids.size, 'session')} in ${esc([...S.f.projects].map(keyLabel).join(', '))}.</div>` : ''}
    <div class="rows">${TOPICS().map(t => `
    <button class="row ${S.f.topic === t.id ? 'on' : ''}" data-topic="${t.id}" title="${esc(t.description)}">
      <i class="sw" style="background:${slotColor(t.id)}"></i><span class="name ${t.named ? '' : 'shimmer'}">${esc(t.name)}</span><span class="n">${tc[t.id] || 0}</span>
      ${t.description ? `<span class="sub">${esc(t.description)}</span>` : ''}
      <span class="bar"><i style="width:${(tc[t.id] || 0) / tmax * 100}%;background:${slotColor(t.id)}"></i></span>
    </button>`).join('')}</div></div>`);
  // projects: repos with their directory hierarchy, then plain folders
  const cnt = treeCounts(filtered('projects'));
  const q = (store.projQ || '').toLowerCase();
  const repos = S.projects.filter(p => p.repo), folders = S.projects.filter(p => !p.repo);
  let tree = '';
  if (q) {
    const hits = [...cnt.keys()].filter(k => k.toLowerCase().includes(q)).sort((x, y) => cnt.get(y) - cnt.get(x)).slice(0, 30);
    tree = hits.map(k => treeRow(k, keyLabel(k), 0, cnt.get(k), [], 'proj')).join('') || '<div class="empty" style="padding:8px">No matching folders</div>';
  } else {
    tree = repos.map(p => treeHtml(p.name, 0, cnt)).join('');
    if (folders.length) tree += `<div class="sec-note" style="margin:10px 4px 4px" title="These come from your agent sessions, not from the folders Pensieve indexes">Other places your agents ran · not indexed repos</div>` + folders.map(p => treeRow(p.name, p.name, 0, cnt.get(p.name) || 0, [], 'proj', esc(p.path || ''))).join('');
  }
  parts.push(`<div class="sec"><div class="sec-h"><span>Projects</span>${S.f.projects.size ? '<button data-clearproj>Clear</button>' : ''}</div>
    <div class="sec-note">Expand a repo to filter by folder — sessions count when they ran in or touched files under it.</div>
    <input class="filterbox" id="projq" placeholder="Filter repos and folders…" value="${esc(store.projQ || '')}">
    <div class="tree">${tree}</div></div>`);
  side.innerHTML = parts.join('');
  $$('[data-src]', side).forEach(b => b.onclick = () => { toggleSet(S.f.sources, b.dataset.src); refresh(); });
  $$('[data-topic]', side).forEach(b => b.onclick = () => toggleTopic(+b.dataset.topic));
  $$('[data-topic]', side).forEach(b => { b.onmouseenter = () => previewTopic(+b.dataset.topic); b.onmouseleave = () => previewTopic(null); });
  bindTree(side, 'proj', k => { toggleProj(k); }, () => renderSidebar());
  $$('[data-rm]', side).forEach(b => b.onclick = () => {
    const k = b.dataset.rm;
    if (k === 'topic') S.f.topic = null; else if (k === 'range') S.f.range = null; else S.f[k].delete(b.dataset.v);
    refresh();
  });
  $('[data-clear]', side)?.addEventListener('click', clearFilters);
  $('[data-clearproj]', side)?.addEventListener('click', () => { S.f.projects.clear(); refresh(); });
  const pq = $('#projq', side);
  if (pq) pq.oninput = debounce(() => { store.projQ = pq.value; renderSidebar(); $('#projq').focus(); $('#projq').setSelectionRange(99, 99); }, 120);
}

/* ---- hierarchy tree ---- */
// Build parent → children from every session's repo directories, so the tree shape is stable across filters.
function buildTree() {
  const children = new Map(), seen = new Set();
  const add = k => {
    if (seen.has(k)) return; seen.add(k);
    const i = k.lastIndexOf('/'); if (i < 0) return;
    const parent = k.slice(0, i); add(parent);
    if (!children.has(parent)) children.set(parent, []);
    children.get(parent).push(k);
  };
  S.sessions.forEach(s => (s.dirs || []).forEach(add));
  S.tree = {children};
}
function treeCounts(list) {
  const cnt = new Map();
  for (const s of list) {
    const ks = new Set([s.project]);
    for (const d of s.dirs || []) { const p = d.split('/'); for (let i = 1; i <= p.length; i++) ks.add(p.slice(0, i).join('/')); }
    ks.forEach(k => cnt.set(k, (cnt.get(k) || 0) + 1));
  }
  return cnt;
}
function treeRow(k, label, depth, n, kids, kind, sub = '', open = false) {
  const selSet = kind === 'proj' ? S.f.projects : kind === 'fdir' ? S.files.folders : kind === 'ddir' ? S.data.folders : S.code.treeSel;
  if (kind === 'ddir' && S.data.repoKeys.has(k)) label = `${label}<span class="cbadge" title="Git repository">code</span>`;
  const on = selSet.has(k), part = !on && [...selSet].some(x => x.startsWith(k + '/'));
  return `<div class="trow ${on ? 'on' : part ? 'part' : ''}" style="--d:${depth}">
    ${kids.length ? `<button class="tw ${open ? 'open' : ''}" data-tw="${esc(k)}" title="${open ? 'Collapse' : 'Expand'}">▶</button>` : '<span class="tw"></span>'}
    <button class="tname" data-${kind}="${esc(k)}" title="${esc(k)}"><span class="check"></span><span class="name ${depth ? 'dir' : ''}">${kind === 'ddir' ? label.replace(/^([^<]*)/, m => esc(m)) : esc(label)}</span><span class="n">${n.toLocaleString()}</span></button>
    ${sub ? `<div class="tsub">${sub}</div>` : ''}</div>`;
}
// Recursive tree; single-child chains with the same count collapse into one row ("core/pipelines/company").
function treeHtml(key, depth, cnt, kind = 'proj', children = S.tree.children, subFn = null, openSet = S.treeOpen) {
  let k = key, label = depth ? key.split('/').pop() : key;
  const kidsOf = x => (children.get(x) || []).filter(c => cnt.get(c));
  let kids = kidsOf(k);
  while (depth && kids.length === 1 && cnt.get(kids[0]) === cnt.get(k)) { k = kids[0]; label += '/' + k.split('/').pop(); kids = kidsOf(k); }
  const n = cnt.get(k) || 0;
  if (!n && depth) return '';
  const open = openSet.has(k);
  let h = treeRow(k, label, depth, n, kids, kind, subFn ? subFn(k) : '', open);
  if (open) {
    kids.sort((a, b) => cnt.get(b) - cnt.get(a));
    h += kids.slice(0, 16).map(c => treeHtml(c, depth + 1, cnt, kind, children, subFn, openSet)).join('');
    if (kids.length > 16) h += `<div class="tmore" style="--d:${depth + 1}">+ ${kids.length - 16} more</div>`;
  }
  return h;
}
function bindTree(root, kind, onPick, rerender, openSet = S.treeOpen, persist = 'treeOpen') {
  $$(`[data-${kind}]`, root).forEach(b => b.onclick = () => onPick(b.dataset[kind]));
  $$('[data-tw]', root).forEach(b => b.onclick = e => {
    e.stopPropagation(); toggleSet(openSet, b.dataset.tw);
    if (persist) store.set(persist, [...openSet]);
    rerender();
  });
}
function toggleProj(k) {
  if (S.f.projects.has(k)) S.f.projects.delete(k);
  else {
    // selecting a folder replaces its ancestors/descendants so the filter stays unambiguous
    [...S.f.projects].forEach(x => { if (x.startsWith(k + '/') || k.startsWith(x + '/')) S.f.projects.delete(x); });
    S.f.projects.add(k);
    if (S.tree.children.has(k)) { S.treeOpen.add(k); store.set('treeOpen', [...S.treeOpen]); }
  }
  refresh();
  if (S.view === 'map') setTimeout(() => gl.fit(visibleIdx()), 350);
}

/* ---- scope: topics re-clustered for the active repo / folder filter ---- */
const scopeKey = () => [...S.f.projects].sort().join('|');
let scopeSeq = 0;
async function syncScope(force = false) {
  const key = scopeKey();
  if (!force && (S.scope?.key || '') === key) return false;
  const seq = ++scopeSeq;
  if (!key) { S.scope = null; S.f.topic = null; return true; }
  const sids = new Set(S.sessions.filter(matchProj).map(s => s.id));
  if (!force) { S.scope = {key, sids, topics: [], topicById: new Map(), assign: new Map(), loading: true}; S.f.topic = null; }
  let topics = [];
  try { topics = await api('/api/topics/scoped', {method: 'POST', headers: {'content-type': 'application/json'}, body: JSON.stringify({session_ids: [...sids], scope: key})}); } catch {}
  if (seq !== scopeSeq) return false;
  const assign = new Map(); topics.forEach(t => t.sessions.forEach(id => assign.set(id, t.id)));
  S.scope = {key, sids, topics, topicById: new Map(topics.map(t => [t.id, t])), assign, loading: false};
  return true;
}
async function applyScope(force = false) {
  const changed = await syncScope(force);
  if (!changed) return;
  if (S.view === 'map') { recolor(); refreshStates(); renderLabels(); }
  renderSidebar(); renderTimeline();
  if (S.view === 'insights') { S.ins.key = null; loadInsights().then(renderInsights); }
}
const toggleSet = (set, v) => set.has(v) ? set.delete(v) : set.add(v);
function toggleTopic(id) {
  S.f.topic = S.f.topic === id ? null : id;
  refresh();
  if (S.f.topic != null) setTimeout(() => gl.fit(visibleIdx()), 30);
}
let previewing = null;
function previewTopic(id) {
  if (S.view !== 'map') return;
  previewing = id;
  if (id == null) { refreshStates(); return; }
  const a = gl.attr('aState'); if (!a) return;
  mapPts.forEach((p, i) => { const s = S.byId.get(p.session); if (a.array[i] >= 0.75 && s && cl(s) !== id) a.array[i] = 0.5; else if (s && cl(s) === id && passSession(s, 'topic')) a.array[i] = 2; });
  a.needsUpdate = true;
}

/* ============================== timeline ============================== */
const tl = {bins: [], x0: 0, x1: 1, drag: null};
function renderTimeline() {
  const svg = $('#tl');
  const code = S.view !== 'map';
  $('#timeline').style.display = code ? 'none' : '';
  if (code || !S.sessions.length) { svg.innerHTML = ''; return; }
  const W = svg.clientWidth || 600, H = svg.clientHeight || 60, padB = 16, padT = 4;  // measure after it is shown
  const ss = filtered('range');
  const [a, b] = S.tRange, span = Math.max(DAY, b - a);
  const bin = span < 50 * DAY ? DAY : span < 400 * DAY ? 7 * DAY : 30 * DAY;
  const start = Math.floor(a / bin) * bin, nb = Math.max(1, Math.ceil((b - start + 1) / bin));
  const bins = Array.from({length: nb}, (_, i) => ({t0: start + i * bin, t1: start + (i + 1) * bin, by: new Map(), n: 0}));
  for (const s of ss) {
    const i = Math.min(nb - 1, Math.max(0, Math.floor((s.t1 - start) / bin)));
    const k = S.colorBy === 'recency' ? 'all' : (sessionColorSlot(s) ?? -1);
    bins[i].by.set(k, (bins[i].by.get(k) || 0) + 1); bins[i].n++;
  }
  const max = Math.max(1, ...bins.map(x => x.n)), bw = (W) / nb, ih = H - padB - padT;
  tl.bins = bins; tl.start = start; tl.bin = bin; tl.W = W; tl.nb = nb;
  const out = [];
  bins.forEach((x, i) => {
    let y = H - padB;
    const keys = [...x.by.keys()].sort((p, q) => (p === -1) - (q === -1) || p - q);
    for (const k of keys) {
      const h = x.by.get(k) / max * ih;
      const col = k === 'all' ? pal().seq[6] : slotColor(k);
      out.push(`<rect x="${i * bw + 1}" y="${y - h}" width="${Math.max(1, bw - 2)}" height="${Math.max(0, h - 1)}" fill="${col}" rx="${bw > 8 ? 1.5 : 0}"/>`);
      y -= h;
    }
  });
  // ticks
  const fmt = bin >= 30 * DAY ? (t => new Date(t).toLocaleDateString(undefined, {month: 'short', year: '2-digit'})) : fmtD;
  const nt = Math.min(8, Math.floor(W / 90));
  for (let j = 0; j <= nt; j++) {
    const t = start + (nb * bin) * j / nt;
    out.push(`<text class="axis" x="${Math.min(W - 30, (t - start) / (nb * bin) * W)}" y="${H - 3}">${fmt(t)}</text>`);
  }
  if (S.f.range) {
    const x0 = (S.f.range[0] - start) / (nb * bin) * W, x1 = (S.f.range[1] - start) / (nb * bin) * W;
    out.push(`<rect class="brush" x="${Math.max(0, x0)}" y="0" width="${Math.max(2, x1 - x0)}" height="${H - padB}" rx="3"/>`);
  }
  if (tl.drag) out.push(`<rect class="brush" x="${Math.min(tl.drag.x0, tl.drag.x1)}" y="0" width="${Math.abs(tl.drag.x1 - tl.drag.x0)}" height="${H - padB}" rx="3"/>`);
  svg.innerHTML = out.join('');
}
const tlTime = x => tl.start + (x / tl.W) * tl.nb * tl.bin;
{
  const svg = $('#tl');
  const lx = e => e.clientX - svg.getBoundingClientRect().left;
  svg.addEventListener('pointerdown', e => { svg.setPointerCapture(e.pointerId); tl.drag = {x0: lx(e), x1: lx(e)}; });
  svg.addEventListener('pointermove', e => {
    if (tl.drag) { tl.drag.x1 = lx(e); renderTimeline(); tip(null); return; }
    const i = Math.floor(lx(e) / (tl.W / tl.nb)), x = tl.bins[i];
    if (!x) return tip(null);
    const rows = [...x.by.entries()].sort((a, b) => b[1] - a[1]).slice(0, 6).map(([k, n]) => `<tr><td><i class="sw" style="background:${k === 'all' ? pal().seq[6] : slotColor(k)}"></i> ${esc(slotLabel(k))}</td><td>${n}</td></tr>`).join('');
    tip(`<b>${fmtD(x.t0)}${tl.bin > DAY ? ' – ' + fmtD(x.t1 - DAY) : ''}</b><div class="m">${plural(x.n, 'session')}</div>${rows ? `<table>${rows}</table>` : ''}<div class="m" style="margin-top:4px">Drag to filter a range</div>`, e.clientX, e.clientY);
  });
  svg.addEventListener('pointerleave', () => tip(null));
  svg.addEventListener('pointerup', e => {
    if (!tl.drag) return;
    const {x0, x1} = tl.drag; tl.drag = null;
    if (Math.abs(x1 - x0) < 4) { // click: select that bin
      const i = Math.floor(x0 / (tl.W / tl.nb)), x = tl.bins[i];
      S.f.range = x && x.n ? [x.t0, x.t1 - 1] : null;
    } else S.f.range = [tlTime(Math.min(x0, x1)), tlTime(Math.max(x0, x1))];
    refresh();
  });
  svg.addEventListener('dblclick', () => { S.f.range = null; refresh(); });
}
function slotLabel(k) {
  if (k === 'all') return 'Sessions';
  if (S.colorBy === 'topic') return topicOf(k)?.name || 'Other';
  if (S.colorBy === 'area') return keyLabel([...S.areaSlot].find(([, v]) => v === k)?.[0] || 'Other / no code');
  if (S.colorBy === 'agent') return AGENT_NAME[Object.keys(AGENT_SLOT).find(a => AGENT_SLOT[a] === k)] || 'Other';
  if (S.colorBy === 'project') return [...S.projectSlot].find(([, v]) => v === k)?.[0] || 'Other';
  return 'Other';
}

/* ============================== drawer: session ============================== */
function openDrawer(titleHtml, bodyHtml) {
  S.data.drawerTopic = null; S.data.drawerPerson = null;
  $('#drawer-title').innerHTML = titleHtml; $('#drawer-body').innerHTML = bodyHtml; $('#drawer-body').scrollTop = 0;
  $('#drawer').classList.add('open'); $('#drawer').setAttribute('aria-hidden', 'false');
}
function closeDrawer() {
  S.data.drawerTopic = null; S.data.drawerPerson = null;
  $('#drawer').classList.remove('open'); $('#drawer').setAttribute('aria-hidden', 'true');
  S.selected = null; selIdx = -1; gl.setRings(hoverIdx, -1);
}
$('#drawer-close').onclick = closeDrawer;

async function openSession(sid, chunkId = null) {
  if (S.view !== 'data') await setView('data');
  if (!S.data.types.has('session')) { S.data.types.add('session'); dataChanged(false); }
  S.selected = {kind: 'session', id: sid, chunk: chunkId};
  selIdx = mapPts.findIndex(matchSel); gl.setRings(hoverIdx, selIdx);
  if (selIdx >= 0) gl.focusPoint(selIdx);
  const base = S.byId.get(sid);
  openDrawer(`<div class="meta"><span class="spinner" style="width:14px;height:14px;border-width:2px"></span> Loading…</div><h2>${esc(base?.title || '')}</h2>`, '');
  const [s, sim, code] = await Promise.all([api('/api/session/' + enc(sid)), api('/api/similar/' + enc(sid)).catch(() => []), api('/api/sessioncode/' + enc(sid)).catch(() => null)]);
  if (S.selected?.id !== sid) return;
  const t = topicOf(cl(S.byId.get(sid) || s));
  const resume = s.source === 'claude' ? `cd ${JSON.stringify(s.cwd || '')} && claude --resume ${s.id.split(':')[1]}` : null;
  openDrawer(`<div class="meta"><span class="badge"><i class="sw" style="background:${slotColor(AGENT_SLOT[s.source])}"></i>${esc(AGENT_NAME[s.source] || s.source)}</span>
      <button class="chip" data-proj="${esc(s.project)}">${esc(s.project)}</button>
      ${t ? `<button class="chip" data-topic="${t.id}"><i class="sw" style="background:${slotColor(t.id)}"></i>${esc(t.name)}</button>` : ''}</div>
      <h2>${esc(s.title)}</h2><div class="meta" style="margin-top:4px">${fmtDY(T(s.started))} ${T(s.updated) - T(s.started) > 6e4 ? '· ' + durStr(T(s.updated) - T(s.started)) : ''} · ${plural(s.turns.length, 'turn')}</div>`,
    `${s.summary ? `<p class="summary">${esc(s.summary)}</p>` : '<p class="muted">Summary is being generated…</p>'}
     ${s.tags ? `<div class="chips">${s.tags.split(',').map(x => `<span class="chip">${esc(x)}</span>`).join('')}</div>` : ''}
     ${leafDirs(base?.dirs || []).length ? `<div class="h4">Worked in</div><div class="chips">${leafDirs(base.dirs).slice(0, 10).map(d => `<button class="chip" data-dirkey="${esc(d)}" title="Filter to sessions that touched ${esc(d)}" style="font:11px var(--mono)">${esc(keyLabel(d))}</button>`).join('')}</div>` : ''}
     <div class="actions">
       ${resume ? `<button class="btn" data-a="resume" title="${esc(resume)}">⧉ Copy resume command</button>` : ''}
       <button class="btn" data-a="similar">Show similar on map</button>
     </div>
     ${codeSection(code)}
     ${sim.length ? `<div class="h4">Similar sessions</div><div class="list">${sim.map(x => `<button class="item" data-sid="${esc(x.id)}"><div class="t"><span class="grow">${esc(x.title)}</span><span class="muted" style="font-size:11px">${Math.round(x.score * 100)}%</span></div><div class="s">${esc(x.project)} · ${rel(T(x.started))}${x.summary ? ' — ' + esc(x.summary.slice(0, 140)) : ''}</div></button>`).join('')}</div>` : ''}
     <div class="h4"><span>Transcript</span><span class="muted" style="text-transform:none;letter-spacing:0">${plural(s.turns.length, 'turn')}</span></div>
     <input class="filterbox" id="tfind" placeholder="Find in transcript…">
     <div id="turns"></div>`);
  const body = $('#drawer-body');
  $$('[data-sid]', body).forEach(b => b.onclick = () => openSession(b.dataset.sid));
  $$('[data-proj]', $('#drawer')).forEach(b => b.onclick = () => { const k = repoDirKey(b.dataset.proj, ''); if (k) toggleDataFolder(k, true); else toast('Not a repository in the index'); });
  $$('[data-dirkey]', $('#drawer')).forEach(b => b.onclick = () => { const [r, ...rest] = b.dataset.dirkey.split('/'); const k = repoDirKey(r, rest.join('/')); if (k) toggleDataFolder(k, true); });
  $$('[data-topic]', $('#drawer')).forEach(b => b.onclick = () => { const t = +b.dataset.topic; highlightData(p => p.type === 'session' && S.byId.get(p.open.slice(8))?.cluster === t); });
  $$('[data-file]', body).forEach(b => b.onclick = () => gotoCode(b.dataset.repo, b.dataset.file));
  $$('[data-person]', body).forEach(b => b.onclick = () => gotoCode(b.dataset.repo, null, b.dataset.person));
  $('[data-a=resume]', body)?.addEventListener('click', () => { navigator.clipboard?.writeText(resume); toast('Resume command copied'); });
  $('[data-a=similar]', body).onclick = () => { const ids = new Set([sid, ...sim.map(x => x.id)].map(x => 'session:' + x)); highlightData(p => ids.has(p.open)); };
  renderTurns(s.turns, '', chunkId);
  $('#tfind').oninput = debounce(e => renderTurns(s.turns, $('#tfind').value.trim(), null), 150);
}
// deepest directories only (drop 'a/b' when 'a/b/c' is present)
const leafDirs = dirs => dirs.filter(d => !dirs.some(x => x.startsWith(d + '/')));
const durStr = ms => ms < 36e5 ? `${Math.round(ms / 6e4)} min` : ms < 864e5 ? `${(ms / 36e5).toFixed(1)} h` : `${Math.round(ms / 864e5)} days`;

function codeSection(c) {
  if (!c || !c.repo) return '';
  const edited = c.files.filter(f => f.edited), root = c.repo;
  const short = p => p.split('/').slice(-3).join('/');
  let h = `<div class="h4">Delivered code · ${esc(c.repo)}</div>`;
  h += c.commits.length ? `<div class="list">${c.commits.map(x => `<div class="item"><div class="t"><code style="font:11px var(--mono);color:var(--text-3)">${x.sha}</code><span class="grow">${esc(x.subject)}</span></div><div class="s">${rel(x.ts * 1000)}</div></div>`).join('')}</div>` : `<div class="muted" style="font-size:12px">No commits of yours landed during this session.</div>`;
  if (edited.length) h += `<div class="h4">Files edited <span class="muted" style="text-transform:none;letter-spacing:0">${edited.length}</span></div><div class="list">${edited.slice(0, 10).map(f => `<button class="item" data-repo="${esc(root)}" data-file="${esc(f.path)}"><div class="t"><span class="grow" style="font:12px var(--mono)">${esc(short(f.path))}</span><span class="muted">→</span></div></button>`).join('')}</div>`;
  if (c.experts.length) h += `<div class="h4">People who know this area</div><div class="kv">${c.experts.slice(0, 4).map(e => `<button class="item" style="grid-column:1/4" data-repo="${esc(root)}" data-person="${esc(e.author)}"><div class="t"><span class="grow">${esc(person(e.author))}</span><span class="muted">${Math.round(e.share * 100)}%</span></div><div class="hbar"><i style="width:${Math.round(e.share * 100)}%"></i></div><div class="s">${esc(e.files.slice(0, 2).join(', '))}</div></button>`).join('')}</div>`;
  return h;
}

function renderTurns(turns, q, chunkId) {
  const box = $('#turns'); if (!box) return;
  const ql = q.toLowerCase();
  const rows = [];
  let shown = 0;
  turns.forEach((t, i) => {
    if (ql && !t.text.toLowerCase().includes(ql)) return;
    if (!ql && shown >= 80) return;
    shown++;
    const long = t.text.length > 1400 && !ql;
    let body = esc(long ? t.text.slice(0, 1400) + '…' : t.text);
    if (ql) body = body.replace(new RegExp(ql.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), 'gi'), m => `<mark>${m}</mark>`);
    rows.push(`<div class="turn ${t.role}" data-i="${i}"><span class="who">${t.role === 'user' ? 'You' : 'Agent'}</span>${body}${long ? `<br><button class="more" data-more="${i}">Show full message</button>` : ''}</div>`);
  });
  box.innerHTML = rows.join('') + (!ql && turns.length > 80 ? `<button class="btn" id="allturns">Show all ${turns.length} turns</button>` : '') + (ql && !rows.length ? '<div class="empty">No matches</div>' : '');
  $$('[data-more]', box).forEach(b => b.onclick = () => { const i = +b.dataset.more; b.parentElement.innerHTML = `<span class="who">${turns[i].role === 'user' ? 'You' : 'Agent'}</span>${esc(turns[i].text)}`; });
  $('#allturns')?.addEventListener('click', () => { box.innerHTML = turns.map(t => `<div class="turn ${t.role}"><span class="who">${t.role === 'user' ? 'You' : 'Agent'}</span>${esc(t.text)}</div>`).join(''); });
}

/* ============================== code view ============================== */
async function loadRepos() {
  const repos = await api('/api/repos');
  const real = repos.filter(r => r.chunks);
  S.code.repos = real.length > 1 ? [{name: ALL, root: ALL, chunks: real.reduce((a, r) => a + r.chunks, 0), files: real.reduce((a, r) => a + r.files, 0),
    sessions: real.reduce((a, r) => a + r.sessions, 0), all: true, n: real.length}, ...repos] : repos;
  if (!S.code.repos.some(r => r.name === S.code.repo)) S.code.repo = S.code.repos[0]?.name || null;
}
async function loadRepo(name) {
  if (!name) { S.code.points = []; return; }
  S.code.repo = name; store.set('repo', name);
  if (name === ALL && S.code.colorBy === 'dir') S.code.colorBy = 'repo';
  if (name !== ALL && S.code.colorBy === 'repo') S.code.colorBy = 'dir';
  $('#loading').classList.remove('gone'); $('#loadingtext').textContent = `Loading ${name}…`;
  const [pts, owners, people] = await Promise.all([api(`/api/repo/${enc(name)}/points`), api(`/api/repo/${enc(name)}/owners`), api(`/api/repo/${enc(name)}/people`)]);
  const depth = name === ALL ? 3 : 2;
  S.code.points = pts.points.map(p => ({...p, area: areaOf(p.path, depth), repo: name === ALL ? p.path.split('/')[0] : name}));
  S.code.repoSlot = new Map([...new Set(S.code.points.map(p => p.repo))].map((r, i) => [r, i]));
  S.code.owners = owners; S.code.people = people;
  S.code.dirSlot = new Map(owners.slice(0, 7).map((o, i) => [o.dir, i]));
  S.code.authorSlot = new Map(people.slice(0, 7).map((p, i) => [p.author, i]));
  const ts = S.code.points.map(p => p.ts).filter(Boolean).sort((a, b) => a - b);
  S.code.tRange = [ts[Math.floor(ts.length * 0.02)] || 0, ts[ts.length - 1] || 1];
  S.code.focus = null; S.code.highlight = null; S.code.treeSel = new Set(); S.code.treeOpen = new Set(); S.code.ctree = null;
}
const areaOf = (path, depth = 2) => { const p = path.split('/'); return p.length > depth ? p.slice(0, depth).join('/') : p.length > 1 ? p.slice(0, -1).join('/') : '.'; };
function codeColorFor(kind, key) {
  const m = kind === 'dir' ? S.code.dirSlot : S.code.authorSlot;
  return slotColor(m.has(key) ? m.get(key) : -1);
}
function codeColor(p) {
  switch (S.code.colorBy) {
    case 'dir': return codeColorFor('dir', p.area);
    case 'repo': return slotColor(S.code.repoSlot?.get(p.repo) ?? -1);
    case 'author': return codeColorFor('author', p.author);
    case 'linked': return p.sessions.length ? slotColor(1) : pal().other;
    case 'recency': { const [a, b] = S.code.tRange; return seqColor(p.ts ? (p.ts - a) / Math.max(1, b - a) : 0); }
  }
}
function codeTree() {
  // directory hierarchy of the indexed code: chunk counts and top authors per folder
  if (S.code.ctree?.repo === S.code.repo && S.code.ctree.n === S.code.points.length) return S.code.ctree;
  const children = new Map(), cnt = new Map(), authors = new Map(), seen = new Set();
  for (const p of S.code.points) {
    const parts = p.path.split('/'); parts.pop();
    let key = '';
    const keys = parts.length ? [] : ['.'];
    for (const part of parts) { const k = key ? key + '/' + part : part; if (!seen.has(k)) { seen.add(k); if (key) { if (!children.has(key)) children.set(key, []); children.get(key).push(k); } } keys.push(k); key = k; }
    for (const k of keys) {
      cnt.set(k, (cnt.get(k) || 0) + 1);
      if (!authors.has(k)) authors.set(k, new Map());
      const m = authors.get(k); m.set(p.author, (m.get(p.author) || 0) + 1);
    }
  }
  const roots = [...cnt.keys()].filter(k => !k.includes('/')).sort((x, y) => cnt.get(y) - cnt.get(x));
  return (S.code.ctree = {repo: S.code.repo, n: S.code.points.length, children, cnt, authors, roots});
}
const topAuthors = (m, n = 2) => [...(m || new Map())].filter(([a]) => a && !/\[bot\]/.test(a)).sort((a, b) => b[1] - a[1]).slice(0, n);
function renderCodeSidebar(side) {
  const c = S.code, T = codeTree();
  const sub = k => esc(topAuthors(T.authors.get(k)).map(([a]) => person(a)).join(' · '));
  side.innerHTML = `
    <div class="sec"><div class="sec-h"><span>Repositories</span></div><div class="rows">${c.repos.map(r => `
      <button class="row ${r.name === c.repo ? 'on' : ''}" data-repo="${esc(r.name)}"><span class="check"></span><span class="name"><b>${esc(repoLabel(r.name))}</b></span><span class="n">${r.files.toLocaleString()} files</span>
      <span class="sub">${r.all ? `${r.n} repos in one map — see how they relate` : `${r.chunks.toLocaleString()} chunks · ${plural(r.sessions, 'session')} of yours`}</span></button>`).join('') || '<div class="empty">No repositories yet — they appear once a session runs inside a git repo.</div>'}</div></div>
    ${c.focus ? `<div class="active-filters"><span class="chip">${esc(c.focus.type === 'dir' ? c.focus.key : person(c.focus.key))}<button class="x" data-unfocus>✕</button></span></div>` : ''}
    <div class="sec"><div class="sec-h"><span>Folders & owners</span></div><div class="sec-note">Click a folder to focus it on the map; expand to drill in.</div>
      <div class="tree">${T.roots.map(k => treeHtml(k, 0, T.cnt, 'cdir', T.children, sub, c.treeOpen)).join('')}</div></div>
    <div class="sec"><div class="sec-h"><span>People</span></div><div class="rows">${c.people.slice(0, 15).map(p => `
      <button class="row ${c.focus?.key === p.author ? 'on' : ''}" data-person="${esc(p.author)}"><i class="sw" style="background:${codeColorFor('author', p.author)}"></i><span class="name">${esc(person(p.author))}</span><span class="n">${(p.share * 100).toFixed(p.share < .1 ? 1 : 0)}%</span>
      <span class="sub">${esc(p.dirs.slice(0, 2).join(', '))} · active ${rel(p.last_active * 1000)}</span></button>`).join('')}</div></div>`;
  $$('[data-repo]', side).forEach(b => b.onclick = async () => { cams[camKey()] = gl.saveCam(); await loadRepo(b.dataset.repo); renderToolbar(); renderSidebar(); buildMap(); gl.fit(null); });
  bindTree(side, 'cdir', k => focusArea(k), () => renderSidebar(), c.treeOpen, null);
  $$('[data-person]', side).forEach(b => b.onclick = () => openPerson(b.dataset.person));
  $('[data-unfocus]', side)?.addEventListener('click', () => { S.code.focus = null; S.code.highlight = null; S.code.treeSel.clear(); refreshStates(); renderSidebar(); renderLabels(); });
}
const inDir = (path, dir) => dir === '.' ? !path.includes('/') : path.startsWith(dir + '/');
function focusArea(dir) {
  if (S.view === 'data') { const k = codeDirToKey(dir); if (k) toggleDataFolder(k, true); showArea(dir); return; }
  if (S.code.focus?.key === dir) { S.code.focus = null; S.code.highlight = null; S.code.treeSel.clear(); }
  else {
    S.code.focus = {type: 'dir', key: dir};
    S.code.treeSel = new Set([dir]);
    const parts = dir.split('/'); for (let i = 1; i < parts.length; i++) S.code.treeOpen.add(parts.slice(0, i).join('/'));
    S.code.highlight = new Set(S.code.points.filter(p => inDir(p.path, dir)).map(p => p.id));
  }
  refreshStates(); renderSidebar(); renderLabels();
  if (S.code.focus) setTimeout(() => gl.fit(visibleIdx()), 30);
  if (S.code.focus) showArea(dir);
}
function showArea(dir) {
  const pts = S.code.points.filter(p => inDir(p.path, dir));
  if (!pts.length) return toast(`${dir} is not in the code index.`);
  const own = new Map(); pts.forEach(p => p.author && !/\[bot\]/.test(p.author) && own.set(p.author, (own.get(p.author) || 0) + 1));
  const owners = [...own].sort((a, b) => b[1] - a[1]).slice(0, 6), tot = owners.reduce((a, x) => a + x[1], 0) || 1;
  const files = new Map(); pts.forEach(p => files.set(p.path, (files.get(p.path) || 0) + 1));
  const touched = new Set(pts.flatMap(p => p.sessions));
  openDrawer(`<div class="meta"><span class="badge">Folder</span>${esc(repoLabel(S.code.repo))} · ${plural(files.size, 'file')}</div><h2 style="font-family:var(--mono);font-size:15px">${esc(dir)}</h2>`,
    `<div class="h4" style="margin-top:4px">Who owns it</div><p class="muted" style="margin:0 0 6px;font-size:11.5px">Share of code chunks where they wrote the most lines (git blame).</p><div class="list">${owners.map(([a, n]) => `<button class="item" data-person="${esc(a)}"><div class="t"><i class="sw" style="background:${codeColorFor('author', a)}"></i><span class="grow">${esc(person(a))}</span><span class="muted">${Math.round(n / tot * 100)}%</span></div><div class="hbar" style="margin-top:4px"><i style="width:${n / tot * 100}%;background:${codeColorFor('author', a)}"></i></div></button>`).join('')}</div>
     <div class="actions"><button class="btn" data-a="who">Who knows this folder</button><button class="btn" data-a="sessions">Show my sessions here</button></div>
     ${touched.size ? `<div class="h4">Your sessions here</div><div class="list">${[...touched].map(id => S.byId.get(id)).filter(Boolean).sort((a, b) => b.t1 - a.t1).slice(0, 8).map(s => `<button class="item" data-sid="${esc(s.id)}"><div class="t"><span class="grow">${esc(s.title)}</span></div><div class="s">${rel(s.t1)}</div></button>`).join('')}</div>` : '<p class="muted" style="font-size:12px">None of your sessions have touched this folder yet.</p>'}
     <div class="h4">Largest files</div><div class="list">${[...files].sort((a, b) => b[1] - a[1]).slice(0, 12).map(([f, n]) => `<button class="item" data-file="${esc(f)}"><div class="t"><span class="grow" style="font:12px var(--mono)">${esc(dir === '.' ? f : f.slice(dir.length + 1))}</span><span class="muted">${n}</span></div></button>`).join('')}</div>`);
  const body = $('#drawer-body');
  $$('[data-person]', body).forEach(b => b.onclick = () => openPerson(b.dataset.person));
  $$('[data-sid]', body).forEach(b => b.onclick = () => openSession(b.dataset.sid));
  $$('[data-file]', body).forEach(b => b.onclick = () => focusFile(b.dataset.file));
  $('[data-a=who]', body).onclick = () => whoKnows(dir.split('/').slice(-2).join(' '));
  $('[data-a=sessions]', body).onclick = () => S.view === 'data' ? (toggleDataFolder(codeDirToKey(dir), true), setTypes('session')) : null;
  if (S.view === 'data') return;
  $('[data-a=sessions]', body).onclick = () => { S.f.projects = new Set([S.code.repo === ALL ? dir : `${S.code.repo}/${dir}`]); setView('map').then(() => { refresh(); setTimeout(() => gl.fit(visibleIdx()), 400); }); };
}
async function gotoArea(repoName, area) {
  if (S.view !== 'code') { await setView('data'); await ensureRepo(repoName); const k = repoDirKey(repoName, area); if (k) toggleDataFolder(k, true); showArea(area); return; }
  await setView('code', repoName);
  if (S.code.focus?.key === area) return showArea(area);
  focusArea(area);
}
function focusFile(path) {
  if (S.view === 'data') { const pt = dataPointFor(...codeSplit(path)); if (!pt) return toast('That file is not in the code index.'); highlightData(x => x.id === pt.id); return openItem(pt.open); }
  const idx = S.code.points.map((p, i) => p.path === path ? i : -1).filter(i => i >= 0);
  if (!idx.length) return toast('That file is not in the code index (binary, generated, or too large).');
  S.code.highlight = new Set(idx.map(i => S.code.points[i].id)); S.code.focus = {type: 'dir', key: path};
  refreshStates(); renderSidebar();
  gl.fit(idx); openCode(S.code.points[idx[0]].id);
}
const emailOf = a => ((a || '').match(/<(.*)>/) || [])[1]?.toLowerCase() || '';
function openPerson(author, {quiet = false} = {}) {
  const p = S.code.people.find(x => x.author === author) || S.code.people.find(x => emailOf(x.author) === emailOf(author))
    || S.code.people.find(x => person(x.author).toLowerCase() === person(author).toLowerCase());
  if (p) author = p.author;
  S.code.focus = {type: 'person', key: author};
  S.code.highlight = new Set(S.code.points.filter(x => x.author === author).map(x => x.id));
  if (S.view === 'data' && quiet) { /* the author filter already shows their work */ }
  else if (S.view === 'data') { const nm = person(author).toLowerCase(), rp = S.code.repo; highlightData(x => x.type === 'code' && (rp === ALL || x.repo === rp) && person(x.author || '').toLowerCase() === nm); renderSidebar(); }
  else { refreshStates(); renderSidebar(); setTimeout(() => gl.fit(visibleIdx()), 30); }
  const email = (author.match(/<(.*)>/) || [])[1] || '';
  openDrawer(`<div class="meta"><span class="badge">Person</span>${esc(repoLabel(S.code.repo))}</div><h2>${esc(person(author))}</h2><div class="meta">${esc(email)}</div>`,
    p ? `<div class="kpis" style="grid-template-columns:repeat(3,1fr)"><div class="kpi"><div class="l">Share of code</div><div class="v">${(p.share * 100).toFixed(1)}%</div></div><div class="kpi"><div class="l">Files</div><div class="v">${p.files}</div></div><div class="kpi"><div class="l">Last change</div><div class="v" style="font-size:15px">${rel(p.last_active * 1000)}</div></div></div>
     <div class="h4">Main areas</div><div class="list">${p.dirs.map(d => `<button class="item" data-dir="${esc(d)}"><div class="t"><i class="sw" style="background:${codeColorFor('dir', d)}"></i><span class="grow" style="font:12px var(--mono)">${esc(d)}</span><span class="muted">→</span></div></button>`).join('')}</div>
     <div class="actions">${email && !email.includes('noreply') ? `<a class="btn" href="mailto:${esc(email)}">✉ Email</a>` : ''}</div>` : '');
  const body = $('#drawer-body');
  body.insertAdjacentHTML('afterbegin', '<div id="pgaps"></div>');
  body.insertAdjacentHTML('beforeend', '<div id="precent"><div class="shimmer" style="height:60px;margin-top:14px;border-radius:8px"></div></div>');
  const gapRepo = S.code.repo === ALL ? (S.code.points.find(x => x.author === author)?.repo) : S.code.repo;
  loadGaps(gapRepo).then(g => {  // an author with no recent commits here: say since when, and which areas they mainly wrote
    const box = $('#pgaps'); if (!g || !box || S.code.focus?.key !== author) return;
    const nm = person(author).toLowerCase(), me = g.people.find(x => x.name.toLowerCase() === nm);
    if (!me?.departed) return;
    const areas = g.gaps.filter(x => x.departed[0]?.name.toLowerCase() === nm);
    box.innerHTML = `<p class="muted inactive-line">${esc(inactiveSince(me.last_active)[0].toUpperCase() + inactiveSince(me.last_active).slice(1))} (no commits in ${esc(gapRepo)} for ${g.inactive_days}+ days before its latest commit; they may have moved teams).</p>
      ${areas.length ? `<div class="h4" style="margin-top:8px">Main author of</div>${areas.map(x => gapItemHtml(x, gapRepo)).join('')}` : ''}`;
    bindGapLinks(box);
  });
  const repo = S.code.repo === ALL ? (S.code.points.find(x => x.author === author)?.repo || S.code.repos.find(r => !r.all)?.name) : S.code.repo;
  api(`/api/team/${enc(repo)}`).then(t => {
    const box = $('#precent'); if (!box || S.code.focus?.key !== author) return;
    const r = t.people.find(x => x.email === emailOf(author)) || t.people.find(x => x.name.toLowerCase() === person(author).toLowerCase());
    if (!r) { box.innerHTML = `<div class="h4">Last ${t.days} days</div><p class="muted" style="font-size:12px">No commits in ${esc(repo)} in the last ${t.days} days.</p>`; return; }
    const sm = t.summaries?.[r.email], wmax = Math.max(1, ...r.weeks);
    box.innerHTML = `<div class="h4">Recently · last ${t.days} days</div>
      <div class="pcard" style="padding:10px 12px">
        <div class="top"><span class="st">${plural(r.commits, 'commit')} · +${kfmt(r.added)} −${kfmt(r.deleted)} · ${plural(r.files, 'file')}</span></div>
        <div class="spark" title="Commits per week (oldest → newest)">${r.weeks.map(w => `<i class="${w ? '' : 'z'}" style="height:${w ? Math.max(12, w / wmax * 100) : 8}%"></i>`).join('')}</div>
        ${sm ? `<div class="focus">${esc(sm.focus)}</div><p class="sum">${esc(sm.summary)}</p>` : t.summarizing ? '<p class="muted" style="font-size:12px;margin:0">Summarizing what they’ve worked on…</p>' : ''}
        <div class="areas">${r.areas.map(a => `<button class="chip" data-dir="${esc(a.area)}" title="${a.commits} commits">${esc(a.area)}</button>`).join('')}</div>
      </div>
      <div class="h4">Recent commits</div><div class="list">${r.subjects.slice(0, 8).map(c => `<div class="item"><div class="t"><code style="font:11px var(--mono);color:var(--text-3)">${c.sha}</code><span class="grow">${esc(c.subject)}</span></div><div class="s">${rel(c.ts * 1000)}</div></div>`).join('')}</div>`;
    $$('[data-dir]', box).forEach(b => b.onclick = () => focusArea(b.dataset.dir));
  }).catch(() => $('#precent')?.remove());
  $$('[data-dir]', body).forEach(b => b.onclick = () => focusArea(b.dataset.dir));
}
async function openCode(id) {
  S.selected = {kind: 'code', id};
  selIdx = mapPts.findIndex(matchSel); gl.setRings(hoverIdx, selIdx);
  if (selIdx >= 0) gl.focusPoint(selIdx, 0.6);
  const c = await api('/api/code/' + id);
  if (S.view === 'data' && selIdx < 0) {  // the map has one point per file: select it by path
    const root = S.data.repos.find(r => r.name === c.repo)?.root;
    if (root) { S.selected.path = `${root}/${c.path}`; selIdx = mapPts.findIndex(matchSel); gl.setRings(hoverIdx, selIdx); if (selIdx >= 0) gl.focusPoint(selIdx, 0.6); }
  }
  const tot = Object.values(c.authors).reduce((a, b) => a + b, 0) || 1;
  const lines = c.text.split('\n');
  openDrawer(`<div class="meta"><span class="badge">Code</span>${esc(c.repo)} · lines ${c.start}–${c.end}</div><h2 style="font:600 14px var(--mono);word-break:break-all">${esc(c.path)}</h2>`,
    `<pre class="code" style="counter-reset:ln ${c.start - 1}">${lines.map(l => `<span>${esc(l) || ' '}</span>`).join('')}</pre>
     <div class="actions"><button class="btn" data-a="open" title="Open in your editor (Settings → editor)">Open in editor</button><button class="btn" data-a="reveal">Reveal in Finder</button><button class="btn" data-a="file">Show whole file</button><button class="btn" data-a="area">Area: ${esc(areaOf(c.path))}</button></div>
     <div class="h4">Written by</div><p class="muted" style="font-size:11.5px;margin:-2px 0 6px">From git blame: who last changed each line.</p><div class="list">${Object.entries(c.authors).sort((a, b) => b[1] - a[1]).map(([a, n]) => `<button class="item" data-person="${esc(a)}"><div class="t"><i class="sw" style="background:${codeColorFor('author', a)}"></i><span class="grow">${esc(person(a))}</span><span class="muted">${n} lines</span></div><div class="hbar" style="margin-top:4px"><i style="width:${n / tot * 100}%;background:${codeColorFor('author', a)}"></i></div></button>`).join('')}</div>
     ${c.commits.length ? `<div class="h4">Recent commits to this file</div><div class="list">${c.commits.map(x => `<div class="item"><div class="t"><code style="font:11px var(--mono);color:var(--text-3)">${x.sha}</code><span class="grow">${esc(x.subject)}</span></div><div class="s">${esc(x.author)} · ${rel(x.ts * 1000)}</div></div>`).join('')}</div>` : ''}
     ${c.sessions.length ? `<div class="h4">Your sessions that touched this file</div><div class="list">${c.sessions.map(s => `<button class="item" data-sid="${esc(s.id)}"><div class="t">${s.edited ? '<span class="badge">edited</span>' : '<span class="badge">read</span>'}<span class="grow">${esc(s.title)}</span><span class="muted">→</span></div></button>`).join('')}</div>` : ''}`);
  const body = $('#drawer-body');
  $$('[data-person]', body).forEach(b => b.onclick = async () => { if (S.view === 'data') await ensureRepo(c.repo); openPerson(b.dataset.person); });
  $$('[data-sid]', body).forEach(b => b.onclick = () => openSession(b.dataset.sid));
  $('[data-a=open]', body).onclick = () => openOnMac('code:' + id, 'open');
  $('[data-a=reveal]', body).onclick = () => openOnMac('code:' + id, 'reveal');
  const full = S.code.repo === ALL ? `${c.repo}/${c.path}` : c.path;
  if (S.view === 'data') {
    const dir = c.path.includes('/') ? c.path.slice(0, c.path.lastIndexOf('/')) : '';
    $('[data-a=file]', body).onclick = () => highlightData(p => p.type === 'code' && p.repo === c.repo && p.path.endsWith('/' + c.path));
    $('[data-a=area]', body).onclick = async () => { await ensureRepo(c.repo); const k = repoDirKey(c.repo, dir); if (k) toggleDataFolder(k, true); };
  } else {
    $('[data-a=file]', body).onclick = () => focusFile(full);
    $('[data-a=area]', body).onclick = () => focusArea(areaOf(full, S.code.repo === ALL ? 3 : 2));
  }
}
async function gotoCode(repoRoot, path, author) {
  const name = (S.code.repos.find(r => r.root === repoRoot || r.name === repoRoot) || {}).name || repoRoot.split('/').pop();
  if (S.view !== 'code') {
    await setView('data'); await ensureRepo(name);
    if (path) { const pt = S.data.points.find(p => p.type === 'code' && p.repo === name && (p.path === path || p.path.endsWith('/' + path))); if (pt) { highlightData(x => x.id === pt.id); openItem(pt.open); } else toast('That file is not in the code index.'); }
    if (author) openPerson(author);
    return;
  }
  await setView('code', name);
  if (path) {
    const r = S.code.repos.find(x => x.name === name);
    const rel_ = r && path.startsWith(r.root + '/') ? path.slice(r.root.length + 1) : path;
    const hit = S.code.points.find(p => p.path === rel_) || S.code.points.find(p => path.endsWith('/' + p.path));
    if (hit) focusFile(hit.path); else toast('That file is not in the code index.');
  }
  if (author) openPerson(author);
}
async function showOverlaps() {
  openDrawer(`<div class="meta"><span class="badge">Analysis</span>${esc(repoLabel(S.code.repo))}</div><h2>Conceptual overlap</h2>`, '<div class="empty"><div class="spinner" style="margin:auto"></div></div>');
  const o = await api(`/api/repo/${enc(S.code.repo)}/overlaps`);
  openDrawer(`<div class="meta"><span class="badge">Analysis</span>${esc(repoLabel(S.code.repo))}</div><h2>Conceptual overlap</h2>`,
    `<p class="muted">Pairs of areas whose code means similar things but lives in different places — candidates for shared abstractions, duplicated logic, or people who should talk.</p>
     <div class="list">${o.map(x => `<div class="item"><div class="t"><span class="grow" style="font:12px var(--mono)"><a href="#" data-dir="${esc(x.a)}">${esc(x.a)}</a> ↔ <a href="#" data-dir="${esc(x.b)}">${esc(x.b)}</a></span><span class="muted">${Math.round(x.sim * 100)}%</span></div>
       <div class="s">${x.owners_a.map(person).map(esc).join(', ')} · ${x.owners_b.map(person).map(esc).join(', ')}</div></div>`).join('') || '<div class="empty">Not enough code yet.</div>'}</div>`);
  $$('[data-dir]', $('#drawer-body')).forEach(a => a.onclick = e => { e.preventDefault(); focusArea(a.dataset.dir); });
}
async function whoKnows(q) {
  if (S.view !== 'data') await setView('data');
  const fr = focusedRepo(); if (fr) await ensureRepo(fr.name); else if (S.code.repo !== ALL && S.code.repos.some(r => r.name === ALL)) await ensureRepo(ALL);
  openDrawer(`<div class="meta"><span class="badge">Who knows</span>${esc(repoLabel(S.code.repo))}</div><h2>${esc(q)}</h2>`, '<div class="empty"><div class="spinner" style="margin:auto"></div></div>');
  const r = await api(`/api/experts?q=${enc(q)}&repo=${enc(S.code.repo)}`);
  S.code.highlight = new Set(r.hit_ids); S.code.focus = null;
  highlightData(p => p.type === 'code' && r.chunks.some(c => p.repo === c.repo && p.path.endsWith('/' + c.path)));
  openDrawer(`<div class="meta"><span class="badge">Who knows</span>${esc(repoLabel(S.code.repo))}</div><h2>${esc(q)}</h2>`,
    `<p class="muted">Ranked by how much of the most relevant code each person wrote (git blame, weighted by relevance).</p>
     <div class="list">${r.experts.map((e, i) => `<button class="item" data-person="${esc(e.author)}"><div class="t"><b style="width:18px;color:var(--text-3)">${i + 1}</b><span class="grow">${esc(person(e.author))}</span><span class="muted">${Math.round(e.share * 100)}% · ${rel(e.last_active * 1000)}</span></div><div class="hbar" style="margin:4px 0 0 24px"><i style="width:${e.share * 100}%"></i></div><div class="s" style="margin-left:24px">${esc(e.files.slice(0, 3).join(' · '))}</div></button>`).join('') || '<div class="empty">No matching code.</div>'}</div>
     <div class="h4">Most relevant code</div><div class="list">${r.chunks.map(c => `<button class="item" data-code="${c.id}"><div class="t"><span class="grow" style="font:12px var(--mono)">${esc(c.path)}:${c.start}</span><span class="muted">${Math.round(c.score * 100)}%</span></div></button>`).join('')}</div>`);
  const body = $('#drawer-body');
  $$('[data-person]', body).forEach(b => b.onclick = () => openPerson(b.dataset.person));
  $$('[data-code]', body).forEach(b => b.onclick = () => openCode(+b.dataset.code));
}

/* ============================== data view: one map of documents, code and agent sessions ============================== */
const TYPE_SLOT = {doc: 0, code: 1, session: 2};
const TYPE_NAME = {doc: 'Documents', code: 'Code', session: 'Agent sessions'};
const TYPE_ONE = {doc: 'Document', code: 'Code', session: 'Agent session'};
S.data = {points: [], repos: [], home: '', types: new Set(['doc', 'code', 'session']), folders: new Set(), colorBy: store.get('dataColor', 'type'),
          treeOpen: new Set(store.get('dataTreeOpen', [])), tree: null, highlight: null, loaded: false, stale: false, repoKeys: new Map(),
          topics: new Map(), topicsAI: false, sel: null, selStack: [], search: null, ctx: null, view: null, rev: '', authors: new Set(), drawerPerson: null};  // sel: the map label you zoomed into {kind, key, cam, folders}
const relHome = p => { const h = S.data.home; return h && p.startsWith(h + '/') ? p.slice(h.length + 1) : p.replace(/^\//, ''); };
const ptTime = p => typeof p.mtime === 'number' ? p.mtime * 1000 : T(p.mtime);
// A repo, or a folder two levels under home (~/Documents/medrec): "Desktop" alone would color nearly everything the same.
// What's in view on the Data map: the type and folder filters, then any highlight (a topic, a person…) and search
// results. Folder and author colors go to the biggest groups in view, so a repo shows its own people and areas.
const dataInView = p => dataPass(p) && (!S.data.highlight || S.data.highlight.has(p.id)) && (!S.data.search || S.data.search.has(p.id));
function dataContextSlots() {
  const D = S.data, inView = D.points.filter(dataInView), ctx = inView.length ? inView : D.points.filter(p => dataPass(p));
  const top = f => { const m = new Map(); ctx.forEach(p => { const k = f(p); if (k) m.set(k, (m.get(k) || 0) + 1); }); return new Map([...m].sort((a, b) => b[1] - a[1]).slice(0, 7).map(([k], i) => [k, i])); };
  const fs = top(p => p.place), as = top(p => p.who), same = (a, b) => a.size === b.size && [...a].every(([k, v]) => b.get(k) === v);
  const ts = ctx.map(p => p.t).filter(x => !isNaN(x)).sort((a, b) => a - b);
  const tr = [ts[Math.floor(ts.length * .05)] || 0, ts[ts.length - 1] || 1];
  const changed = !same(fs, D.folderSlot) || !same(as, D.authorSlot) || (D.colorBy === 'recency' && (tr[0] !== D.tRange?.[0] || tr[1] !== D.tRange?.[1]));
  D.ctx = ctx; D.folderSlot = fs; D.authorSlot = as; D.tRange = tr;
  return changed;
}
// search results light up on the map (the rest dims), and narrow the legend
function setDataSearch(results) {
  const D = S.data;
  if (!results) { if (!D.search) return; D.search = null; }
  else {
    const byPath = new Map(D.points.filter(p => p.type !== 'session').map(p => [p.path, p.id])), ids = new Set();
    results.forEach(r => { if (r.is_dir) return; const id = r.kind === 'session' ? r.id : byPath.get(r.path); if (id && (r.kind !== 'session' || D.points.some(p => p.id === id))) ids.add(id); });
    D.search = ids.size ? ids : null;
  }
  if (S.view === 'data') { refreshStates(); renderLabels(); }
}
const placeOf = dir => { const r = repoOfKey(dir); return r ? r.key : dir.split('/').slice(0, 2).join('/'); };
const placeName = k => { const r = repoOfKey(k); return r && r.key === k ? r.name : '~/' + k; };
// One color per person: git and document metadata spell the same person several ways ("Ted", "TedHaley", "Ted Haley",
// "tedhaley-affinity"). Two spellings are merged when one is a leading run of the other's name parts; a lone first name
// is merged only when it matches exactly one person. Bots and agents get no color of their own.
function canonAuthors(points) {
  const n = new Map(); points.forEach(p => { if (p.author && p.author_source !== 'agent' && !/\[bot\]|\bbot$/i.test(p.author)) n.set(p.author, (n.get(p.author) || 0) + 1); });
  const names = [...n.keys()], toks = new Map(names.map(a => [a, a.replace(/([a-z])([A-Z])/g, '$1 $2').toLowerCase().split(/[^a-z0-9]+/).filter(Boolean)]));
  const flat = a => toks.get(a).join(''), heads = a => toks.get(a).map((_, i, t) => t.slice(0, i + 1).join(''));
  const up = new Map(names.map(a => [a, a])), find = a => up.get(a) === a ? a : find(up.get(a)), join = (a, b) => up.set(find(a), find(b));
  const lone = [];
  for (const a of names) {
    if (flat(a).length < 3) continue;
    if (toks.get(a).length < 2) { lone.push(a); continue; }
    for (const b of names) if (b !== a && toks.get(b).length >= 2 && heads(b).includes(flat(a))) join(a, b);
  }
  for (const a of lone) { const m = new Set(names.filter(b => b !== a && heads(b).includes(flat(a))).map(find)); if (m.size === 1) join(a, [...m][0]); }
  const groups = new Map(); names.forEach(a => { const r = find(a); if (!groups.has(r)) groups.set(r, []); groups.get(r).push(a); });
  const out = new Map();
  for (const g of groups.values()) {  // shown as the spelling that looks most like a full name, then the most used
    const best = [...g].sort((x, y) => (/\s/.test(y) - /\s/.test(x)) || (n.get(y) - n.get(x)))[0];
    g.forEach(a => out.set(a, best));
  }
  return out;
}
async function loadDataTopics() {
  const r = await api('/api/data/topics').catch(() => null);
  S.data.topics = new Map((r?.topics || []).map(t => [t.id, t])); S.data.topicsAI = !!r?.ai; S.data.topicsPaused = r?.ai_paused || null;
  if (S.data.view) refreshViewNames();
}
// Topics follow what's on screen: the items that pass the filters and sit inside the camera's view are re-clustered on
// the server, so zooming into an area (or into a topic) brings out its finer topics, and a filter re-finds topics among
// what's left (the whole map too: until the first answer, the map-wide topics stand in). A new topic keeps the
// color of the old one it mostly came from; items outside the view take the topic of the nearest one on screen.
const dataTopicOf = p => { const v = S.data.view; return v ? v.of.get(p.i) : p.cluster; };
const dataTopics = () => S.data.view ? S.data.view.topics : S.data.topics;
const topicSlot = id => id == null ? -1 : S.data.view ? (S.data.view.slot.get(id) ?? -1) : id;
const jaccard = (a, b) => { if (!a.size && !b.size) return 1; let n = 0; for (const x of a) if (b.has(x)) n++; return n / (a.size + b.size - n); };
const VT = {prev: new Set(), still: 0, seq: 0, busy: false};
function onScreen() {
  const D = S.data, cam = gl.camera, v = new THREE.Vector3(), out = new Set();
  cam.updateMatrixWorld();
  D.points.forEach((p, i) => {
    if (!dataInView(p)) return;
    v.set(p.p[0], p.p[1], p.p[2]).project(cam);
    if (v.z < 1 && v.x > -1 && v.x < 1 && v.y > -1 && v.y < 1) out.add(i);
  });
  return out;
}
function viewTopicsTick() {
  const D = S.data;
  if (S.view !== 'data' || !D.loaded || document.hidden || VT.busy) return;
  const cur = onScreen(), now = performance.now();
  if (jaccard(cur, VT.prev) < 0.95) { VT.prev = cur; VT.still = now; return; }  // still moving: wait for it to settle
  if (cur.size < 10) return;  // too few to cluster: keep the topics you zoomed in from
  if (D.view && jaccard(cur, D.view.basis) >= 0.8) {
    if (!D.view.nameAsked && now - VT.still > 6000) { D.view.nameAsked = true; fetchViewTopics([...D.view.basis], true); }  // settled: ask for written names
    return;
  }
  fetchViewTopics([...cur].sort((a, b) => a - b), false);
}
async function fetchViewTopics(idx, name) {
  const D = S.data, seq = ++VT.seq;
  VT.busy = !name;
  let r;
  try {
    const res = await fetch('/api/data/view_topics', {method: 'POST', headers: {'content-type': 'application/json'}, body: JSON.stringify({rev: D.rev, idx, name})});
    if (res.status === 409) { VT.busy = false; reloadDataSoon(); return; }
    if (!res.ok) throw new Error(res.status);
    r = await res.json();
  } catch { VT.busy = false; return; }
  VT.busy = false;
  if (seq !== VT.seq || !r.topics.length) return;  // a newer request went out, or too few items to cluster
  const of = new Map(Object.entries(r.of).map(([i, t]) => [+i, t])), topics = new Map(r.topics.map(t => [t.id, t]));
  if (name && D.view) {  // same clustering, now with names coming: keep colors
    D.view.topics = topics; D.view.nameAsked = true; renderLabels(); renderLegend(); return;
  }
  // colors carry over: each new topic takes the slot most of its items had, biggest overlaps first
  const pairs = new Map();
  of.forEach((t, i) => { const old = topicSlot(dataTopicOf(D.points[i])); if (old >= 0) { const k = t + ':' + old; pairs.set(k, (pairs.get(k) || 0) + 1); } });
  const slot = new Map(), used = new Set();
  [...pairs].sort((a, b) => b[1] - a[1]).forEach(([k]) => { const [t, o] = k.split(':').map(Number); if (!slot.has(t) && !used.has(o)) { slot.set(t, o); used.add(o); } });
  const size = new Map(); of.forEach(t => size.set(t, (size.get(t) || 0) + 1));
  [...topics.keys()].sort((a, b) => (size.get(b) || 0) - (size.get(a) || 0)).forEach(t => { if (!slot.has(t)) { let o = 0; while (used.has(o)) o++; slot.set(t, o); used.add(o); } });
  // filtered items off screen join the topic whose on-screen items are nearest in the map
  const cen = new Map(); of.forEach((t, i) => { const c = cen.get(t) || [0, 0, 0, 0], q = D.points[i].p; c[0] += q[0]; c[1] += q[1]; c[2] += q[2]; c[3]++; cen.set(t, c); });
  const cs = [...cen].map(([t, c]) => [t, c[0] / c[3], c[1] / c[3], c[2] / c[3]]);
  D.points.forEach((p, i) => {
    if (of.has(i) || !dataPass(p)) return;
    let best = null, bd = Infinity; for (const [t, x, y, z] of cs) { const d = (p.p[0] - x) ** 2 + (p.p[1] - y) ** 2 + (p.p[2] - z) ** 2; if (d < bd) { bd = d; best = t; } }
    if (best != null) of.set(i, best);
  });
  D.view = {topics, of, slot, basis: new Set(idx), nameAsked: name};
  VT.prev = new Set(idx);
  afterTopicsChange();
}
function afterTopicsChange() {
  if (S.view !== 'data') return;
  recolor(); renderLabels(); renderLegend();
  if (S.data.drawerPerson) openPersonPanel(S.data.drawerPerson, {keep: true});
}
// written names arrive in the background: show them, and update a zoomed-into topic that has one now
async function refreshViewNames() {
  const D = S.data; if (!D.view) return;
  const r = await fetch('/api/data/view_topics', {method: 'POST', headers: {'content-type': 'application/json'}, body: JSON.stringify({rev: D.rev, idx: [...D.view.basis], name: false})}).then(x => x.ok ? x.json() : null).catch(() => null);
  if (!r || !D.view) return;
  const bySig = new Map(r.topics.map(t => [t.sig, t]));
  D.view.topics = new Map(r.topics.map(t => [t.id, t]));
  D.selStack.forEach(e => { const t = e.topic && bySig.get(e.topic.sig); if (t?.named) { e.topic = t; e.name = t.name; } });
  renderLabels(); renderLegend();
  if (D.drawerTopic && D.drawerTopic === D.sel) openTopic(D.sel);
}
setInterval(viewTopicsTick, 700);
const loadScopedTopics = () => {};  // topics now follow the view (viewTopicsTick)
async function loadData() {
  let r = {points: [], repos: [], home: ''};
  try { r = await api('/api/data/points'); } catch {}
  await loadDataTopics();
  const D = S.data;
  D.home = r.home || ''; D.repos = r.repos || []; D.rev = r.rev || ''; D.view = null;
  D.repoKeys = new Map(D.repos.map(x => [relHome(x.root), x.name]));  // '~'-relative folder key -> repo name
  D.points = r.points.map(p => {
    const rel = relHome(p.path), dir = rel.includes('/') ? rel.slice(0, rel.lastIndexOf('/')) : '';
    return {...p, rel, dir, top: rel.split('/')[0], group: p.repo || rel.split('/')[0], t: ptTime(p)};
  });
  D.points.forEach((p, i) => { p.i = i;
  });
  const cnt = (f) => { const m = new Map(); D.points.forEach(p => { const k = f(p); if (k) m.set(k, (m.get(k) || 0) + 1); }); return new Map([...m].sort((a, b) => b[1] - a[1]).slice(0, 7).map(([k], i) => [k, i])); };
  const who = canonAuthors(D.points); D.whoMap = who;
  D.points.forEach(p => { p.who = who.get(p.author) || null; p.place = placeOf(p.dir); });
  D.folderSlot = cnt(p => p.place); D.authorSlot = cnt(p => p.who);
  const ts = D.points.map(p => p.t).filter(x => !isNaN(x)).sort((a, b) => a - b);
  D.tRange = [ts[Math.floor(ts.length * .05)] || 0, ts[ts.length - 1] || 1];
  D.tree = null; D.loaded = true; D.stale = false;
}
function dataColor(p) {
  const D = S.data;
  switch (D.colorBy) {
    case 'type': return slotColor(TYPE_SLOT[p.type]);
    case 'folder': return slotColor(D.folderSlot.has(p.place) ? D.folderSlot.get(p.place) : -1);
    case 'author': return slotColor(p.who && D.authorSlot.has(p.who) ? D.authorSlot.get(p.who) : -1);
    case 'topic': return slotColor(topicSlot(dataTopicOf(p)));
    case 'recency': { const [a, b] = D.tRange; return seqColor(isNaN(p.t) ? 0 : Math.max(0, Math.min(1, (p.t - a) / Math.max(1, b - a)))); }
    case 'risk': { const r = riskOf(p); return r == null ? pal().other : seqColor(r); }
  }
  return pal().other;
}
function dataPass(p, skip) {
  const D = S.data;
  if (skip !== 'types' && !D.types.has(p.type)) return false;
  if (skip !== 'authors' && D.authors.size && !D.authors.has(p.who)) return false;
  if (skip !== 'folders' && D.folders.size && ![...D.folders].some(k => p.rel.startsWith(k + '/'))) return false;
  return true;
}
function dataTree() {
  const D = S.data;
  if (D.tree?.n === D.points.length) return D.tree;
  const children = new Map(), seen = new Set();
  for (const p of D.points) {
    let key = '';
    for (const part of p.dir.split('/').filter(Boolean)) {
      const k = key ? key + '/' + part : part;
      if (!seen.has(k)) { seen.add(k); if (key) { if (!children.has(key)) children.set(key, []); children.get(key).push(k); } }
      key = k;
    }
  }
  return (D.tree = {n: D.points.length, children, roots: [...seen].filter(k => !k.includes('/'))});
}
// Map labels follow the folder tree: start at the folder filter (or home), step through folders that hold just one
// subfolder, always open home-level containers (Desktop, Documents…) since their names say little, then keep splitting
// the biggest group (a repo into its areas, a folder into its subfolders) until there are enough labels.
const tail = k => { const p = k.split('/'); return p.length > 2 ? '…/' + p.slice(-2).join('/') : k; };
const repoOfKey = k => { for (const [rk, name] of S.data.repoKeys) if (k === rk || k.startsWith(rk + '/')) return {key: rk, name}; return null; };
function dataLabelGroups(budget = 22) {
  const D = S.data, vis = [];
  mapPts.forEach((p, i) => { if (dataPass(p)) vis.push(i); });
  const starts = D.folders.size ? [...D.folders] : [''];
  let groups = starts.map(k => ({key: k, idx: vis.filter(i => !k || mapPts[i].dir === k || mapPts[i].dir.startsWith(k + '/'))})).filter(g => g.idx.length);
  const split = g => {  // [loose group?, ...one group per child folder], or null when there's nothing below
    const kids = new Map(), loose = [], depth = g.key ? g.key.split('/').length : 0;
    g.idx.forEach(i => { const parts = mapPts[i].dir.split('/').filter(Boolean); if (parts.length <= depth) loose.push(i); else { const k = parts.slice(0, depth + 1).join('/'); if (!kids.has(k)) kids.set(k, []); kids.get(k).push(i); } });
    if (!kids.size) return null;
    return [...(loose.length ? [{key: g.key, idx: loose, leaf: true}] : []), ...[...kids].map(([key, idx]) => ({key, idx}))];
  };
  const sized = (gs, f) => gs.filter(g => g.idx.length >= Math.max(2, vis.length / budget / f)).length;  // groups big enough to compete for a label
  const must = g => !g.leaf && (!g.key || (!g.key.includes('/') && !D.repoKeys.has(g.key)));  // home and its top-level containers
  for (let guard = 0; guard < 400; guard++) {
    let i = groups.findIndex(g => !g.leaf && !g.done && (must(g) || split(g)?.length === 1));
    if (i < 0) {
      const cand = groups.map((g, j) => [g, j]).filter(([g]) => !g.leaf && !g.done && g.idx.length > vis.length / 6).sort((a, b) => b[0].idx.length - a[0].idx.length);
      if (!cand.length || sized(groups, 1) >= budget) break;  // only dominant groups split, so labels stay at the project level until you zoom in
      i = cand[0][1];
    }
    const parts = split(groups[i]);
    if (!parts) { groups[i].leaf = true; continue; }
    if (!must(groups[i]) && parts.length > 1 && (sized(parts, 4) - 1 + sized(groups, 4) > budget * 2 ||  // would flood the map
        Math.max(...parts.map(x => x.idx.length)) < Math.max(4, vis.length / budget))) { groups[i].done = true; continue; }  // or shatter into crumbs
    groups.splice(i, 1, ...parts);
  }
  return groups.filter(g => g.idx.length >= 2 || D.repoKeys.has(g.key)).sort((a, b) => b.idx.length - a.idx.length).slice(0, budget);
}
// anchor a label where its group is densest, not at a centroid that can fall between two far-apart clumps
function dataAnchor(idx) {
  if (idx.length < 4) return centroid(idx);
  const P = i => mapPts[i].p, d2 = (a, b) => (a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2;
  const step = Math.max(1, Math.floor(idx.length / 300)), sample = idx.filter((_, k) => k % step === 0);
  const c = centroid(idx), r2 = (sample.map(i => d2(P(i), c)).sort((a, b) => a - b)[Math.floor(sample.length / 2)] || 1e-4) * 0.25;
  let best = sample[0], bestN = -1;
  sample.forEach(i => { let n = 0; for (const j of sample) if (d2(P(i), P(j)) < r2) n++; if (n > bestN) { bestN = n; best = i; } });
  return centroid(idx.filter(j => d2(P(best), P(j)) < r2));
}
// Clicking a map label zooms into it: a topic is highlighted, explained, and broken into its own finer topics; a
// folder is filtered to. Each click goes one level deeper; clicking the label you're in steps back out one level.
const clearSel = () => { S.data.selStack = []; S.data.sel = null; };
let selSeq = 0;
function selectLabel(kind, key) {
  const D = S.data;
  if (D.sel?.kind === kind && D.sel.key === key) return deselectLabel();
  const entry = {kind, key, cam: gl.saveCam(), folders: new Set(D.folders), highlight: D.highlight};
  if (kind === 'folder') {
    const stack = D.selStack;
    toggleDataFolder(key, true);  // filters and fits (and clears the selection, restored here)
    D.selStack = [...stack, entry]; D.sel = entry;
  } else {
    const t = dataTopics().get(key) || {name: `Topic ${key + 1}`, keywords: '', description: '', reps: []};
    const members = new Set(D.points.filter(p => dataTopicOf(p) === key && dataInView(p)).map(p => p.id));
    Object.assign(entry, {key: 't' + (++selSeq), topic: t, name: t.name, slot: topicSlot(key), members});
    D.selStack.push(entry); D.sel = entry;
    D.highlight = members;
    refreshStates(); renderToolbar(); setTimeout(() => gl.fit(visibleIdx()), 30);
    openTopic(entry);
  }
  renderLabels();
}
function deselectLabel() {
  const D = S.data, s = D.selStack.pop(); if (!s) return;
  D.sel = D.selStack.at(-1) || null;
  D.highlight = s.highlight;
  const sameFolders = s.folders.size === D.folders.size && [...s.folders].every(f => D.folders.has(f));
  if (!sameFolders) { const stack = D.selStack, sel = D.sel; D.folders = new Set(s.folders); dataChanged(false); D.selStack = stack; D.sel = sel; D.highlight = s.highlight; refreshStates(); renderLabels(); }
  else { refreshStates(); renderToolbar(); renderLabels(); }
  if (D.drawerTopic) { if (D.sel?.kind === 'topic') openTopic(D.sel); else closeDrawer(); }
  const dir = s.cam.p.clone().sub(s.cam.t), dist = dir.length();
  gl.flyTo(s.cam.t.clone(), dist, dir.normalize());
}
function openTopic(entry) {
  const D = S.data, t = entry.topic, slot = entry.slot;
  const mem = D.points.filter(p => entry.members.has(p.id)), byId = new Map(mem.map(p => [p.id, p]));
  const tc = {}; mem.forEach(p => tc[p.type] = (tc[p.type] || 0) + 1);
  const where = new Map();  // repo, else the folder two levels under home
  mem.forEach(p => { const r = repoOfKey(p.dir), k = r ? r.key : p.dir.split('/').slice(0, 2).join('/'); where.set(k, (where.get(k) || 0) + 1); });
  const places = [...where].sort((a, b) => b[1] - a[1]).slice(0, 5);
  const authors = new Map(); mem.forEach(p => { if (p.who) authors.set(p.who, (authors.get(p.who) || 0) + 1); });
  const ts = mem.map(p => p.t).filter(x => !isNaN(x)).sort((a, b) => b - a);
  const central = t.reps.map(i => byId.get(i)).filter(Boolean).slice(0, 6);
  const recent = [...mem].filter(p => !isNaN(p.t)).sort((a, b) => b.t - a.t).filter(p => !central.includes(p)).slice(0, 5);
  const itemRow = p => `<button class="item" data-open="${esc(p.open)}"><div class="t"><i class="sw" style="background:${slotColor(TYPE_SLOT[p.type])}"></i><span class="grow">${esc(p.title)}</span>${isNaN(p.t) ? '' : `<span class="muted">${rel(p.t)}</span>`}</div><div class="s">${esc(p.type === 'session' ? (p.repo ? 'in ' + p.repo : p.display) : p.display.replace(/\/[^/]*$/, ''))}</div></button>`;
  openDrawer(`<div class="meta"><span class="badge"><i class="sw" style="background:${slotColor(slot)}"></i>Topic</span>${plural(mem.length, 'item')}${D.selStack.filter(e => e.kind === 'topic').length > 1 ? ` · inside ${esc(D.selStack.filter(e => e.kind === 'topic').slice(0, -1).map(e => e.name).join(' › '))}` : ''}</div><h2>${esc(t.name)}</h2>`,
    `${t.description ? `<p>${esc(t.description)}</p>` : `<p class="muted">${D.topicsAI ? (D.topicsPaused ? `Named from its most distinctive words for now. A written name and description come when background AI resumes (${esc(D.topicsPaused.toLowerCase())}).` : 'A written description is on its way; named from its most distinctive words for now.') : 'Named from its most distinctive words. Choose an AI engine in Settings for written names and descriptions.'}</p>`}
     <div class="kpis" style="grid-template-columns:repeat(3,1fr)">${Object.keys(TYPE_SLOT).map(k => `<div class="kpi"><div class="l">${TYPE_NAME[k]}</div><div class="v">${(tc[k] || 0).toLocaleString()}</div></div>`).join('')}</div>
     ${ts.length ? `<p class="muted" style="font-size:12px;margin:8px 0 0">Last changed ${rel(ts[0])}${ts.length > 1 ? ` · oldest ${rel(ts[ts.length - 1])}` : ''}</p>` : ''}
     <div class="h4">Where it lives</div><div class="list">${places.map(([k, n]) => { const r = repoOfKey(k); return `<button class="item" data-place="${esc(k)}"><div class="t"><span class="grow">${esc(r ? r.name : '~/' + k)}${r ? '<span class="cbadge">code</span>' : ''}</span><span class="muted">${Math.round(n / mem.length * 100)}%</span></div></button>`; }).join('')}</div>
     ${t.keywords ? `<div class="h4">Distinctive words</div><div class="chips">${t.keywords.split(',').map(w => `<span class="chip">${esc(w.trim())}</span>`).join('')}</div>` : ''}
     ${authors.size ? `<div class="h4">Who wrote it</div><div class="chips">${[...authors].sort((a, b) => b[1] - a[1]).slice(0, 5).map(([a, n]) => `<span class="chip">${esc(person(a))}<span class="n">${n}</span></span>`).join('')}</div>` : ''}
     ${central.length ? `<div class="h4">Most typical</div><div class="list">${central.map(itemRow).join('')}</div>` : ''}
     ${recent.length ? `<div class="h4">Recently changed</div><div class="list">${recent.map(itemRow).join('')}</div>` : ''}
     <p class="muted" style="font-size:12px;margin:12px 0 0">The map now shows the finer topics inside this one; click a label to go deeper.</p>
     <div class="actions"><button class="btn" data-zoomout>Zoom back out</button></div>`);
  D.drawerTopic = entry;
  const body = $('#drawer-body');
  $$('[data-open]', body).forEach(b => b.onclick = () => openItem(b.dataset.open));
  $$('[data-place]', body).forEach(b => b.onclick = () => selectLabel('folder', b.dataset.place));
  $('[data-zoomout]', body).onclick = () => deselectLabel();
}
// the repo (name, folder key) that the current folder filter is in, if exactly one
function focusedRepo() {
  const D = S.data; if (D.folders.size !== 1) return null;
  const f = [...D.folders][0];
  for (const [k, name] of D.repoKeys) if (f === k || f.startsWith(k + '/')) return {name, key: k, root: D.repos.find(r => r.name === name)?.root, sub: f === k ? '' : f.slice(k.length + 1)};
  return null;
}
function renderDataSidebar(side) {
  const D = S.data, T = dataTree(), all = D.points, vis = all.filter(p => dataPass(p));
  const cnt = new Map();
  all.filter(p => dataPass(p, 'folders')).forEach(p => { const parts = p.dir.split('/').filter(Boolean); for (let i = 1; i <= parts.length; i++) { const k = parts.slice(0, i).join('/'); cnt.set(k, (cnt.get(k) || 0) + 1); } });
  const tc = {}; all.filter(p => dataPass(p, 'types')).forEach(p => tc[p.type] = (tc[p.type] || 0) + 1);
  const people = dataPeople();
  side.innerHTML = `
    <div class="sec"><div class="chips type-chips" role="group" aria-label="Types">${Object.keys(TYPE_SLOT).map(t => `<button class="chip ${D.types.has(t) ? 'on' : ''}" data-type="${t}" aria-pressed="${D.types.has(t)}"><i class="sw" style="background:${slotColor(TYPE_SLOT[t])}"></i>${TYPE_NAME[t]}<span class="n">${(tc[t] || 0).toLocaleString()}</span></button>`).join('')}</div>
      <div class="sec-note" style="margin-top:8px">${vis.length.toLocaleString()} of ${plural(all.length, 'item')}${D.folders.size ? ` in <b>${esc([...D.folders].join(', '))}</b> <button class="linkbtn" data-dclearf>Clear</button>` : ''}</div></div>
    <div class="sec"><div class="sec-h"><span>People</span>${D.authors.size ? `<button class="linkbtn" data-dclearp>Clear</button>` : ''}</div>
      ${people.length ? `<div class="rows">${people.map(x => `<div class="prow-w ${D.drawerPerson === x.who ? 'open' : ''}"><button class="row prow ${D.authors.has(x.who) ? 'on' : ''}" data-who="${esc(x.who)}" aria-pressed="${D.authors.has(x.who)}" title="${D.authors.has(x.who) ? 'Click again to stop filtering by ' + esc(x.who) : 'Show only work by ' + esc(x.who) + ' (pick several to compare)'}"><span class="check"></span><span class="sw" style="background:${slotColor(D.authorSlot.get(x.who) ?? -1)}"></span><span class="name">${esc(x.who)}</span><span class="n">${x.n.toLocaleString()}</span></button><button class="parrow" data-pwho="${esc(x.who)}" title="What ${esc(x.who)} knows about, in what's shown" aria-label="About ${esc(x.who)}"><svg viewBox="0 0 24 24" width="14" height="14"><path d="m9.5 6 6 6-6 6" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg></button></div>`).join('')}</div>` : '<div class="sec-note">No authors known for what is shown.</div>'}</div>
    <div class="sec"><div class="sec-h"><span>Folders</span></div>
      <div class="tree">${T.roots.filter(k => cnt.get(k)).sort((a, b) => cnt.get(b) - cnt.get(a)).map(k => treeHtml(k, 0, cnt, 'ddir', T.children, null, D.treeOpen)).join('') || '<div class="empty" style="padding:8px">Nothing indexed yet</div>'}</div></div>`;
  $$('[data-type]', side).forEach(b => b.onclick = () => { const t = b.dataset.type; if (D.types.has(t) && D.types.size === 1) D.types = new Set(Object.keys(TYPE_SLOT)); else toggleSet(D.types, t); dataChanged(); });
  bindTree(side, 'ddir', k => toggleDataFolder(k), () => renderSidebar(), D.treeOpen, 'dataTreeOpen');
  $('[data-dclearf]', side)?.addEventListener('click', () => { D.folders.clear(); dataChanged(); });
  $$('[data-who]', side).forEach(b => b.onclick = () => toggleAuthor(b.dataset.who));
  $$('[data-pwho]', side).forEach(b => b.onclick = () => {
    if (D.drawerPerson === b.dataset.pwho) { closeDrawer(); renderSidebar(); } else openPersonPanel(b.dataset.pwho);
  });
  $('[data-dclearp]', side)?.addEventListener('click', () => { D.authors.clear(); if (D.drawerPerson) closeDrawer(); dataChanged(); });
}
// People in view (ignoring the author filter itself, so more can be added), selected ones always listed.
function dataPeople(max = 8) {
  const D = S.data, n = new Map();
  D.points.forEach(p => { if (p.who && dataPass(p, 'authors')) n.set(p.who, (n.get(p.who) || 0) + 1); });
  const top = [...n].sort((a, b) => b[1] - a[1]).slice(0, max).map(([w]) => w);
  return [...new Set([...D.authors, ...top])].map(w => ({who: w, n: n.get(w) || 0}));
}
const whoOf = author => S.data.whoMap?.get(person(author)) || S.data.whoMap?.get(author) || person(author);
// What one person knows about, within whatever the map is filtered to (types, folders, search; any repos): the topics
// and places where they wrote the biggest share, who they work alongside, and what they changed recently. Code repos
// add their last 90 days of commits and, with an AI engine, a written summary of their focus.
function openPersonPanel(who, {keep = false} = {}) {
  const D = S.data;
  const ctx = D.points.filter(p => dataPass(p, 'authors') && (!D.search || D.search.has(p.id)));
  const mine = ctx.filter(p => p.who === who);
  const group = (items, key) => { const m = new Map(); items.forEach(p => { const k = key(p); if (k != null) m.set(k, (m.get(k) || 0) + 1); }); return m; };
  const allT = group(ctx, dataTopicOf), myT = group(mine, dataTopicOf);
  const topics = [...myT].map(([t, n]) => ({t, n, of: allT.get(t), share: n / allT.get(t)})).filter(x => x.n >= 2)
    .sort((a, b) => b.n * b.share - a.n * a.share).slice(0, 4);
  const allP = group(ctx, p => p.place), myP = group(mine, p => p.place);
  const places = [...myP].map(([k, n]) => ({k, n, of: allP.get(k), share: n / allP.get(k)})).sort((a, b) => b.n - a.n).slice(0, 5);
  const areaOf = p => { const r = repoOfKey(p.dir); const sub = r ? p.dir.slice(r.key.length + 1) : p.dir.split('/').slice(2).join('/'); return sub.split('/').slice(0, 2).join('/') || null; };
  const areas = place => [...group(mine.filter(p => p.place === place), areaOf)].sort((a, b) => b[1] - a[1]).slice(0, 3).map(([a]) => a);
  const near = new Map(); places.forEach(({k}) => ctx.forEach(p => { if (p.place === k && p.who && p.who !== who) near.set(p.who, (near.get(p.who) || 0) + 1); }));
  const peers = [...near].sort((a, b) => b[1] - a[1]).slice(0, 5);
  const tc = group(mine, p => p.type), ts = mine.map(p => p.t).filter(x => !isNaN(x)).sort((a, b) => b - a);
  const recent = [...mine].filter(p => !isNaN(p.t)).sort((a, b) => b.t - a.t).slice(0, 6);
  const tname = id => dataTopics().get(id)?.name || `Topic ${id + 1}`;
  const knows = [...topics.slice(0, 2).map(x => tname(x.t)), ...places.slice(0, 1).map(x => placeName(x.k))];
  const scopeNote = [D.folders.size ? [...D.folders].map(placeName).join(', ') : 'everything indexed', D.types.size < 3 ? [...D.types].map(t => TYPE_NAME[t].toLowerCase()).join(' and ') : '', D.search ? 'search results' : ''].filter(Boolean).join(' · ');
  const pct = x => `${Math.round(x * 100)}%`;
  const itemRow = p => `<button class="item" data-open="${esc(p.open)}"><div class="t"><i class="sw" style="background:${slotColor(TYPE_SLOT[p.type])}"></i><span class="grow">${esc(p.title)}</span><span class="muted">${rel(p.t)}</span></div><div class="s">${esc(p.display.replace(/\/[^/]*$/, ''))}</div></button>`;
  openDrawer(`<div class="meta"><span class="badge"><i class="sw" style="background:${slotColor(D.authorSlot.get(who) ?? -1)}"></i>Person</span>in ${esc(scopeNote)}</div><h2>${esc(who)}</h2>`,
    !mine.length ? `<p class="muted">Nothing by ${esc(who)} in what the map shows now. Widen the filter to see their work.</p>` : `
     ${knows.length ? `<p class="lead">Knows most about <b>${knows.map(esc).join('</b>, <b>')}</b>.</p>` : ''}
     <div class="kpis" style="grid-template-columns:repeat(3,1fr)"><div class="kpi"><div class="l">Items</div><div class="v">${mine.length.toLocaleString()}</div></div>
       <div class="kpi"><div class="l">Share of what's shown</div><div class="v">${pct(mine.length / Math.max(1, ctx.length))}</div></div>
       <div class="kpi"><div class="l">Last change</div><div class="v" style="font-size:15px">${ts.length ? rel(ts[0]) : '–'}</div></div></div>
     <p class="muted" style="font-size:12px;margin:8px 0 0">${Object.keys(TYPE_SLOT).filter(t => tc.get(t)).map(t => plural(tc.get(t), {doc: 'document', code: 'code file', session: 'agent session'}[t])).join(' · ')}${ts.length > 1 ? ` · since ${rel(ts[ts.length - 1])}` : ''}</p>
     ${topics.length ? `<div class="h4">Most knowledgeable about</div><div class="list">${topics.map(x => `<div class="item kn"><div class="t"><i class="sw" style="background:${slotColor(topicSlot(x.t))}"></i><span class="grow">${esc(tname(x.t))}</span><span class="muted">${pct(x.share)} of it</span></div><div class="bar"><i style="width:${x.share * 100}%;background:${slotColor(topicSlot(x.t))}"></i></div><div class="s">${x.n} of the ${x.of} items on this topic</div></div>`).join('')}</div>` : ''}
     <div class="h4">Where</div><div class="list">${places.map(x => `<button class="item" data-pplace="${esc(x.k)}"><div class="t"><span class="grow">${esc(placeName(x.k))}${repoOfKey(x.k) ? '<span class="cbadge">code</span>' : ''}</span><span class="muted">${pct(x.share)} · ${x.n}</span></div>${areas(x.k).length ? `<div class="s">${areas(x.k).map(esc).join(' · ')}</div>` : ''}</button>`).join('')}</div>
     ${peers.length ? `<div class="h4">Works alongside</div><div class="chips">${peers.map(([w, n]) => `<button class="chip" data-ppeer="${esc(w)}" title="Open ${esc(w)}"><i class="sw" style="background:${slotColor(D.authorSlot.get(w) ?? -1)}"></i>${esc(w)}<span class="n">${n}</span></button>`).join('')}</div>` : ''}
     <div id="pteam"></div>
     ${recent.length ? `<div class="h4">Recently changed</div><div class="list">${recent.map(itemRow).join('')}</div>` : ''}
     <div class="actions"><button class="btn" data-pfilter>${D.authors.has(who) && D.authors.size === 1 ? 'Show everyone' : 'Show only their work'}</button></div>`);
  D.drawerPerson = who;
  if (!keep) renderSidebar(); else $$('.prow-w').forEach(r => r.classList.toggle('open', r.querySelector('[data-pwho]')?.dataset.pwho === who));
  const body = $('#drawer-body');
  $$('[data-open]', body).forEach(b => b.onclick = () => openItem(b.dataset.open));
  $$('[data-pplace]', body).forEach(b => b.onclick = () => selectLabel('folder', b.dataset.pplace));
  $$('[data-ppeer]', body).forEach(b => b.onclick = () => openPersonPanel(b.dataset.ppeer));
  $('[data-pfilter]', body)?.addEventListener('click', () => { if (D.authors.has(who) && D.authors.size === 1) D.authors.clear(); else D.authors = new Set([who]); clearSel(); D.highlight = null; dataChanged(); });
  // commits in the code repos they work in (team data is per repo; the busiest two)
  const repos = [...new Set(places.map(x => repoOfKey(x.k)?.name).filter(Boolean))].slice(0, 2);
  if (!repos.length || !mine.some(p => p.type === 'code')) return;
  Promise.all(repos.map(r => api(`/api/team/${enc(r)}`).then(t => ({r, t})).catch(() => null))).then(rs => {
    const box = $('#pteam'); if (!box || D.drawerPerson !== who) return;
    const cards = rs.filter(Boolean).map(({r, t}) => {
      const me = t.people.find(x => whoOf(x.name) === who || whoOf(x.email) === who);
      if (!me) return `<div class="pcard" style="padding:10px 12px;margin-bottom:8px"><div class="top"><b>${esc(r)}</b><span class="st">no commits in the last ${t.days} days</span></div></div>`;
      const sm = t.summaries?.[me.email], wmax = Math.max(1, ...me.weeks);
      return `<div class="pcard" style="padding:10px 12px;margin-bottom:8px">
        <div class="top"><b>${esc(r)}</b><span class="st">${plural(me.commits, 'commit')} · +${kfmt(me.added)} −${kfmt(me.deleted)}</span></div>
        <div class="spark" title="Commits per week (oldest → newest)">${me.weeks.map(w => `<i class="${w ? '' : 'z'}" style="height:${w ? Math.max(12, w / wmax * 100) : 8}%"></i>`).join('')}</div>
        ${sm ? `<div class="focus">${esc(sm.focus)}</div><p class="sum">${esc(sm.summary)}</p>` : t.summarizing ? '<p class="muted" style="font-size:12px;margin:0">Summarizing what they’ve worked on…</p>' : ''}
        <div class="areas">${me.areas.slice(0, 4).map(a => `<span class="chip">${esc(a.area)}</span>`).join('')}</div></div>`;
    });
    if (cards.length) box.innerHTML = `<div class="h4">Commits · last ${rs.find(Boolean)?.t.days || 90} days</div>${cards.join('')}`;
  });
}
// Clicking a person filters the map to their work (several can be picked); clicking them again removes them.
// The arrow beside a name opens what they know about within whatever the map is filtered to (openPersonPanel).
function toggleAuthor(who) {
  const D = S.data;
  if (D.authors.has(who)) D.authors.delete(who); else D.authors.add(who);
  clearSel(); D.highlight = null;
  dataChanged();
}
function dataChanged(fit = true) {
  if (S.data.drawerPerson) openPersonPanel(S.data.drawerPerson, {keep: true});
  const wasRisk = S.data.colorBy === 'risk';
  refreshStates(); renderSidebar(); renderToolbar(); if (wasRisk && S.data.colorBy !== 'risk') recolor(); renderLabels(); renderLegend();
  if (fit && S.view === 'data') setTimeout(() => gl.fit(visibleIdx()), 30);
}
function toggleDataFolder(k, only = false) {
  const D = S.data;
  if (D.folders.has(k) && !only) D.folders.delete(k);
  else {
    if (only) D.folders.clear();
    [...D.folders].forEach(x => { if (x.startsWith(k + '/') || k.startsWith(x + '/')) D.folders.delete(x); });
    D.folders.add(k);
    const parts = k.split('/'); for (let i = 1; i < parts.length; i++) D.treeOpen.add(parts.slice(0, i).join('/'));
    if (dataTree().children.has(k)) D.treeOpen.add(k);
    store.set('dataTreeOpen', [...D.treeOpen]);
  }
  D.highlight = null; clearSel();
  dataChanged();
}
// folder key for a repo-relative directory of the repo whose blame data is loaded
const repoDirKey = (repoName, dir) => { const r = S.data.repos.find(x => x.name === repoName); if (!r) return null; const k = relHome(r.root); return dir && dir !== '.' ? `${k}/${dir}` : k; };
// code-view dirs/paths are repo-relative, or '<repo>/…' when the joint All-repositories data is loaded
const codeSplit = x => S.code.repo === ALL ? [x.split('/')[0], x.split('/').slice(1).join('/')] : [S.code.repo, x];
const codeDirToKey = dir => repoDirKey(...codeSplit(dir));
const ensureRepo = async name => { if (name && S.code.repo !== name) { if (!S.code.repos.length) await loadRepos(); await loadRepo(name); $('#loading').classList.add('gone'); } };
const dataPointFor = (repoName, relPath) => S.data.points.find(p => p.type === 'code' && p.repo === repoName && (p.path.endsWith('/' + relPath) || p.path === relPath));
function highlightData(pred) {
  clearSel();
  S.data.highlight = new Set(S.data.points.filter(pred).map(p => p.id));
  refreshStates(); renderToolbar();
  if (S.data.highlight.size) setTimeout(() => gl.fit(visibleIdx()), 30);
}
const setTypes = (...t) => { S.data.types = new Set(t); if (S.view === 'data') dataChanged(); };

/* ============================== knowledge gaps: areas whose main authors aren't active in the repo any more ============================== */
// "Inactive" is a heuristic from git: no commits in this repo for 180+ days before its latest commit. People may have
// moved teams, so the wording is always "not active here since …", never anything about leaving.
const gapsCache = new Map();
const monY = t => t ? new Date(t * 1000).toLocaleDateString(undefined, {month: 'short', year: 'numeric'}) : null;
const inactiveSince = t => monY(t) ? `not active here since ${monY(t)}` : 'no recent commits here';
async function loadGaps(repo) {
  if (!repo || repo === ALL) return null;
  if (gapsCache.has(repo)) return gapsCache.get(repo);
  const p = api(`/api/repo/${enc(repo)}/gaps?inactive_days=180`).catch(() => null);
  gapsCache.set(repo, p);
  const r = await p; gapsCache.set(repo, r); return r;
}
const peekGaps = repo => { const g = gapsCache.get(repo); return g && !(g instanceof Promise) ? g : null; };
function gapOwnersLine(g) {
  const latest = Math.max(0, ...g.departed.map(d => d.last_active || 0));
  return `${Math.round(g.departed_share * 100)}% written by people ${latest ? `not active here since ${monY(latest)}` : 'with no recent commits here'}`;
}
const busHtml = g => `<span class="${g.bus_factor <= 1 ? 'warn-t' : 'muted'}">${g.bus_factor === 0 ? 'No active owner' : plural(g.bus_factor, 'active owner')}</span>`;
function gapItemHtml(g, repo) {
  return `<div class="gap"><div class="gap-h"><button class="linkbtn mono" data-gap-area="${esc(g.area)}" data-repo="${esc(repo)}" title="Show this area on the map">${esc(g.area)}</button><span class="muted">${g.lines.toLocaleString()} lines</span></div>
    <div class="gap-bar" title="${Math.round(g.departed_share * 100)}% by inactive authors"><i style="width:${g.departed_share * 100}%"></i></div>
    <div class="gap-l">${esc(gapOwnersLine(g))} · ${busHtml(g)}</div>
    ${g.departed.length ? `<div class="gap-l muted">Written by ${g.departed.slice(0, 3).map(d => `<button class="linkbtn subtle" data-gap-person="${esc(d.name)}" data-repo="${esc(repo)}">${esc(d.name)}</button> (${Math.round(d.share * 100)}%${d.last_active ? `, ${esc(monY(d.last_active))}` : ''})`).join(', ')}</div>` : ''}
    ${g.ask ? `<div class="gap-ask">Ask: <button class="linkbtn" data-gap-person="${esc(g.ask)}" data-repo="${esc(repo)}">${esc(g.ask)}</button> <span class="muted">— ${esc(g.ask_reason || '')}</span></div>` : ''}</div>`;
}
async function openGaps(repo) {
  openDrawer(`<div class="meta"><span class="badge">Knowledge gaps</span>${esc(repo)}</div><h2>Areas whose authors aren't active here</h2>`, '<div class="empty"><div class="spinner" style="margin:auto"></div></div>');
  const d = await loadGaps(repo);
  if (!d) return openDrawer(`<div class="meta"><span class="badge">Knowledge gaps</span>${esc(repo)}</div><h2>Knowledge gaps</h2>`, '<p class="muted">Couldn’t load knowledge gaps for this repository.</p>');
  openDrawer(`<div class="meta"><span class="badge">Knowledge gaps</span>${esc(repo)}</div><h2>${d.gaps.length ? plural(d.gaps.length, 'area') + ' at risk' : 'No knowledge gaps'}</h2>`,
    `<p class="muted" style="font-size:12px;margin:0 0 10px">Areas where most of the code was written by people with no commits in ${esc(repo)} for ${d.inactive_days}+ days before its latest commit (${esc(monY(d.as_of) || 'recently')}). They may simply have moved teams — the people listed under “Ask” are the best active contacts.</p>
     ${d.gaps.map(g => gapItemHtml(g, repo)).join('') || '<div class="empty">Every sizeable area has active authors.</div>'}`);
  bindGapLinks($('#drawer-body'));
}
function bindGapLinks(root) {
  $$('[data-gap-area]', root).forEach(b => b.onclick = () => gotoArea(b.dataset.repo, b.dataset.gapArea));
  $$('[data-gap-person]', root).forEach(b => b.onclick = async () => { await setView('data'); await ensureRepo(b.dataset.repo); openPerson(b.dataset.gapPerson); });
  $$('[data-gaps]', root).forEach(b => b.onclick = () => openGaps(b.dataset.gaps));
}
// share of a code point's area written by inactive authors (longest matching gap area), for the "Knowledge risk" colouring
function riskOf(p) {
  if (p.type !== 'code' || !p.repo) return null;
  const d = peekGaps(p.repo); if (!d || !p.repo_root) return null;
  const rel = p.path.slice(p.repo_root.length + 1);
  let best = null;
  for (const g of d.gaps) if ((rel.startsWith(g.area + '/') || g.area === '.') && (!best || g.area.length > best.area.length)) best = g;
  return best ? best.departed_share : 0;
}

/* ============================== files view ============================== */
const KIND_SLOT = {doc: 0, pdf: 1, code: 2, data: 3, slides: 4, notebook: 5, other: -1};
const KIND_NAME = {doc: 'Documents', pdf: 'PDFs', code: 'Code & text', data: 'Data', slides: 'Slides', notebook: 'Notebooks', other: 'Other files'};
const fmtSize = b => b == null ? '' : b < 1024 ? `${b} B` : b < 1048576 ? `${(b / 1024).toFixed(b < 10240 ? 1 : 0)} KB` : `${(b / 1048576).toFixed(1)} MB`;
// a file's color area: its root folder plus one level ('Documents/notes'), or just the root for top-level files
const topOf = rel => { const p = rel.split('/'); return p.length > 2 ? p.slice(0, 2).join('/') : p[0]; };
const dirOf = rel => rel.includes('/') ? rel.slice(0, rel.lastIndexOf('/')) : '';
async function loadFiles() {
  let pts = [];
  try { pts = await api('/api/files/points'); } catch {}
  S.files.points = pts.map(p => ({...p, top: topOf(p.rel), dir: dirOf(p.rel)}));
  S.files.byId = new Map(S.files.points.map(p => [p.id, p]));
  const c = new Map(); S.files.points.forEach(p => c.set(p.top, (c.get(p.top) || 0) + 1));
  S.files.folderSlot = new Map([...c].sort((a, b) => b[1] - a[1]).slice(0, 7).map(([k], i) => [k, i]));
  const au = new Map(); S.files.points.forEach(p => p.author && au.set(p.author, (au.get(p.author) || 0) + 1));
  S.files.authorSlot = new Map([...au].sort((a, b) => b[1] - a[1]).slice(0, 7).map(([k], i) => [k, i]));
  const ts = S.files.points.map(p => p.mtime).filter(Boolean).sort((a, b) => a - b);
  S.files.tRange = [ts[Math.floor(ts.length * 0.05)] || 0, ts[ts.length - 1] || 1];
  S.files.tree = null; S.files.loaded = true; S.files.stale = false;
}
function fileColor(p) {
  switch (S.files.colorBy) {
    case 'folder': return slotColor(S.files.folderSlot.has(p.top) ? S.files.folderSlot.get(p.top) : -1);
    case 'kind': return slotColor(KIND_SLOT[p.kind] ?? -1);
    case 'author': return slotColor(p.author && S.files.authorSlot.has(p.author) ? S.files.authorSlot.get(p.author) : -1);
    case 'topic': return slotColor(p.cluster ?? -1);
    case 'recency': { const [a, b] = S.files.tRange; return seqColor(Math.max(0, Math.min(1, (p.mtime - a) / Math.max(1, b - a)))); }
  }
  return pal().other;
}
const inFolder = (p, k) => p.rel.startsWith(k + '/');
function filePass(p, skip) {
  if (skip !== 'kinds' && S.files.kinds.size && !S.files.kinds.has(p.kind)) return false;
  if (skip !== 'folders' && S.files.folders.size && ![...S.files.folders].some(k => inFolder(p, k))) return false;
  return true;
}
function filesTree() {
  if (S.files.tree?.n === S.files.points.length) return S.files.tree;
  const children = new Map(), seen = new Set();
  for (const p of S.files.points) {
    const parts = p.dir.split('/'); let key = '';
    for (const part of parts) {
      const k = key ? key + '/' + part : part;
      if (!seen.has(k)) { seen.add(k); if (key) { if (!children.has(key)) children.set(key, []); children.get(key).push(k); } }
      key = k;
    }
  }
  return (S.files.tree = {n: S.files.points.length, children, roots: [...seen].filter(k => !k.includes('/'))});
}
function renderFilesSidebar(side) {
  const F = S.files, T = filesTree(), all = F.points, vis = all.filter(p => filePass(p));
  const cnt = new Map();
  all.filter(p => filePass(p, 'folders')).forEach(p => { const parts = p.dir.split('/'); for (let i = 1; i <= parts.length; i++) { const k = parts.slice(0, i).join('/'); cnt.set(k, (cnt.get(k) || 0) + 1); } });
  const kc = {}; all.filter(p => filePass(p, 'kinds')).forEach(p => kc[p.kind] = (kc[p.kind] || 0) + 1);
  const kinds = Object.keys(KIND_SLOT).filter(k => all.some(p => p.kind === k));
  const af = [...F.kinds].map(k => ['kind', k, KIND_NAME[k]]).concat([...F.folders].map(k => ['folder', k, k]));
  side.innerHTML = `${af.length ? `<div class="active-filters">${af.map(([t, v, l]) => `<span class="chip">${esc(l)}<button class="x" data-frm="${t}" data-v="${esc(v)}">✕</button></span>`).join('')}<button class="chip" data-fclear>Clear all</button></div>` : ''}
    <div class="sec"><div class="sec-h"><span>Showing</span></div><div style="padding:0 4px;font-size:13px"><b>${vis.length.toLocaleString()}</b> <span class="muted">of ${plural(all.length, 'file')}</span></div>
      ${all.length ? '' : '<div class="sec-note" style="margin-top:6px">Files appear here once they are indexed. Choose which folders in <a href="#settings">Settings</a>.</div>'}</div>
    <div class="sec"><div class="sec-h"><span>Kinds</span></div><div class="chips">${kinds.map(k => `<button class="chip ${F.kinds.has(k) ? 'on' : ''}" data-kind="${k}"><i class="sw" style="background:${slotColor(KIND_SLOT[k])}"></i>${KIND_NAME[k]}<span class="n">${kc[k] || 0}</span></button>`).join('')}</div></div>
    <div class="sec"><div class="sec-h"><span>Folders</span>${F.folders.size ? '<button data-fclearf>Clear</button>' : ''}</div><div class="sec-note">Click a folder to filter the map; expand to drill in.</div>
      <div class="tree">${T.roots.filter(k => cnt.get(k)).sort((a, b) => cnt.get(b) - cnt.get(a)).map(k => treeHtml(k, 0, cnt, 'fdir', T.children, null, F.treeOpen)).join('') || '<div class="empty" style="padding:8px">No files yet</div>'}</div></div>`;
  $$('[data-kind]', side).forEach(b => b.onclick = () => { toggleSet(F.kinds, b.dataset.kind); filesChanged(); });
  bindTree(side, 'fdir', k => toggleFolder(k), () => renderSidebar(), F.treeOpen, 'filesTreeOpen');
  $$('[data-frm]', side).forEach(b => b.onclick = () => { (b.dataset.frm === 'kind' ? F.kinds : F.folders).delete(b.dataset.v); filesChanged(); });
  $('[data-fclear]', side)?.addEventListener('click', () => { F.kinds.clear(); F.folders.clear(); filesChanged(); });
  $('[data-fclearf]', side)?.addEventListener('click', () => { F.folders.clear(); filesChanged(); });
}
function filesChanged(fit = true) {
  refreshStates(); renderSidebar(); renderLabels();
  if (fit && S.view === 'files') setTimeout(() => gl.fit(visibleIdx()), 30);
}
function toggleFolder(k) {
  const F = S.files;
  if (F.folders.has(k)) F.folders.delete(k);
  else {
    [...F.folders].forEach(x => { if (x.startsWith(k + '/') || k.startsWith(x + '/')) F.folders.delete(x); });
    F.folders.add(k);
    const parts = k.split('/'); for (let i = 1; i < parts.length; i++) F.treeOpen.add(parts.slice(0, i).join('/'));
    store.set('filesTreeOpen', [...F.treeOpen]);
  }
  filesChanged();
}
async function openOnMac(id, action) {
  try {
    const r = await api('/api/open', {method: 'POST', headers: {'content-type': 'application/json'}, body: JSON.stringify({id, action})});
    toast(action === 'reveal' ? 'Shown in Finder' : r.editor ? `Opened in ${r.editor}` : 'Opened');
  } catch (e) { toast('Could not open it: ' + e.message); }
}
const PROSE = new Set(['doc', 'pdf', 'slides', 'other']);
const AUTHOR_FROM = {pdf: 'from the PDF', office: 'from the document properties', spotlight: 'from Spotlight metadata', owner: 'file owner on this Mac', git: 'from git'};
async function openFile(path) {
  if (S.view !== 'data') await setView('data');
  if (!S.data.types.has('doc')) { S.data.types.add('doc'); dataChanged(false); }
  const id = 'file:' + path;
  S.selected = {kind: 'file', id};
  selIdx = mapPts.findIndex(matchSel); gl.setRings(hoverIdx, selIdx);
  if (selIdx >= 0) gl.focusPoint(selIdx, 0.6);
  const p0 = S.files.byId.get(id);
  openDrawer(`<div class="meta"><span class="spinner" style="width:14px;height:14px;border-width:2px"></span> Loading…</div><h2>${esc(p0?.name || path.split('/').pop())}</h2>`, '');
  let f;
  try { f = await api('/api/file?path=' + enc(path)); } catch { openDrawer(`<h2>${esc(path.split('/').pop())}</h2>`, '<p class="muted">This file is not in the index (it may have moved or been excluded).</p>'); return; }
  if (S.selected?.id !== id) return;
  const mono = !PROSE.has(f.kind);
  const chunks = f.chunks.slice(0, 3);
  const preview = chunks.length ? chunks.map(c => `<pre class="fprev ${mono ? 'mono' : ''}">${esc(c.text.length > 2400 ? c.text.slice(0, 2400) + '…' : c.text)}</pre>`).join('') +
    (f.chunks.length > 3 ? `<p class="muted" style="font-size:12px">${plural(f.chunks.length - 3, 'more section')} not shown.</p>` : '')
    : '<p class="muted" style="font-size:12px">Indexed by name only (no readable text).</p>';
  openDrawer(`<div class="meta"><span class="badge"><i class="sw" style="background:${slotColor(KIND_SLOT[f.kind] ?? -1)}"></i>${esc(KIND_NAME[f.kind] || f.kind)}</span>${f.ext ? `<span class="chip">.${esc(f.ext)}</span>` : ''}</div>
      <h2 style="word-break:break-word">${esc(f.name)}</h2>
      <div class="fmeta"><span style="font-family:var(--mono);word-break:break-all">${esc(f.display)}</span></div>
      <div class="fmeta"><span>${fmtSize(f.size)}</span><span>modified ${rel(f.mtime * 1000)}</span>${f.n ? `<span>${plural(f.n, 'section')}</span>` : ''}</div>
      ${f.author ? `<div class="fmeta"><span>By <b style="color:var(--text)">${esc(f.author)}</b> <span class="muted">· ${esc(AUTHOR_FROM[f.author_source] || 'from the file')}</span></span></div>` : ''}
      <div class="fmeta muted" style="font-size:11.5px">${f.author ? 'Files record who authored them; code in git repos shows who wrote each line.' : 'No author recorded for this file. Code in git repos shows who wrote each line.'}</div>`,
    `<div class="actions" style="margin-top:0"><button class="btn" data-a="open">Open</button><button class="btn" data-a="reveal">Reveal in Finder</button><button class="btn" data-a="copy">Copy path</button><button class="btn" data-a="sim">Show similar on map</button></div>
     <div class="h4">Preview</div>${preview}
     ${f.similar?.length ? `<div class="h4">Similar files</div><div class="list">${f.similar.map(x => `<button class="item" data-fpath="${esc(x.path)}"><div class="t"><span class="grow">${esc(x.name)}</span><span class="muted" style="font-size:11px">${Math.round(x.score * 100)}%</span></div><div class="s" style="font-family:var(--mono);font-size:11px">${esc(x.display)}</div></button>`).join('')}</div>` : ''}`);
  const body = $('#drawer-body');
  $('[data-a=open]', body).onclick = () => openOnMac(id, 'open');
  $('[data-a=reveal]', body).onclick = () => openOnMac(id, 'reveal');
  $('[data-a=copy]', body).onclick = () => { navigator.clipboard?.writeText(path); toast('Path copied'); };
  $('[data-a=sim]', body).onclick = () => { S.files.highlight = new Set([id, ...(f.similar || []).map(x => x.id)]); refreshStates(); renderToolbar(); gl.fit(visibleIdx()); };
  $$('[data-fpath]', body).forEach(b => b.onclick = () => openFile(b.dataset.fpath));
}

/* ============================== settings view ============================== */
const SET_GROUPS = [
  ['What gets indexed', ['exclude', 'exclude_files', 'max_file_mb', 'skip_temp_sessions']],
  ['Git repositories', ['sweep_roots', 'sweep_depth', 'max_repo_files']],
  ['Mac app', ['hotkey', 'editor']],
];
const CHIP_LISTS = new Set(['exclude', 'exclude_files']);
const SET_HIDDEN = new Set(['repos', 'disabled', 'folders', 'sources', 'sweep_repos', 'default_scope', 'scopes', 'appearance', 'ai', 'ai_on_battery', 'auto_update_check', 'llm_url', 'llm_model', 'llm_key', 'llm_key_set']);  // managed in their own sections
const SRC_ICON = {
  agents: '<path d="M4 5h16v11H8l-4 4Z" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"/><path d="M8 10h8M8 13h5" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/>',
  files: '<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2Z" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"/>',
  repos: '<circle cx="6" cy="6" r="2.2" fill="none" stroke="currentColor" stroke-width="1.8"/><circle cx="6" cy="18" r="2.2" fill="none" stroke="currentColor" stroke-width="1.8"/><circle cx="18" cy="8" r="2.2" fill="none" stroke="currentColor" stroke-width="1.8"/><path d="M6 8.2v7.6M18 10.2c0 4-6 3-11 6" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/>',
  apps: '<rect x="4" y="4" width="7" height="7" rx="2" fill="none" stroke="currentColor" stroke-width="1.8"/><rect x="13" y="4" width="7" height="7" rx="2" fill="none" stroke="currentColor" stroke-width="1.8"/><rect x="4" y="13" width="7" height="7" rx="2" fill="none" stroke="currentColor" stroke-width="1.8"/><rect x="13" y="13" width="7" height="7" rx="2" fill="none" stroke="currentColor" stroke-width="1.8"/>',
};
// 'a/very/long/path/to/thing' -> 'a/very/lo…/to/thing': keeps both the root and the name readable
const midEllipsis = (t, max) => !t || t.length <= max ? t : t.slice(0, Math.ceil(max * .4)) + '…' + t.slice(-Math.floor(max * .6 - 1));
const srcCount = (n, unit) => n == null ? '' : `${n.toLocaleString()} ${n === 1 && unit ? unit.replace(/s$/, '') : unit || ''}`;
const srcState = {cat: null, hints: new Map(), err: null};
async function loadSources() { try { srcState.cat = await api('/api/sources'); } catch { srcState.cat = srcState.cat || []; } }
function srcHint(id) {
  const h = srcState.hints.get(id);
  if (!h) return '';
  if (Date.now() - h.t > 20000) { srcState.hints.delete(id); return ''; }
  return `<span class="src-hint">${h.on ? 'indexing…' : 'removing…'}</span>`;
}
function srcItem(it, catOn, parentOff = false) {
  const off = !it.available;
  const detail = off ? 'Not found on this Mac' : it.detail;
  const shown = it.kind === 'rule' ? detail : midEllipsis(detail, 64);
  return `<div class="src-item ${it.kind === 'rule' ? 'rule' : ''} ${it.parent ? 'child' : ''} ${catOn && !parentOff ? '' : 'muted-all'} ${off ? 'na' : ''}">
    <label class="tog" title="${off ? 'Not found on this Mac' : it.enabled ? 'Switch off' : 'Switch on'}"><input type="checkbox" data-src-id="${esc(it.id)}" ${it.enabled ? 'checked' : ''} ${off || !catOn ? 'disabled' : ''}></label>
    <div class="src-tx"><div class="src-l">${esc(it.label)}${srcHint(it.id)}</div><div class="src-d ${it.error ? 'warn' : ''}" title="${esc(detail)}">${it.error ? '⚠ ' : ''}${esc(it.error ? detail : shown)}</div></div>
    <div class="src-n">${it.kind === 'rule' ? '' : srcCount(it.count, it.unit)}</div>
    ${it.removable ? `<button class="iconbtn src-rm" data-src-rm="${esc(it.id.slice(6))}" title="Stop indexing this folder">✕</button>` : '<span></span>'}
  </div>`;
}
function sourcesHtml() {
  const cat = srcState.cat;
  if (!cat) return '<div class="set-card"><div class="shimmer" style="height:120px"></div></div>';
  return `<div class="src-head"><h2>Sources</h2><p>What Pensieve indexes. Switching a source off removes its data from the index on the next sweep; switching it back on re-indexes it.</p>${srcState.err ? `<p class="err">${esc(srcState.err)}</p>` : ''}</div>
    <div class="src-grid">${cat.map(c => {
      const rules = c.items.filter(i => i.kind === 'rule' && !i.parent), rest = c.items.filter(i => i.kind !== 'rule' && !i.parent);
      const kids = id => c.items.filter(i => i.parent === id);
      return `<div class="set-card src-card ${c.enabled ? '' : 'off'}">
        <div class="src-top"><span class="src-ic"><svg viewBox="0 0 24 24" width="18" height="18">${SRC_ICON[c.id] || SRC_ICON.apps}</svg></span>
          <div class="src-tx"><h3>${esc(c.label)}${srcHint(c.id)}</h3><p>${esc(c.description)}</p></div>
          <div class="src-n big">${c.count != null ? srcCount(c.count, c.id === 'agents' ? 'sessions' : c.id === 'apps' ? 'items' : 'files') : ''}</div>
          <label class="tog" title="${c.enabled ? 'Switch off everything in ' + esc(c.label) : 'Switch on'}"><input type="checkbox" data-src-id="${esc(c.id)}" ${c.enabled ? 'checked' : ''}></label></div>
        ${rules.length ? `<div class="src-rules">${rules.map(i => srcItem(i, c.enabled) + (kids(i.id).length ? `<div class="src-kids">${kids(i.id).map(k => srcItem(k, c.enabled, !i.enabled)).join('')}</div>` : '')).join('')}</div>` : ''}
        ${rest.length ? `<div class="src-items">${rest.map(i => srcItem(i, c.enabled)).join('')}</div>` : c.items.some(i => i.parent) ? '' : `<div class="src-items"><div class="muted" style="font-size:12px;padding:6px 0">${c.id === 'repos' ? 'No repositories found yet.' : 'Nothing here yet.'}</div></div>`}
        ${c.addable ? `<div class="set-add src-add"><input class="set-in" placeholder="Add a folder, e.g. ~/notes" data-src-add-in><button class="btn" data-src-add>Add folder</button></div><div class="err" data-src-add-err></div>` : ''}
      </div>`;
    }).join('')}</div>`;
}
function renderSources() {
  const box = $('#srcbox'); if (!box) return;
  box.innerHTML = sourcesHtml();
  $$('[data-src-id]', box).forEach(i => i.onchange = async () => {
    const id = i.dataset.srcId, on = i.checked;
    i.disabled = true;
    try {
      srcState.cat = await api('/api/sources', {method: 'PUT', headers: {'content-type': 'application/json'}, body: JSON.stringify({id, enabled: on})});
      srcState.hints.set(id, {on, t: Date.now()}); srcState.err = null;
      setTimeout(renderSources, 20500);
    } catch (e) { srcState.err = `Could not change ${id}: ${e.message}`; }
    renderSources();
  });
  $$('[data-src-rm]', box).forEach(b => b.onclick = async () => {
    try { srcState.cat = await api('/api/sources/folders?path=' + enc(b.dataset.srcRm), {method: 'DELETE'}); toast('Folder removed — its files leave the index on the next sweep'); }
    catch (e) { srcState.err = 'Could not remove folder: ' + e.message; }
    renderSources();
  });
  const add = async () => {
    const inp = $('[data-src-add-in]', box), path = inp.value.trim(); if (!path) return;
    const r = await fetch('/api/sources/folders', {method: 'POST', headers: {'content-type': 'application/json'}, body: JSON.stringify({path})});
    const j = await r.json().catch(() => ({}));
    if (!r.ok) { $('[data-src-add-err]', box).textContent = typeof j.detail === 'string' ? j.detail : 'Could not add that folder'; return; }
    srcState.cat = j; srcState.hints.set('files', {on: true, t: Date.now()}); renderSources(); toast('Folder added — indexing it now');
  };
  $('[data-src-add]', box)?.addEventListener('click', add);
  $('[data-src-add-in]', box)?.addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); add(); } });
}
const refetchSourcesSoon = debounce(async () => { if (S.view !== 'settings') return; await Promise.all([loadSources(), loadScopes()]); renderSources(); renderScopes(); }, 1200);

/* ---- scopes: named slices of the index an agent (or the search panel) works within ---- */
const scState = {data: null, edit: null, filter: '', resolve: null, confirmDel: null, err: null};
async function loadScopes() { try { scState.data = await api('/api/scopes'); } catch { scState.data = scState.data || {scopes: [], default_scope: ''}; } renderPalScope(); }
const scopeNames = () => (scState.data?.scopes || []).map(x => x.name);
function srcLabel(id) {
  for (const c of srcState.cat || []) {
    if (c.id === id) return {text: `All ${c.label.toLowerCase()}`, cat: c.id};
    const it = c.items.find(i => i.id === id);
    if (it) return {text: it.label, cat: c.id, title: it.detail};
  }
  const [cat, ...rest] = id.split('/');
  return {text: rest.join('/').split('/').filter(Boolean).pop() || id, cat, title: id};
}
const CAT_SHORT = {agents: 'agent', files: 'folder', repos: 'repo', apps: 'app'};
function scopeCounts(x) {
  const parts = [];
  if (x.chunks?.code) parts.push(`${x.chunks.code.toLocaleString()} code chunks`);
  if (x.chunks?.file) parts.push(`${x.chunks.file.toLocaleString()} file chunks`);
  if (x.sessions) parts.push(plural(x.sessions, 'session'));
  return parts.join(' · ') || 'Nothing indexed yet';
}
const mcpBase = () => `http://127.0.0.1:${location.port || '8765'}/mcp`;
const httpCmd = name => `claude mcp add --transport http pensieve "${mcpBase()}${name ? '?scope=' + encodeURIComponent(name) : ''}"`;
const stdioCmd = name => `pensieve mcp${name ? ' --scope ' + name : ''}`;
function scopeEditor() {
  const e = scState.edit, q = scState.filter.toLowerCase();
  const cats = (srcState.cat || []).map(c => {
    const whole = e.sources.has(c.id);
    const items = c.items.filter(i => i.kind !== 'rule');
    const shown = c.id === 'repos' && q ? items.filter(i => (i.label + ' ' + i.detail).toLowerCase().includes(q)) : items;
    const n = items.filter(i => e.sources.has(i.id)).length;
    return `<div class="sc-cat"><label class="sc-check head"><input type="checkbox" data-sc-src="${esc(c.id)}" ${whole ? 'checked' : ''}><b>${esc(c.label)}</b><span class="muted">${whole ? 'everything' : n ? `${n} selected` : ''}</span></label>
      ${c.id === 'repos' && items.length > 6 ? `<input class="set-in sc-filter" placeholder="Filter repositories…" value="${esc(scState.filter)}" data-sc-filter>` : ''}
      <div class="sc-items ${whole ? 'all' : ''}">${shown.map(i => `<label class="sc-check" title="${esc(i.detail || '')}"><input type="checkbox" data-sc-src="${esc(i.id)}" ${whole || e.sources.has(i.id) ? 'checked' : ''} ${whole ? 'disabled' : ''}>${esc(i.label)}<span class="muted">${i.count != null ? srcCount(i.count, i.unit) : ''}</span></label>`).join('') || '<span class="muted" style="font-size:12px">No matches</span>'}</div></div>`;
  }).join('');
  return `<div class="set-card sc-edit"><h3>${e.orig ? `Edit scope “${esc(e.orig)}”` : 'New scope'}</h3>
    <div class="sc-form"><label>Name<input class="set-in" data-sc-name value="${esc(e.name)}" ${e.orig ? 'disabled' : ''} placeholder="e.g. payments"></label>
      <label>Description<input class="set-in" data-sc-desc value="${esc(e.description)}" placeholder="What this slice is for"></label></div>
    <div class="d" style="font-size:12px;color:var(--text-3);margin:10px 0 6px">Sources — agent sessions that ran in a chosen repo come along automatically.</div>
    <div class="sc-pick">${cats}</div>
    ${scState.err ? `<div class="err">${esc(scState.err)}</div>` : ''}
    <div class="sc-actions"><button class="btn" data-sc-cancel>Cancel</button><button class="btn primary" data-sc-save ${e.name.trim() && e.sources.size ? '' : 'disabled'}>Save scope</button></div></div>`;
}
function scopesHtml() {
  const d = scState.data;
  if (!d) return '<div class="set-card"><div class="shimmer" style="height:90px"></div></div>';
  const def = d.default_scope || '';
  const card = x => {
    const chips = x.sources.map(id => { const l = srcLabel(id); return `<span class="chip sc-chip" title="${esc(l.title || id)}"><span class="muted">${CAT_SHORT[l.cat] || l.cat}</span> ${esc(l.text)}</span>`; }).join('');
    return `<div class="set-card sc-card"><div class="sc-top"><div><h3>${esc(x.name)}${def === x.name ? ' <span class="badge">default</span>' : ''}</h3>${x.description ? `<p>${esc(x.description)}</p>` : ''}</div>
      <div class="sc-btns"><button class="btn" data-sc-editbtn="${esc(x.name)}">Edit</button><button class="btn ${scState.confirmDel === x.name ? 'danger' : ''}" data-sc-del="${esc(x.name)}">${scState.confirmDel === x.name ? 'Confirm delete' : 'Delete'}</button></div></div>
      <div class="set-chips">${chips}</div><div class="sc-n">${scopeCounts(x)}</div>
      <div class="cmd"><code>${esc(stdioCmd(x.name))}</code><button class="btn" data-copy="${esc(stdioCmd(x.name))}">Copy</button></div>
      <div class="cmd"><code>${esc(httpCmd(x.name))}</code><button class="btn" data-copy="${esc(httpCmd(x.name))}">Copy</button></div></div>`;
  };
  return `<div class="src-head"><h2>Scopes</h2><p>Named slices of the index. An agent connected with a scope only searches inside it; the search panel uses the default below.</p></div>
    <div class="set-card sc-def"><div class="sc-top"><div><h3>Default for the search panel and visualizer search</h3><p>Plain searches use this unless you pick another scope in the search box.</p></div>
      <select class="set-in" data-sc-default style="max-width:240px"><option value="" ${def ? '' : 'selected'}>All — everything</option>${scopeNames().map(n => `<option value="${esc(n)}" ${def === n ? 'selected' : ''}>${esc(n)}</option>`).join('')}</select></div></div>
    ${d.scopes.map(card).join('') || '<p class="muted" style="font-size:12.5px">No scopes yet. Create one for a group of repos you work on together.</p>'}
    ${scState.edit ? scopeEditor() : '<button class="btn" data-sc-new style="margin-bottom:14px">+ New scope</button>'}
    <div class="set-card"><h3>How agents pick a scope</h3>
      <p class="sc-p"><code>pensieve mcp</code> (stdio) uses <b>--scope auto</b>: the repo the agent was started in, or the first named scope that includes that repo. Outside a repo it sees everything. Pin one with <code>pensieve mcp --scope &lt;name&gt;</code>.</p>
      <p class="sc-p">Over HTTP Pensieve can't see the agent's folder, so add <code>?scope=&lt;name&gt;</code> to the URL; without it the agent sees everything.</p>
      <div class="set-add"><input class="set-in" placeholder="Try auto: a folder an agent might start in, e.g. ~/code/my-repo" data-sc-try-in><button class="btn" data-sc-try>Resolve</button></div>
      ${scState.resolve ? `<div class="sc-resolve">${scState.resolve.error ? `<span class="err">${esc(scState.resolve.error)}</span>` : `→ <b>${esc(scState.resolve.name)}</b>${scState.resolve.description ? ` <span class="muted">— ${esc(scState.resolve.description)}</span>` : ''}<div class="set-chips" style="margin-top:6px">${(scState.resolve.sources || []).map(id => `<span class="chip sc-chip">${esc(srcLabel(id).text)}</span>`).join('') || '<span class="muted">all sources</span>'}</div>`}</div>` : ''}
    </div>`;
}
function renderScopes() {
  const box = $('#scopebox'); if (!box) return;
  box.innerHTML = scopesHtml();
  const E = scState.edit;
  $$('[data-copy]', box).forEach(b => b.onclick = () => { navigator.clipboard?.writeText(b.dataset.copy); toast('Copied'); });
  $('[data-sc-default]', box)?.addEventListener('change', async e => {
    const v = e.target.value;
    try { await api('/api/settings', {method: 'PUT', headers: {'content-type': 'application/json'}, body: JSON.stringify({default_scope: v})}); toast(v ? `Search defaults to “${v}”` : 'Search covers everything'); }
    catch (err) { toast('Could not set the default: ' + err.message); }
    await loadScopes(); renderScopes();
  });
  $('[data-sc-new]', box)?.addEventListener('click', () => { scState.edit = {name: '', description: '', sources: new Set(), orig: null}; scState.err = null; renderScopes(); $('[data-sc-name]', box)?.focus(); });
  $$('[data-sc-editbtn]', box).forEach(b => b.onclick = () => { const x = scState.data.scopes.find(s => s.name === b.dataset.scEditbtn); scState.edit = {name: x.name, description: x.description || '', sources: new Set(x.sources), orig: x.name}; scState.err = null; renderScopes(); });
  $$('[data-sc-del]', box).forEach(b => b.onclick = async () => {
    const n = b.dataset.scDel;
    if (scState.confirmDel !== n) { scState.confirmDel = n; renderScopes(); setTimeout(() => { if (scState.confirmDel === n) { scState.confirmDel = null; renderScopes(); } }, 4000); return; }
    scState.confirmDel = null;
    try { scState.data = await api('/api/scopes/' + enc(n), {method: 'DELETE'}); toast(`Deleted scope “${n}”`); } catch (e) { toast('Could not delete: ' + e.message); }
    await loadScopes(); renderScopes();
  });
  $('[data-sc-try]', box)?.addEventListener('click', tryResolve);
  $('[data-sc-try-in]', box)?.addEventListener('keydown', e => { if (e.key === 'Enter') tryResolve(); });
  if (!E) return;
  const name = $('[data-sc-name]', box), desc = $('[data-sc-desc]', box), save = $('[data-sc-save]', box);
  const sync = () => { E.name = name.value; E.description = desc.value; save.disabled = !(E.name.trim() && E.sources.size); };
  name.oninput = sync; desc.oninput = sync;
  $$('[data-sc-src]', box).forEach(c => c.onchange = () => {
    const id = c.dataset.scSrc;
    if (c.checked) { E.sources.add(id); if (!id.includes('/')) [...E.sources].forEach(x => x.startsWith(id + '/') && E.sources.delete(x)); }
    else E.sources.delete(id);
    renderScopes();
  });
  const f = $('[data-sc-filter]', box);
  if (f) f.oninput = debounce(() => { scState.filter = f.value; renderScopes(); const g = $('[data-sc-filter]'); g?.focus(); g?.setSelectionRange(99, 99); }, 120);
  $('[data-sc-cancel]', box).onclick = () => { scState.edit = null; scState.err = null; renderScopes(); };
  save.onclick = async () => {
    const r = await fetch('/api/scopes/' + enc(E.name.trim()), {method: 'PUT', headers: {'content-type': 'application/json'}, body: JSON.stringify({sources: [...E.sources], description: E.description.trim()})});
    const j = await r.json().catch(() => ({}));
    if (!r.ok) { scState.err = typeof j.detail === 'string' ? j.detail : 'Could not save the scope'; return renderScopes(); }
    toast(`Saved scope “${E.name.trim()}”`); scState.edit = null; scState.err = null; scState.filter = '';
    await loadScopes(); renderScopes();
  };
}
async function tryResolve() {
  const v = $('[data-sc-try-in]')?.value.trim(); if (!v) return;
  try { scState.resolve = await api(`/api/scopes/resolve?scope=auto&cwd=${enc(v)}`); } catch (e) { scState.resolve = {error: e.message}; }
  renderScopes(); const i = $('[data-sc-try-in]'); if (i) i.value = v;
}
/* palette scope switcher */
const palScope = () => $('#palscope')?.value || 'all';
function renderPalScope() {
  const sel = $('#palscope'); if (!sel || !scState.data) return;
  const def = scState.data.default_scope || 'all', cur = sel.dataset.touched ? sel.value : def;
  sel.innerHTML = `<option value="all">All</option>${scopeNames().map(n => `<option value="${esc(n)}">${esc(n)}</option>`).join('')}`;
  sel.value = [...sel.options].some(o => o.value === cur) ? cur : def;
  sel.hidden = !scopeNames().length;
}
const settingsNav = () => `<div class="sec"><div class="sec-h"><span>Settings</span></div><div class="rows"><button class="row" data-setgo="Sources"><span class="name">Sources</span></button><button class="row" data-setgo="Scopes"><span class="name">Scopes</span></button>${SET_GROUPS.map(([g]) => `<button class="row" data-setgo="${esc(g)}"><span class="name">${esc(g)}</span></button>`).join('')}<button class="row" data-setgo="Connect an agent"><span class="name">Connect an agent</span></button><button class="row" data-setgo="Index"><span class="name">Index</span></button></div></div>`;
function bindSettingsNav(side) {
  $$('[data-setgo]', side).forEach(b => b.onclick = () => document.getElementById('set-' + b.dataset.setgo.replace(/\W+/g, '-'))?.scrollIntoView({behavior: 'smooth', block: 'start'}));
}
async function loadSettings(force = false) {
  if (S.set && !force) return;
  const [r, st] = await Promise.all([api('/api/settings'), api('/api/status').catch(() => S.status), loadSources(), loadScopes()]);
  Object.assign(S.status, st);
  S.set = {orig: r.settings, draft: structuredClone(r.settings), defaults: r.defaults, desc: r.descriptions, err: null, errKey: null};
}
const setDirty = () => S.set ? Object.keys(S.set.draft).filter(k => JSON.stringify(S.set.draft[k]) !== JSON.stringify(S.set.orig[k])) : [];
function setField(k) {
  const v = S.set.draft[k], d = S.set.defaults[k] ?? v;
  const id = `sf-${k}`;
  if (typeof d === 'boolean') return `<label class="tog"><input type="checkbox" data-sk="${k}" ${v ? 'checked' : ''}>${v ? 'On' : 'Off'}</label>`;
  if (typeof d === 'number') return `<input class="set-in" type="number" min="0" step="1" data-sk="${k}" value="${esc(v)}" style="max-width:140px">`;
  if (Array.isArray(d)) {
    if (CHIP_LISTS.has(k)) return `<div class="set-chips">${v.map((x, i) => `<span class="chip">${esc(x)}<button class="x" data-lrm="${k}" data-i="${i}" title="Remove">✕</button></span>`).join('')}</div>
      <div class="set-add"><input class="set-in" id="${id}" placeholder="Add a name or pattern…" data-ladd-in="${k}"><button class="btn" data-ladd="${k}">Add</button></div>`;
    return `<div class="set-list">${v.map((x, i) => `<div class="li"><input class="set-in" data-li="${k}" data-i="${i}" value="${esc(x)}"><button class="iconbtn" data-lrm="${k}" data-i="${i}" title="Remove">✕</button></div>`).join('') || '<span class="muted" style="font-size:12px">None</span>'}</div>
      <div class="set-add"><input class="set-in" id="${id}" placeholder="${k === 'repos' ? '~/code/some-repo' : '~/path/to/folder'}" data-ladd-in="${k}"><button class="btn" data-ladd="${k}">Add</button></div>`;
  }
  if (d && typeof d === 'object') return Object.keys({...d, ...v}).map(n => `<label class="tog"><input type="checkbox" data-sobj="${k}" data-n="${n}" ${v[n] !== false ? 'checked' : ''}>${esc(AGENT_NAME[n] || n)}</label>`).join('');
  if (k === 'editor') return `<select class="set-in" data-sk="${k}" style="max-width:200px">${[['default', 'Default app'], ['vscode', 'VS Code'], ['cursor', 'Cursor'], ['zed', 'Zed']].map(([o, l]) => `<option value="${o}" ${v === o ? 'selected' : ''}>${l}</option>`).join('')}</select>`;
  return `<input class="set-in" data-sk="${k}" value="${esc(v)}">`;
}
// Settings is a System-Settings-style sheet: a list on the left, one section at a time on the right.
const SET_TABS = [
  ['general', 'General', '#8e8e93', '<circle cx="12" cy="12" r="3" fill="none" stroke="#fff" stroke-width="1.8"/><path d="M12 3.5v2.4M12 18.1v2.4M3.5 12h2.4M18.1 12h2.4M6 6l1.7 1.7M16.3 16.3 18 18M6 18l1.7-1.7M16.3 7.7 18 6" stroke="#fff" stroke-width="1.8" stroke-linecap="round"/>'],
  ['sources', 'Sources', '#0a84ff', '<path d="M4 6.5C4 5.1 7.6 4 12 4s8 1.1 8 2.5S16.4 9 12 9 4 7.9 4 6.5Z M4 6.5v11C4 18.9 7.6 20 12 20s8-1.1 8-2.5v-11 M4 12c0 1.4 3.6 2.5 8 2.5s8-1.1 8-2.5" fill="none" stroke="#fff" stroke-width="1.8"/>'],
  ['scopes', 'Scopes', '#5e5ce6', '<circle cx="12" cy="12" r="7.5" fill="none" stroke="#fff" stroke-width="1.8"/><circle cx="12" cy="12" r="3" fill="#fff"/>'],
  ['agents', 'Search & Agents', '#30b0c7', '<circle cx="11" cy="11" r="6" fill="none" stroke="#fff" stroke-width="2"/><path d="m20 20-4.2-4.2" stroke="#fff" stroke-width="2" stroke-linecap="round"/>'],
  ['ai', 'AI & Insights', '#bf5af2', '<path d="M12 3.5l1.9 5.1 5.1 1.9-5.1 1.9L12 17.5l-1.9-5.1-5.1-1.9 5.1-1.9Z" fill="#fff"/><circle cx="18.5" cy="17.5" r="1.6" fill="#fff"/>'],
  ['appearance', 'Appearance', '#ff9f0a', '<circle cx="12" cy="12" r="7.5" fill="none" stroke="#fff" stroke-width="1.8"/><path d="M12 4.5a7.5 7.5 0 0 1 0 15Z" fill="#fff"/>'],
  ['advanced', 'Advanced', '#8e8e93', '<path d="M5 7h8M17 7h2M5 17h2M11 17h8" stroke="#fff" stroke-width="2" stroke-linecap="round"/><circle cx="15" cy="7" r="2" fill="none" stroke="#fff" stroke-width="1.8"/><circle cx="9" cy="17" r="2" fill="none" stroke="#fff" stroke-width="1.8"/>'],
];
S.setTab = store.get('setTab', 'sources');
// General: version, update check, reset. Inside the Mac app (PensieveMac/2+) the app checks and installs updates itself.
const IN_APP = /PensieveMac\/(\d+)/.test(navigator.userAgent) && +RegExp.$1 >= 2;
const fmtBytes = n => n >= 1e9 ? (n / 1e9).toFixed(1) + ' GB' : n >= 1e6 ? Math.round(n / 1e6) + ' MB' : Math.round(n / 1e3) + ' KB';
async function bindGeneral() {
  const ab = $('#aboutbox');
  try {
    const a = await api('/api/about');
    if (ab) ab.innerHTML = `<div class="set-row" style="border-top:0"><div><div class="k">Version</div><div class="d">Pensieve ${esc(a.version)}</div></div><div><a class="btn" href="https://github.com/TedHaley/pensieve/releases" target="_blank" rel="noopener">Release notes</a></div></div>
      <div class="set-row"><div><div class="k">Index</div><div class="d">${fmtBytes(a.index_bytes)} in <code>${esc(a.data_dir)}</code></div></div><div></div></div>`;
  } catch { if (ab) ab.innerHTML = '<p class="sc-p">Could not load version info.</p>'; }
  const chk = $('#updcheck'), msg = $('#updmsg');
  if (chk) chk.onclick = async () => {
    if (IN_APP) { location.href = 'pensieve://check-updates'; return; }  // the app shows the result and can install
    chk.disabled = true; msg.textContent = 'Checking…';
    try {
      const u = await api('/api/update/check');
      msg.innerHTML = u.newer ? `Pensieve ${esc(u.latest)} is available (you have ${esc(u.current)}). <a href="${esc(u.url)}" target="_blank" rel="noopener">Download</a>, or use Check for Updates in the Mac app’s menu.`
        : `You’re up to date: ${esc(u.current)} is the latest release.`;
    } catch (e) { msg.textContent = 'Could not check: ' + e.message; }
    chk.disabled = false;
  };
  renderReset();
}
function renderReset(step) {
  const box = $('#resetbox');
  if (!box) return;
  if (step === 'busy') { box.innerHTML = `<p class="sc-p"><span class="spinner" style="display:inline-block;width:12px;height:12px;vertical-align:-2px;margin-right:6px"></span>Resetting. Pensieve is restarting and will rebuild the index from scratch; this page reloads when it's back.</p>`; return; }
  const all = !!box.dataset.all;
  box.innerHTML = step === 'confirm'
    ? `<p class="sc-p"><b>Delete the index${all ? ' and your settings' : ''} and restart?</b> Search, maps and insights start empty and fill in again as Pensieve re-indexes your ${all ? 'default' : ''} sources, which can take a while for big repos. Your files, repos and agent sessions themselves are not touched.</p>
       <div class="bar-btns" style="justify-content:flex-start"><button class="btn" data-reset-cancel>Cancel</button><button class="btn danger" data-reset-go>Delete and restart</button></div>`
    : `<div class="set-row" style="border-top:0"><div><div class="k">Delete the index and start over</div><div class="d">Removes everything Pensieve has indexed (search, maps, summaries, insights) and rebuilds it. Use it if results look wrong after an upgrade.</div></div><div><button class="btn danger" data-reset>Reset…</button></div></div>
       <div class="set-row"><div><div class="k">Also reset settings</div><div class="d">Back to default folders, scopes, AI engine and hotkey: a factory reset.</div></div><div><label class="tog"><input type="checkbox" data-reset-all ${all ? 'checked' : ''}></label></div></div>`;
  $('[data-reset-all]', box)?.addEventListener('change', e => { if (e.target.checked) box.dataset.all = '1'; else delete box.dataset.all; });
  $('[data-reset]', box)?.addEventListener('click', () => renderReset('confirm'));
  $('[data-reset-cancel]', box)?.addEventListener('click', () => renderReset());
  $('[data-reset-go]', box)?.addEventListener('click', async () => {
    renderReset('busy');
    try { await api('/api/reset', {method: 'POST', headers: {'content-type': 'application/json'}, body: JSON.stringify({settings: all})}); }
    catch (e) { box.innerHTML = `<p class="sc-p">Could not reset: ${esc(e.message)}</p>`; return; }
    await new Promise(r => setTimeout(r, 2500));
    for (let i = 0; i < 120; i++) {
      try { if ((await fetch('/api/status', {cache: 'no-store'})).ok) { location.hash = ''; location.reload(); return; } } catch {}
      await new Promise(r => setTimeout(r, 1000));
    }
    box.innerHTML = '<p class="sc-p">Pensieve hasn’t come back yet. Quit and reopen the app if this page stays empty.</p>';
  });
}

// Agent add-ons: an instructions block for CLAUDE.md / AGENTS.md and optional Claude Code skills (pensieve/integrations)
const IG_STATE = {on: 'Installed', stale: 'Update available', off: '', theirs: 'A folder with this name exists (not Pensieve’s)'};
async function loadIntegrations() {
  const box = $('#igbox');
  if (!box) return;
  let st;
  try { st = await (await fetch('/api/integrations')).json(); } catch { box.innerHTML = '<p class="sc-p">Could not load add-ons.</p>'; return; }
  const tog = (kind, id, state, dis) => `<label class="tog"><input type="checkbox" data-ig="${kind}:${id}" ${state === 'on' || state === 'stale' ? 'checked' : ''} ${dis ? 'disabled' : ''}></label>`;
  const row = (k, d, kind, id, state, dis) => `<div class="set-row"><div><div class="k">${k}</div><div class="d">${d}${IG_STATE[state] ? ` · <b>${esc(IG_STATE[state])}</b>` : ''}</div></div><div>${state === 'stale' ? `<button class="btn" data-igup="${kind}:${id}">Update</button> ` : ''}${tog(kind, id, state, dis)}</div></div>`;
  box.innerHTML = `<p class="sc-p">Agents grep by habit. These tell them to search with Pensieve first, which on our benchmark solved more lookups with about 40% fewer tool calls. Nothing changes until you switch one on; switching it off removes exactly what was added.</p>
    <div class="cmd-l">Instructions block: “search before you grep”</div>
    ${st.instructions.map(t => row(esc(t.label), `Adds a marked section to <code>${esc(t.path)}</code>`, 'ins', t.id, t.state)).join('')}
    <div class="cmd-l" style="margin-top:10px">Claude Code skills (in <code>~/.claude/skills</code>)</div>
    ${st.skills.map(k => row(esc(k.label) + (k.recommended ? ' <span class="ig-rec">Recommended</span>' : ''), esc(k.description), 'skill', k.id, k.state, k.state === 'theirs')).join('')}
    <div class="cmd-l" style="margin-top:10px">Other agents (Cursor, Windsurf, …): paste into the agent’s rules</div>
    <div class="cmd ig-text"><code>${esc(st.text)}</code><button class="btn" data-copy="${esc(st.text)}">Copy</button></div>
    <p class="sc-p" style="margin-top:6px">From a terminal: <code>pensieve integrate recommended</code></p>`;
  const set = async (key, on) => {
    const [kind, id] = key.split(':');
    const r = await fetch('/api/integrations/' + encodeURIComponent(id), {method: 'PUT', headers: {'content-type': 'application/json'}, body: JSON.stringify({on})});
    if (!r.ok) toast('Could not change: ' + ((await r.json().catch(() => ({}))).detail || r.status));
    else toast(on ? (kind === 'ins' ? 'Instructions added' : 'Skill installed') : (kind === 'ins' ? 'Instructions removed' : 'Skill removed'));
    loadIntegrations();
  };
  $$('[data-ig]', box).forEach(i => i.onchange = () => set(i.dataset.ig, i.checked));
  $$('[data-igup]', box).forEach(b => b.onclick = () => set(b.dataset.igup, true));
  $$('[data-copy]', box).forEach(b => b.onclick = () => { navigator.clipboard?.writeText(b.dataset.copy); toast('Copied'); });
}

function setTab(t) { S.setTab = t; store.set('setTab', t); if (S.view === 'settings') renderSettings(); }
async function renderSettings() {
  const el = $('#settings');
  const was = $('#setmain'), keep = was ? {tab: was.dataset.tab, y: was.scrollTop} : null;  // before anything replaces it
  if (!S.set) { el.innerHTML = '<div class="empty"><div class="spinner" style="margin:auto"></div></div>'; try { await loadSettings(); } catch { el.innerHTML = '<div class="empty">Could not load settings.</div>'; return; } }
  const st = S.status, dirty = setDirty(), port = location.port || '8765', mcpUrl = `http://127.0.0.1:${port}/mcp`;
  const tab = SET_TABS.some(t => t[0] === S.setTab) ? S.setTab : 'sources';
  const known = new Set([...SET_GROUPS.flatMap(g => g[1]), ...SET_HIDDEN]);
  const groups = [...SET_GROUPS.map(([g, ks]) => [g, ks.filter(k => k in S.set.draft)]), ['Other', Object.keys(S.set.draft).filter(k => !known.has(k))]].filter(g => g[1].length);
  const row = k => `<div class="set-row ${dirty.includes(k) ? 'changed' : ''}"><div><div class="k">${esc(SET_LABEL[k] || k)}</div><div class="d">${esc(S.set.desc[k] || '')}</div></div>
    <div>${setField(k)}${S.set.errKey === k ? `<div class="err">${esc(S.set.err)}</div>` : ''}</div></div>`;
  const cmd = (c, label) => `<div class="cmd"><code>${esc(c)}</code><button class="btn" data-copy="${esc(c)}">${label || 'Copy'}</button></div>`;
  const kpi = (l, v) => `<div class="kpi"><div class="l">${l}</div><div class="v">${(v || 0).toLocaleString()}</div></div>`;
  const card = ([g, ks]) => `<div class="set-card" id="set-${g.replace(/\W+/g, '-')}"><h3>${esc(g)}</h3>${ks.map(row).join('')}</div>`;
  const form = ks => groups.filter(([g]) => ks.includes(g));
  const bar = (title, sub, withSave) => `<div class="set-bar"><div><h1>${esc(title)}</h1><div class="msg ${S.set.err ? 'bad' : ''}">${S.set.err && !S.set.errKey ? esc(S.set.err) : withSave && dirty.length ? plural(dirty.length, 'unsaved change') : esc(sub)}</div></div>
      ${withSave ? `<div class="bar-btns"><button class="btn" id="setreset" ${dirty.length ? '' : 'disabled'}>Discard</button><button class="btn primary" id="setsave" ${dirty.length ? '' : 'disabled'}>Save</button></div>` : ''}</div>`;
  let body = '';
  if (tab === 'sources') body = bar('Sources', 'What Pensieve indexes. Switching a source off removes its data on the next sweep.') + `<div id="set-Sources"><div id="srcbox">${sourcesHtml()}</div></div>`;
  else if (tab === 'scopes') body = bar('Scopes', 'Named slices of the index that agents and search work within.') + `<div id="set-Scopes"><div id="scopebox">${scopesHtml()}</div></div>`;
  else if (tab === 'agents') body = bar('Search & Agents', 'How you and your agents reach Pensieve.', true) + form(['Mac app']).map(card).join('') + `
    <div class="set-card" id="set-Connect-an-agent"><h3>Connect an agent</h3>
      <p class="sc-p">Pensieve is an MCP server: Claude Code, Codex, Cursor and other agents can search your files, code and sessions and find who knows what. Agents get the 10 search and people tools by default; add <code>?tools=all</code> (HTTP) or <code>--all-tools</code> (stdio) to let one change these settings too.</p>
      <div class="cmd-l">Claude Code (HTTP)</div>${cmd(`claude mcp add --transport http pensieve ${mcpUrl}`)}
      <div class="cmd-l">Agents that only speak stdio — scoped automatically to the repo the agent starts in</div>${cmd('pensieve mcp')}
      <div class="cmd-l">JSON config (Cursor, Claude Desktop, …)</div>${cmd(JSON.stringify({mcpServers: {pensieve: {command: 'pensieve', args: ['mcp']}}}))}
      <p class="sc-p" style="margin-top:8px">To limit an agent to a named slice, use the per-scope commands in <a href="#scopes">Scopes</a>.</p>
    </div>
    <div class="set-card" id="set-Agent-add-ons"><h3>Agent add-ons</h3><div id="igbox"><div class="spinner" style="margin:8px 0"></div></div></div>`;
  else if (tab === 'ai') body = bar('AI & Insights', 'Optional: the engine that writes summaries, topic names and insights.') + aiSettingsHtml();
  else if (tab === 'appearance') body = bar('Appearance', 'Matches the Pensieve Mac app.') + `
    <div class="set-card"><div class="set-row"><div><div class="k">Appearance</div><div class="d">System follows your Mac's light or dark setting as it changes.</div></div>
      <div><div class="seg app-seg" role="radiogroup" aria-label="Appearance">${APPEARANCES.map(m => `<button data-app="${m}" class="${appearance === m ? 'on' : ''}">${m[0].toUpperCase() + m.slice(1)}</button>`).join('')}</div></div></div></div>`;
  else if (tab === 'general') body = bar('General', 'Version, updates, and starting over.') + `
    <div class="set-card" id="set-About"><h3>About</h3><div id="aboutbox"><div class="spinner" style="margin:8px 0"></div></div></div>
    <div class="set-card" id="set-Updates"><h3>Updates</h3>
      <div class="set-row" style="border-top:0"><div><div class="k">Check for updates</div><div class="d" id="updmsg">${IN_APP ? 'Pensieve checks GitHub and offers to install a newer version.' : 'Compares this version with the latest release on GitHub.'}</div></div>
        <div><button class="btn" id="updcheck">Check now</button></div></div>
      <div class="set-row"><div><div class="k">Check for updates automatically</div><div class="d">Pensieve checks GitHub once a day and asks before installing.</div></div>
        <div><label class="tog"><input type="checkbox" data-upd ${S.set.orig.auto_update_check !== false ? 'checked' : ''}></label></div></div></div>
    <div class="set-card danger" id="set-Reset"><h3>Reset Pensieve</h3><div id="resetbox"></div></div>`;
  else body = bar('Advanced', 'Fine-tuning for indexing and the local language model.', true) + groups.filter(([g]) => g !== 'Mac app').map(card).join('') + `
    <div class="set-card" id="set-Index"><h3>Index</h3><div class="set-stats">${kpi('Files', st.files)}${kpi('Repositories', st.repos)}${kpi('Code chunks', st.code_chunks)}${kpi('Agent sessions', st.sessions)}</div>
      <p class="sc-p" style="margin:10px 0 0">${esc(st.message || '')}${st.embed ? ` · embeddings: ${esc(st.embed)}` : ''}${st.llm ? ` · model: ${esc(st.llm)}` : ''}</p></div>`;
  const prevMain = $('#setmain'), y = prevMain && prevMain.dataset.tab === tab ? prevMain.scrollTop : keep?.tab === tab ? keep.y : 0;
  el.innerHTML = `<div class="set-sheet"><nav class="set-nav">${SET_TABS.map(([id, l, c, ic]) => `<button class="${id === tab ? 'on' : ''}" data-settab="${id}"><span class="set-ic" style="background:${c}"><svg viewBox="0 0 24 24" width="14" height="14">${ic}</svg></span>${esc(l)}</button>`).join('')}<span class="grow"></span><button class="btn set-done" data-setdone title="Close (Esc)">Done</button></nav>
    <div class="set-main" id="setmain" data-tab="${tab}">${body}</div></div>`;
  $('#setmain').scrollTop = y;
  $$('[data-settab]', el).forEach(b => b.onclick = () => setTab(b.dataset.settab));
  $$('#setmain [data-app]', el).forEach(b => b.onclick = () => applyAppearance(b.dataset.app, true));
  if (tab === 'ai') bindAISettings($('#setmain'));
  if (tab === 'agents') loadIntegrations();
  if (tab === 'general') bindGeneral();
  const upd = $('[data-upd]', el);
  if (upd) upd.onchange = async () => {
    const r = await fetch('/api/settings', {method: 'PUT', headers: {'content-type': 'application/json'}, body: JSON.stringify({auto_update_check: upd.checked})});
    if (r.ok) { if (S.set) S.set.orig.auto_update_check = S.set.draft.auto_update_check = upd.checked; toast(upd.checked ? 'Pensieve will check for updates daily' : 'Automatic update checks are off'); }
    else { upd.checked = !upd.checked; toast('Could not save: ' + ((await r.json().catch(() => ({}))).detail || r.status)); }
  };
  $('[data-setdone]', el).onclick = () => setView(S.prevView || 'code');
  const D = S.set.draft, again = () => renderSettings();
  $$('[data-sk]', el).forEach(i => i.onchange = () => {
    const k = i.dataset.sk, d = S.set.defaults[k];
    D[k] = typeof d === 'boolean' ? i.checked : typeof d === 'number' ? (i.value === '' ? i.value : Number(i.value)) : i.value;
    again();
  });
  $$('[data-sobj]', el).forEach(i => i.onchange = () => { D[i.dataset.sobj] = {...D[i.dataset.sobj], [i.dataset.n]: i.checked}; again(); });
  $$('[data-li]', el).forEach(i => i.onchange = () => { D[i.dataset.li] = D[i.dataset.li].map((x, j) => j === +i.dataset.i ? i.value.trim() : x).filter(Boolean); again(); });
  $$('[data-lrm]', el).forEach(b => b.onclick = () => { D[b.dataset.lrm] = D[b.dataset.lrm].filter((_, j) => j !== +b.dataset.i); again(); });
  const addTo = k => { const i = $(`[data-ladd-in="${k}"]`, el), v = i.value.trim(); if (v && !D[k].includes(v)) { D[k] = [...D[k], v]; again(); } };
  $$('[data-ladd]', el).forEach(b => b.onclick = () => addTo(b.dataset.ladd));
  $$('[data-ladd-in]', el).forEach(i => i.onkeydown = e => { if (e.key === 'Enter') { e.preventDefault(); addTo(i.dataset.laddIn); } });
  $$('#setmain > .set-card [data-copy]', el).forEach(b => b.onclick = () => { navigator.clipboard?.writeText(b.dataset.copy); toast('Copied'); });
  $('#setreset')?.addEventListener('click', () => { S.set.draft = structuredClone(S.set.orig); S.set.err = S.set.errKey = null; again(); });
  $('#setsave')?.addEventListener('click', saveSettings);
  renderSources(); renderScopes();
}
const SET_LABEL = {exclude: 'Skip folders named', exclude_files: 'Skip files matching', max_file_mb: 'Largest document (MB)', skip_temp_sessions: 'Skip sessions in temp folders', sweep_roots: 'Look for repos under', sweep_depth: 'Folder depth', max_repo_files: 'Largest swept repo (files)', repos: 'Extra repos', hotkey: 'Search shortcut', editor: 'Open code in', llm_url: 'Model server URL', llm_model: 'Model'};
async function saveSettings() {
  const ks = setDirty(); if (!ks.length) return;
  const body = Object.fromEntries(ks.map(k => [k, S.set.draft[k]]));
  const r = await fetch('/api/settings', {method: 'PUT', headers: {'content-type': 'application/json'}, body: JSON.stringify(body)});
  const j = await r.json().catch(() => ({}));
  if (!r.ok) {
    const msg = typeof j.detail === 'string' ? j.detail : JSON.stringify(j.detail || j);
    S.set.err = msg; S.set.errKey = ks.find(k => msg.startsWith(k + ' ') || msg.includes(`'${k}'`)) || (ks.length === 1 ? ks[0] : null);
  } else {
    S.set.orig = j.settings; S.set.draft = structuredClone(j.settings); S.set.err = S.set.errKey = null;
    toast(`Saved ${ks.join(', ')}`);
  }
  if (S.view === 'settings') renderSettings();
}

/* ============================== deep links (#open=, #view=, #settings) ============================== */
const VIEW_ALIAS = {sessions: 'map', session: 'map', map: 'map', code: 'code', files: 'files', file: 'files', insights: 'insights', settings: 'settings'};
async function openItem(id) {
  const i = id.indexOf(':'), kind = id.slice(0, i), ref = id.slice(i + 1);
  if (kind === 'file') return openFile(ref);
  if (kind === 'session') return openSession(ref);
  if (kind === 'code') {
    const c = await api('/api/code/' + enc(ref)).catch(() => null);
    if (!c) return toast('That code is no longer in the index.');
    if (!S.code.repos.length) await loadRepos();
    const inAll = S.code.repo === ALL && S.view === 'code';
    if (S.view !== 'data') await setView('data');
    if (!S.data.types.has('code')) { S.data.types.add('code'); dataChanged(false); }
    return openCode(+ref);
  }
  toast('Unknown item: ' + id);
}
async function route() {
  const h = location.hash.slice(1);
  if (!h) return;
  history.replaceState(null, '', location.pathname + location.search);  // so the same link works twice
  if (h === 'settings') return setView('settings');
  if (['sources', 'scopes', 'agents', 'appearance', 'advanced', 'ai'].includes(h)) {
    S.setTab = h; store.set('setTab', h);
    return S.view === 'settings' ? renderSettings() : setView('settings');
  }
  const q = new URLSearchParams(h);
  if (q.get('view')) { const vv = q.get('view'); if (LEGACY_TYPE[vv]) { setTypes(LEGACY_TYPE[vv]); await setView('data'); } else await setView(VIEW_ALIAS[vv] || 'data'); }
  if (q.get('open')) await openItem(q.get('open'));
  if (q.has('q')) await openPalette(q.get('q'), {focus: !q.get('open')});  // the Mac panel's query, without closing the item it opened
}
window.addEventListener('hashchange', route);

/* ============================== AI engine (optional: summaries, topic names, insights) ============================== */
const aiState = {data: null, timer: null, busy: null, err: null};
const AI_STATUS = {ready: ['live', 'Ready'], starting: ['busy', 'Starting…'], downloading: ['busy', 'Downloading…'], offline: ['warn', 'Not reachable'], off: ['off', 'Off'], error: ['off', 'Error']};
const aiPreparing = d => !!d && ['downloading', 'starting'].includes(d.status);
// Insights are "on" once an engine is enabled and ready; until then the tab is greyed and shows the setup page
const insightsOn = () => { const d = aiState.data; return !d || (d.enabled && !aiPreparing(d)); };
async function loadAI() {
  try { aiState.data = await api('/api/ai'); } catch { aiState.data = aiState.data || null; }
  clearTimeout(aiState.timer);
  const d = aiState.data, prep = aiPreparing(d);
  const watching = (S.view === 'settings' && S.setTab === 'ai') || S.view === 'insights';
  if (prep || watching) aiState.timer = setTimeout(async () => {  // fast while preparing; slowly while an AI view is open
    const before = JSON.stringify(aiState.data); await loadAI(); if (JSON.stringify(aiState.data) !== before) refreshAIViews();
  }, prep ? 3000 : 10000);
  syncInsightsTab();
  return d;
}
function syncInsightsTab() {
  const b = $('#tabs [data-view=insights]'); if (!b) return;
  const d = aiState.data, on = insightsOn(), prep = aiPreparing(d);
  b.classList.toggle('off', !on);
  b.title = on ? 'Insights (2)' : prep ? (d.detail || 'Preparing insights…') : 'Insights are off — click to turn them on (2)';
  const pct = prep && d.progress != null ? Math.round(d.progress * 100) : null;
  b.innerHTML = `Insights${prep ? `<i class="tab-ring" style="--p:${pct ?? 25}" ${pct == null ? 'data-spin' : ''}></i>` : !on ? '<span class="tab-off">Off</span>' : ''}`;
}
function refreshAIViews() {
  syncInsightsTab();
  if (S.view === 'settings' && S.setTab === 'ai' && !document.activeElement?.dataset?.aif) renderSettings();
  if (S.view === 'insights') renderInsights();
}
async function setAI(changes, msg) {
  aiState.err = null;
  try {
    const r = await fetch('/api/settings', {method: 'PUT', headers: {'content-type': 'application/json'}, body: JSON.stringify(changes)});
    if (!r.ok) aiState.err = (await r.json().catch(() => ({}))).detail || `Could not save (${r.status})`;
    else if (msg) toast(msg);
  } catch (e) { aiState.err = e.message; }
  aiState.busy = null; await loadAI(); refreshAIViews(); pollStatus();
}
function aiStatusHtml(d) {
  if (!d) return '';
  const [cls, label] = AI_STATUS[d.status] || ['off', d.status];
  const opt = d.options.find(o => o.id === d.provider);
  return `<div class="ai-status"><i class="dot ${cls}"></i><b>${esc(label)}</b><span class="muted">${esc(opt && d.provider !== 'none' ? opt.label : 'No engine')}${d.detail ? ' · ' + esc(d.detail) : ''}</span>${d.setting === 'auto' && d.provider && d.provider !== 'none' ? '<span class="badge">picked automatically</span>' : ''}</div>`;
}
function aiProgressHtml(d) {
  if (!aiPreparing(d) && !d?.paused) return '';
  const pct = d.progress != null ? Math.round(d.progress * 100) : null;
  return `<div class="ai-prog"><div class="ai-prog-t"><span>${esc(d.detail || (d.status === 'starting' ? 'Starting the model…' : 'Downloading…'))}</span>${pct != null ? `<b>${pct}%</b>` : ''}</div>
    <div class="ai-bar ${pct == null ? 'indet' : ''}"><i style="width:${pct ?? 30}%"></i></div>
    ${d.paused ? `<p class="ai-paused">⏸ ${esc(d.paused)}${S.set?.orig?.ai_on_battery === false || !S.set ? ' — <a href="#ai">change in Settings</a>' : ''}</p>` : '<p class="ai-paused muted">Keep working; Pensieve carries on in the background.</p>'}</div>`;
}
const INSIGHTS_ADD = ['Plain-language summaries of every session and code area', 'Topic names instead of keyword lists', 'Themes, open threads and unexplored directions across your work', 'Short summaries of what each teammate has been shipping'];
const NO_AI_WORKS = 'Maps, search, activity, keyword topics, your code footprint and team commits all work without it.';
// The calm setup page shown on the Insights tab while insights are off or getting ready
function aiOffPage() {
  const d = aiState.data;
  if (!d) return '<div class="empty"><div class="spinner" style="margin:auto"></div></div>';
  const prep = aiPreparing(d), opts = d.options.filter(o => o.id !== 'none');
  const builtin = opts.find(o => o.id === 'builtin'), others = opts.filter(o => o.id !== 'builtin');
  return `<div class="ai-off">
    <div class="ai-off-h"><span class="brand"><svg viewBox="0 0 24 24" width="34" height="34" aria-hidden="true"><circle cx="12" cy="12" r="10" fill="none" stroke="currentColor" stroke-width="1.6"/><circle cx="9" cy="10" r="1.6" fill="currentColor"/><circle cx="15" cy="9" r="1.2" fill="currentColor"/><circle cx="13" cy="15" r="1.9" fill="currentColor"/></svg></span>
      <div><h1>${prep ? 'Getting insights ready' : 'Insights are off'}</h1><p>${esc(NO_AI_WORKS)} Insights add:</p>
      <ul>${INSIGHTS_ADD.map(x => `<li>${esc(x)}</li>`).join('')}</ul></div></div>
    ${prep ? `<div class="card ai-rec">${aiProgressHtml(d)}</div>` : ''}
    ${builtin && !prep ? `<div class="card ai-rec"><div class="ai-rec-h"><div><span class="badge rec">Recommended</span><h3>Run insights in the background on this Mac</h3></div>
      <button class="btn primary" data-ai="builtin" ${builtin.available ? '' : 'disabled'}>${aiState.busy === 'builtin' ? 'Turning on…' : 'Turn on'}</button></div>
      <p>Pensieve downloads ${esc(builtin.label.replace(/^Built-in:\s*/, ''))} while you keep working${builtin.note ? ` (${esc(builtin.note.replace(/\.$/, '').replace(/^\w/, c => c.toLowerCase()))})` : ''}, then runs it at low priority. Nothing leaves this Mac.</p>
      <p class="muted">It loads only when there's work, unloads after 10 idle minutes, and pauses on battery power.</p></div>` : ''}
    ${!prep ? `<div class="ai-alt"><div class="h4" style="margin-top:4px">Or use</div>${others.map(o => `<div class="ai-alt-row ${o.available ? '' : 'na'}">
        <div class="tx"><b>${esc(o.label)}</b> <span class="priv ${o.privacy}">${o.privacy === 'cloud' ? 'Cloud' : 'Local'}</span>
          <div class="muted">${esc(o.privacy === 'cloud' ? o.description.replace(/^.*?\)\.\s*/, '') : o.description)}${o.note ? ' · ' + esc(o.note) : ''}</div></div>
        ${o.id === 'server' ? '<a class="btn" href="#ai">Set up</a>' : `<button class="btn" data-ai="${esc(o.id)}" ${o.available ? '' : 'disabled'}>${aiState.busy === o.id ? 'Saving…' : 'Use'}</button>`}</div>`).join('')}</div>` : ''}
    ${aiState.err ? `<p class="err">${esc(aiState.err)}</p>` : ''}
  </div>`;
}
function bindAIButtons(root) {
  $$('[data-ai]', root).forEach(b => b.onclick = () => {
    const id = b.dataset.ai; aiState.busy = id; refreshAIViews();
    setAI({ai: id}, id === 'none' ? 'Insights are off' : id === 'builtin' ? 'Turning on insights in the background' : 'AI engine saved');
  });
}
// Settings → AI & Insights
function aiSettingsHtml() {
  const d = aiState.data;
  if (!d) { loadAI().then(() => S.setTab === 'ai' && renderSettings()); return '<div class="set-card"><p class="sc-p">Checking…</p></div>'; }
  const bat = !!S.set?.orig?.ai_on_battery;
  return `<div class="set-card ai-set"><h3>Status</h3>${aiStatusHtml(d)}${aiProgressHtml(d)}${d.paused && !aiPreparing(d) ? `<p class="ai-paused">⏸ ${esc(d.paused)}</p>` : ''}</div>
    <div class="set-card ai-set"><h3>Engine</h3><p class="sc-p">${esc(NO_AI_WORKS)}</p>
      <div class="ai-opts" role="radiogroup" aria-label="AI engine">${d.options.filter(o => o.id !== 'none').map(o => {
        const on = d.setting === o.id || (d.setting === 'auto' && d.provider === o.id);
        if (o.id === 'server') return aiServerCard(o, on, d.setting === 'auto');
        return `<button class="ai-opt ${on ? 'on' : ''}" data-ai="${esc(o.id)}" role="radio" aria-checked="${on}" ${o.available ? '' : 'disabled'}>
          <div class="ai-opt-h"><b>${esc(o.label)}</b><span class="priv ${o.privacy}">${o.privacy === 'cloud' ? 'Cloud' : 'Local'}</span></div>
          <p>${esc(o.description)}</p>${o.note ? `<p class="note">${o.available ? '' : 'Unavailable · '}${esc(o.note)}</p>` : ''}
          ${on ? `<span class="ai-check">✓ ${d.setting === 'auto' ? 'In use (picked automatically)' : 'Selected'}</span>` : ''}</button>`;
      }).join('')}</div>${aiState.err ? `<p class="err">${esc(aiState.err)}</p>` : ''}</div>
    <div class="set-card ai-set"><div class="set-row" style="border-top:0"><div><div class="k">Keep working on battery power</div><div class="d">Off by default: background insight work pauses until your Mac is plugged in.</div></div>
      <div><label class="tog"><input type="checkbox" data-aibat ${bat ? 'checked' : ''}></label></div></div>
      <div class="set-row"><div><div class="k">Turn off insights</div><div class="d">Stops all AI work. ${esc(NO_AI_WORKS)}</div></div>
      <div><button class="btn" data-ai="none" ${d.setting === 'none' ? 'disabled' : ''}>${d.setting === 'none' ? 'Insights are off' : 'Turn off'}</button></div></div></div>`;
}
// The local-or-hosted server option: URL, model and an optional API key, with a connection test that lists models.
const isLocalUrl = u => { try { const h = new URL(u).hostname; return ['localhost', '127.0.0.1', '[::1]'].includes(h) || h.endsWith('.local'); } catch { return true; } };
function aiServerStatus(o) {
  const f = aiState.form, t = f?.test;
  if (t === 'busy') return ['', 'Checking…'];
  if (t) return t.ok ? ['ok', `Connected · ${plural(t.models.length, 'model')}`] : ['bad', t.error];
  return o.up ? ['ok', `Connected to ${o.url}`] : ['bad', `Can't reach ${o.url}`];
}
function aiServerCard(o, on, auto) {
  const f = aiState.form || (aiState.form = {url: o.url, model: o.model, key: '', test: null}), local = isLocalUrl(f.url);
  const [cls, msg] = aiServerStatus(o);
  return `<div class="ai-opt ai-srv ${on ? 'on' : ''}" role="radio" aria-checked="${on}">
    <div class="ai-opt-h"><b>${esc(o.label)}</b><span class="priv ${local ? 'local' : 'cloud'}" data-aipriv>${local ? 'Local' : 'Hosted'}</span></div>
    <p>Any OpenAI-compatible server: LM Studio or Ollama on this Mac, or a hosted API.</p>
    <label class="ai-f"><span>Server URL</span><input class="set-in" data-aif="url" value="${esc(f.url)}" placeholder="http://localhost:1234/v1" spellcheck="false" autocomplete="off"></label>
    <label class="ai-f"><span>Model</span><input class="set-in" data-aif="model" list="aimodels" value="${esc(f.model)}" placeholder="e.g. qwen/qwen3.5-9b" spellcheck="false" autocomplete="off">
      <datalist id="aimodels">${(f.test?.models || []).map(m => `<option value="${esc(m)}">`).join('')}</datalist></label>
    <label class="ai-f"><span>API key <span class="muted">· hosted servers only</span></span><input class="set-in" type="password" data-aif="key" value="${esc(f.key)}" placeholder="${o.key_set ? 'Saved · leave empty to keep it' : 'Not needed for LM Studio or Ollama'}" autocomplete="off"></label>
    <div class="ai-srv-st ${cls}" data-aist>${esc(msg)}</div>
    <div class="ai-srv-btns"><button class="btn" data-aitest>Test connection</button><button class="btn primary" data-aisave>${aiState.busy === 'server' ? 'Saving…' : on && !auto ? 'Save' : 'Use this server'}</button></div>
    ${on ? `<span class="ai-check">✓ ${auto ? 'In use (picked automatically)' : 'Selected'}</span>` : ''}</div>`;
}
function bindAIServer(root) {
  const card = $('.ai-srv', root); if (!card) return;
  const f = aiState.form, inp = k => $(`[data-aif=${k}]`, card);
  const paint = () => {  // update in place: re-rendering would lose the cursor in the field being typed in
    const o = aiState.data.options.find(x => x.id === 'server'), [cls, msg] = aiServerStatus(o), st = $('[data-aist]', card);
    st.className = 'ai-srv-st ' + cls; st.textContent = msg;
    $('#aimodels', card).innerHTML = (f.test?.models || []).map(m => `<option value="${esc(m)}">`).join('');
    const local = isLocalUrl(f.url), pv = $('[data-aipriv]', card); pv.className = 'priv ' + (local ? 'local' : 'cloud'); pv.textContent = local ? 'Local' : 'Hosted';
  };
  const test = async () => {
    if (!f.url.trim()) return;
    f.test = 'busy'; paint();
    const r = await api(`/api/ai/models?url=${enc(f.url.trim())}&key=${enc(f.key)}`).catch(e => ({ok: false, models: [], error: e.message}));
    f.test = r;
    if (r.ok && r.models.length && !r.models.includes(f.model.trim())) {  // suggest a chat model when the current one isn't served
      const pick = r.models.find(m => !/embed/i.test(m)); if (pick && !f.model.trim()) { f.model = pick; inp('model').value = pick; }
    }
    paint();
  };
  ['url', 'model', 'key'].forEach(k => inp(k).oninput = () => { f[k] = inp(k).value; if (k !== 'model') { f.test = null; paint(); } });
  inp('url').onchange = test; inp('key').onchange = test;
  $('[data-aitest]', card).onclick = test;
  $('[data-aisave]', card).onclick = () => {
    const url = f.url.trim().replace(/\/+$/, ''), model = f.model.trim();
    if (!url || !model) { aiState.err = 'Enter the server URL and a model name.'; refreshAIViews(); return; }
    const ch = {ai: 'server', llm_url: url, llm_model: model}; if (f.key) ch.llm_key = f.key;
    aiState.busy = 'server'; aiState.form = null; setAI(ch, 'AI server saved');
  };
}
function bindAISettings(root) {
  bindAIButtons(root);
  bindAIServer(root);
  const bat = $('[data-aibat]', root);
  if (bat) bat.onchange = async () => { await setAI({ai_on_battery: bat.checked}, bat.checked ? 'Insights keep working on battery' : 'Insights pause on battery'); await loadSettings(true); renderSettings(); };
}

/* ============================== views ============================== */
// Three top-level tabs: Data (code, files and agent sessions, chosen with a type switch), Insights, Settings.
// Three top-level tabs: Data (one map of documents, code and agent sessions), Insights, Settings.
// 'code', 'files' and 'map' are legacy per-type views; links to them land on Data filtered to that type.
const LEGACY_TYPE = {code: 'code', files: 'doc', file: 'doc', docs: 'doc', map: 'session', sessions: 'session', session: 'session'};
const dataType = () => 'data';
const syncTabs = v => $$('#tabs button').forEach(b => b.classList.toggle('on', b.dataset.view === (['data', 'code', 'files', 'map'].includes(v) ? 'data' : v)));
// Insights and the Data map share one context: a repo (or a folder inside one) picked on the map scopes Insights
// to that repo's sessions and codebase, and a project picked in Insights filters the map when you go back.
const dataProjKey = () => { const fr = focusedRepo(); return fr ? (fr.sub ? `${fr.name}/${fr.sub}` : fr.name) : ''; };
async function insightsFollowData() {
  if (!S.data.loaded) return;
  const k = dataProjKey();
  S.ins.ctx = k;
  if (scopeKey() !== k) { S.f.projects = new Set(k ? [k] : []); S.f.topic = null; await applyScope(); }
  if (k) { const name = k.split('/')[0]; if (S.ins.repo !== name) { S.ins.repo = name; S.ins.key = null; } }
}
function dataFollowInsights() {
  const k = scopeKey();
  if (k === (S.ins.ctx ?? k)) return;  // unchanged on the Insights side
  const [name, ...rest] = k.split('/'), key = k && !k.includes('|') ? repoDirKey(name, rest.join('/')) : null;
  if (k && !key) return;  // several projects, or one that isn't a repo on the map: leave the map as it is
  S.data.folders = new Set(key ? [key] : []); clearSel(); S.data.highlight = null;
  loadScopedTopics();
}
async function setView(v, repo) {
  if (LEGACY_TYPE[v] && v !== 'data') { setTypes(LEGACY_TYPE[v]); v = 'data'; }
  if (S.view === v && !repo) return;
  if (S.view !== 'insights' && S.view !== 'settings') cams[camKey()] = gl.saveCam();
  const prev = S.view;
  S.view = v; store.set('view', v);
  syncTabs(v);
  const page = v === 'insights' || v === 'settings';
  if (prev !== 'settings' && prev !== v) S.prevView = prev;
  document.body.dataset.view = v;  // the map stays as the backdrop; CSS shows the right floating panels
  $('#insights').hidden = v !== 'insights'; $('#settings').hidden = v !== 'settings';
  if (prev !== v) { $('#drawer').classList.remove('open'); S.selected = null; selIdx = -1; }
  if (v === 'code') {
    if (!S.code.repos.length) await loadRepos();
    if (repo && repo !== S.code.repo || !S.code.points.length) await loadRepo(repo || S.code.repo);
  }
  if (v === 'data' && prev === 'insights') dataFollowInsights();
  if (v === 'data' && (!S.data.loaded || S.data.stale)) { $('#loading').classList.remove('gone'); $('#loadingtext').textContent = 'Loading your map…'; await loadData(); }
  if (v === 'files' && (!S.files.loaded || S.files.stale)) await loadFiles();
  if (v === 'files') $('#loadingtext').textContent = S.files.points.length ? '' : 'Indexing your files — the map appears once the first batch is laid out…';
  if (!page) { renderToolbar(); buildMap(); gl.loadCam(cams[camKey()]); if (!cams[camKey()]) gl.fit(null, true); }
  renderSidebar(); renderTimeline();
  if (v === 'insights') { await insightsFollowData(); renderInsights(); loadAI().then(refreshAIViews); }
  if (v === 'settings') renderSettings();
}
$$('#tabs button').forEach(b => b.onclick = () => setView(b.dataset.view === 'data' ? dataType() : b.dataset.view));

/* ============================== insights ============================== */
async function loadInsights() {
  const key = scopeKey();
  try { S.insights = {...await api('/api/insights?scope=' + enc(key)), key}; } catch { S.insights = null; return; }
  // first visit to a scope with no insights yet: generate them once, in the background
  const st = S.insights;
  if (key && !st.data && !st.running && !S.insAuto.has(key) && S.scope?.sids?.size >= 3) { S.insAuto.add(key); regenInsights(); }
}
async function regenInsights() {
  const key = scopeKey();
  const body = key ? {scope: key, session_ids: [...S.sessions.filter(matchProj).map(s => s.id)]} : null;
  const r = await api('/api/insights', {method: 'POST', headers: {'content-type': 'application/json'}, body: JSON.stringify(body)});
  if (r.busy) toast('Already generating insights — try again in a minute');
  S.insights = {...(S.insights || {}), running: true, running_scope: key || null};
  if (S.view === 'insights') renderInsights();
}
function insRepos() {
  const repos = S.code.repos.filter(r => r.chunks && !r.all);
  if (!S.scope) return repos.filter(r => r.sessions).sort((a, b) => b.sessions - a.sessions);
  const ss = S.sessions.filter(matchProj);
  return repos.map(r => ({...r, n: ss.filter(s => matchKey(s, r.name)).length})).filter(r => r.n).sort((a, b) => b.n - a.n);
}
async function loadCodebase() {
  const repos = insRepos();
  if (!repos.some(r => r.name === S.ins.repo)) S.ins.repo = repos[0]?.name || null;
  const repo = S.ins.repo, key = repo + '|' + scopeKey();
  if (!repo || S.ins.key === key || S.ins.loading === key) return;
  S.ins.loading = key;
  const body = JSON.stringify({session_ids: S.scope ? [...S.scope.sids] : [], scope: scopeKey() || null});
  try {
    const [team, terr] = await Promise.all([api(`/api/team/${enc(repo)}`),
      api(`/api/territory/${enc(repo)}`, {method: 'POST', headers: {'content-type': 'application/json'}, body}), loadGaps(repo)]);
    if (S.ins.loading !== key) return;
    Object.assign(S.ins, {team, terr, key});
  } catch { S.ins.key = key; S.ins.team = S.ins.terr = null; }
  finally { if (S.ins.loading === key) S.ins.loading = null; }
}
function renderInsights() {
  const el = $('#insights');
  if (!insightsOn()) { el.innerHTML = aiOffPage(); bindAIButtons(el); return; }
  const ss = filtered(), all = S.sessions;
  if (!all.length) { el.innerHTML = '<div class="empty">No sessions indexed yet.</div>'; return; }
  if (S.insights?.key !== scopeKey()) { loadInsights().then(() => S.view === 'insights' && renderInsights()); }
  const repo = insRepos().some(r => r.name === S.ins.repo) ? S.ins.repo : insRepos()[0]?.name;
  if (repo && S.ins.key !== repo + '|' + scopeKey() && !S.ins.loading) loadCodebase().then(() => S.view === 'insights' && renderInsights());
  const now = Date.now();
  const days = new Set(ss.map(s => new Date(s.t0).toDateString())).size;
  const w1 = ss.filter(s => s.t1 > now - 7 * DAY).length, w0 = ss.filter(s => s.t1 > now - 14 * DAY && s.t1 <= now - 7 * DAY).length;
  const moments = ss.reduce((a, s) => a + (s.n || 0), 0);
  const med = [...ss.map(s => s.n || 0)].sort((a, b) => a - b)[Math.floor(ss.length / 2)] || 0;
  const projs = new Set(ss.map(s => s.project)).size;
  const fresh = S.insights?.key === scopeKey();
  const ins = fresh ? S.insights?.data : null;
  const running = (S.insights?.running || S.status.insights_running) && (S.insights?.running_scope || S.status.insights_scope || '') === scopeKey();
  const busyElsewhere = (S.insights?.running || S.status.insights_running) && !running;
  const kpi = (l, v, d = '') => `<div class="kpi"><div class="l">${l}</div><div class="v">${v}</div><div class="d">${d}</div></div>`;
  const delta = w1 - w0;
  const scopeName = S.f.projects.size ? [...S.f.projects].map(keyLabel).join(', ') : '';
  el.innerHTML = `<div class="ins-wrap">
    <div class="ins-head"><div>
      <div class="muted" style="font-size:12px;display:flex;gap:8px;align-items:center;flex-wrap:wrap">${scopeName ? `<span class="scope-chip">▣ ${esc(scopeName)}</span>` : '<span class="scope-chip">All projects</span>'}${plural(ss.length, 'session')} · ${ss.length ? fmtDY(Math.min(...ss.map(s => s.t0))) + ' – ' + fmtDY(Math.max(...ss.map(s => s.t1))) : ''}</div>
      <h1>${ins?.headline ? esc(ins.headline) : running ? '<span class="shimmer">Reading through these sessions to find patterns…</span>' : scopeName ? `Your work in ${esc(scopeName)}` : 'Your AI work at a glance'}</h1></div>
      <button class="btn" id="regen" ${running || busyElsewhere ? 'disabled' : ''}>${running ? '<span class="spinner" style="width:12px;height:12px;border-width:2px"></span> Thinking…' : busyElsewhere ? 'Busy with another scope…' : ins ? '✦ Regenerate insights' : '✦ Generate insights'}</button></div>
    <div class="kpis">
      ${kpi('Sessions', ss.length.toLocaleString(), `${plural(projs, 'project')}`)}
      ${kpi('Last 7 days', w1, `<span style="color:${delta > 0 ? 'var(--good)' : delta < 0 ? 'var(--critical)' : 'inherit'}">${delta > 0 ? '▲' : delta < 0 ? '▼' : '•'} ${Math.abs(delta)}</span> vs previous week`)}
      ${kpi('Active days', days, ss.length ? `${(ss.length / Math.max(1, days)).toFixed(1)} sessions per active day` : '')}
      ${kpi('Conversation moments', moments.toLocaleString(), `median ${med} per session`)}
      ${S.ins.terr && S.ins.key === (repo + '|' + scopeKey()) ? kpi(`Code explored · ${esc(repo)}`, Math.round(S.ins.terr.coverage * 100) + '%', `${S.ins.terr.footprint.filter(f => f.sessions).length} of ${S.ins.terr.footprint.length} areas`) : kpi('Agents', new Set(ss.map(s => s.source)).size, [...new Set(ss.map(s => AGENT_NAME[s.source] || s.source))].join(' · '))}
    </div>
    <div class="grid2">
      <div class="card"><h3>Activity by topic</h3><p class="cap">Sessions per ${actBinLabel(ss)}, stacked by ${S.scope ? 'this scope’s topics' : 'topic'}. Click a bar to filter the map to that period.</p><div class="legend-row" id="actleg"></div><svg id="actchart" class="chart" height="220"></svg></div>
      <div class="card"><h3>When you work</h3><p class="cap">Sessions started by weekday and hour (local time).</p><svg id="heat" class="chart" height="220"></svg></div>
    </div>
    ${aiState.data && ['offline', 'error'].includes(aiState.data.status) ? `<div class="card ai-setup" style="margin-bottom:12px">${aiStatusHtml(aiState.data)}<p class="cap" style="margin:6px 0 0">Insight work pauses until the engine is reachable. <a href="#ai">Change the AI engine</a></p></div>` : ''}
    ${aiSection(ins, running, scopeName)}
    ${codebaseSection(repo)}
    <div class="grid2">
      <div class="card"><h3>Topics${S.scope ? ' in this scope' : ''}</h3><p class="cap">Click a topic to explore it on the map.</p><div id="topicbars"></div></div>
      <div class="card"><h3>Projects</h3><p class="cap">Click a project to filter everything to it.</p><div style="overflow:auto"><table class="tbl" id="projtbl"></table></div></div>
    </div></div>`;
  $('#regen').onclick = regenInsights;
  drawActivity(ss); drawHeat(ss); drawTopicBars(ss); drawProjTable(ss);
  $$('[data-sid]', el).forEach(b => b.onclick = () => openSession(b.dataset.sid));
  $$('[data-tid]', el).forEach(b => b.onclick = () => toggleTopic(+b.dataset.tid));
  $$('[data-findq]', el).forEach(b => b.onclick = () => openPalette(b.dataset.findq));
  $$('[data-area]', el).forEach(b => b.onclick = () => gotoArea(b.dataset.repo || repo, b.dataset.area));
  $$('[data-author]', el).forEach(b => b.onclick = e => { e.stopPropagation(); gotoCode(b.dataset.repo || repo, null, b.dataset.author); });
  $$('[data-insrepo]', el).forEach(b => b.onclick = () => { S.ins.repo = b.dataset.insrepo; S.ins.key = null; renderInsights(); });
  bindGapLinks(el);
  $$('.frow', el).forEach(b => { b.onmousemove = e => tip(b.dataset.tip, e.clientX, e.clientY); b.onmouseleave = () => tip(null); });
}
const actBin = ss => { const span = ss.length ? Math.max(...ss.map(s => s.t1)) - Math.min(...ss.map(s => s.t0)) : 0; return span < 45 * DAY ? DAY : 7 * DAY; };
const actBinLabel = ss => actBin(ss) === DAY ? 'day' : 'week';
function aiSection(ins, running, scopeName) {
  const where = scopeName ? `the sessions in ${esc(scopeName)}` : 'all sessions';
  if (!ins) return `<div class="card" style="margin-bottom:12px"><h3>AI insights</h3><p class="cap">${running ? 'Generating with ' + esc(S.status.llm || 'the local model') + ` from ${where} — this takes about a minute.` : (S.status.summarized_sessions < S.status.sessions ? 'Writing insights in the background… they fill in as sessions are summarized.' : `Not generated for ${where} yet. Click “Generate insights”.`)}</p>${running ? '<div class="shimmer" style="height:80px"></div>' : ''}</div>`;
  const links = it => `<div class="links">${(it.topics || []).filter(t => topicOf(t)).map(t => `<button class="chip" data-tid="${t}"><i class="sw" style="background:${slotColor(t)}"></i>${esc(topicOf(t).name)}</button>`).join('')}${(it.sessions || []).filter(id => S.byId.has(id)).slice(0, 4).map(id => `<button class="chip" data-sid="${esc(id)}">${esc(S.byId.get(id).title.slice(0, 40))}</button>`).join('')}</div>`;
  const card = (ico, title, cap, items, extra) => `<div class="card ai-card"><h3><span class="ico">${ico}</span>${title}</h3><p class="cap">${cap}</p>${items.map(it => `<div class="ai-item"><b>${esc(it.title)}</b><p>${esc(it.insight)}</p>${extra ? extra(it) : ''}${links(it)}</div>`).join('')}</div>`;
  const unexplored = it => (it.first_step ? `<p class="step">${esc(it.first_step)}</p>` : '') +
    ((it.area || it.people?.length) ? `<div class="links" style="margin-bottom:4px">${it.area ? `<button class="chip" data-area="${esc(it.area)}" style="font:11px var(--mono)">⌗ ${esc(it.area)}</button>` : ''}${(it.people || []).map(n => { const a = teamAuthor(n); return a ? `<button class="chip" data-author="${esc(a)}">👤 ${esc(n)}</button>` : `<span class="chip">👤 ${esc(n)}</span>`; }).join('')}</div>` : '');
  return `<div class="ai-grid">
    ${card('◎', 'Recurring themes', 'Ideas you keep coming back to', ins.themes || [])}
    ${card('⇄', 'Unexpected connections', 'Links between topics that could reinforce each other', ins.connections || [])}
    ${card('◔', 'Open threads', 'Work that looks started but unfinished', ins.open_threads || [], it => `<button class="chip" style="margin-bottom:6px" data-findq="${esc(it.title)}" title="Search for everything related">⌕ Find related</button>`)}
    ${card('✧', 'Unexplored directions', 'Suggestions grounded in your sessions, the code you haven’t touched, and what teammates are doing', ins.unexplored || [], unexplored)}
  </div><p class="muted" style="font-size:11.5px;margin:-4px 0 14px">Insights cover ${ins.n_sessions ? plural(ins.n_sessions, 'session') + ' in ' : ''}${where}${ins.generated ? ` · generated ${rel(ins.generated * 1000)}` : ''} by ${esc(S.status.llm || 'local model')} — verify before acting.</p>`;
}
const teamAuthor = name => { const p = S.ins.team?.people.find(x => x.name === name); return p ? p.author : null; };
const initials = n => n.split(/[\s._-]+/).filter(Boolean).slice(0, 2).map(w => w[0].toUpperCase()).join('') || '?';
const kfmt = n => n >= 1e4 ? Math.round(n / 1e3) + 'k' : n >= 1e3 ? (n / 1e3).toFixed(1) + 'k' : String(n);
function codebaseSection(repo) {
  const repos = insRepos();
  if (!repos.length) return '';
  const ready = S.ins.key === repo + '|' + scopeKey() && S.ins.terr;
  const seg = repos.length > 1 ? `<div class="repo-seg">${repos.slice(0, 6).map(r => `<button class="chip ${r.name === repo ? 'on' : ''}" data-insrepo="${esc(r.name)}">${esc(r.name)}</button>`).join('')}</div>` : '';
  const head = (t, sub, right = '') => `<div class="ins-sec"><div><h2>${t}</h2><p>${sub}</p></div>${right}</div>`;
  if (!ready) return head(`Codebase · ${esc(repo)}`, 'Where you’ve worked, what’s next to it, and who is active there.', seg) + `<div class="card"><div class="shimmer" style="height:140px"></div></div>`;
  const {terr, team} = S.ins;
  const name = e => team?.people.find(p => p.email === e)?.name || e.split('@')[0];
  const ownerName = a => person(a);
  // footprint
  const foot = terr.footprint.slice(0, 18), fmax = Math.max(1, ...foot.map(f => f.chunks));
  const exploredCol = pal().cat[0], otherCol = pal().other;
  const footHtml = foot.map(f => `<button class="frow" data-area="${esc(f.area)}" data-tip="${esc(`<b>${esc(f.area)}</b><div class="m">${f.chunks.toLocaleString()} code chunks</div><p>${f.sessions ? `You worked here in ${plural(f.sessions, 'session')}` : 'Not touched in your sessions yet'}${f.recent_commits ? ` · ${plural(f.recent_commits, 'commit')} by teammates in 90 days` : ''}${f.owners.length ? `<br>Owners: ${f.owners.map(ownerName).map(esc).join(', ')}` : ''}</p>`)}">
      <span class="a" title="${esc(f.area)}">&lrm;${esc(f.area)}</span><span><i class="b" style="display:block;width:${f.chunks / fmax * 100}%;background:${f.sessions ? exploredCol : otherCol}"></i></span><span class="r">${f.sessions ? plural(f.sessions, 'session') : '—'}</span></button>`).join('');
  const sugg = terr.suggestions.map(x => {
    const recent = x.recent_people.map(e => team?.people.find(p => p.email === e)).filter(Boolean);
    const people = recent.length ? recent.map(p => [p.author, p.name]) : x.owners.map(a => [a, ownerName(a)]);
    return `<div class="sg"><div class="h"><code>${esc(x.area)}</code><span class="muted" style="font-size:11.5px">${x.chunks.toLocaleString()} chunks</span></div>
      <div class="why">${x.near ? `Next to <a href="#" data-area="${esc(x.near)}"><code>${esc(x.near)}</code></a>, where you’ve worked (${Math.round(x.near_sim * 100)}% similar). ` : ''}${x.recent_commits ? `Active: ${plural(x.recent_commits, 'file change')} in the last 90 days.` : `Quiet: no teammate changes in 90 days${x.last_ts ? `, last touched ${rel(x.last_ts * 1000)}` : ''}.`}</div>
      <div class="acts">${people.slice(0, 3).map(([a, n]) => `<button class="chip" data-author="${esc(a)}" title="Open in Code view">👤 ${esc(n)}</button>`).join('')}
        <button class="chip" data-area="${esc(x.area)}">⌗ Explore</button></div></div>`;
  }).join('') || '<div class="empty">Nothing obvious — you’ve touched every sizeable area.</div>';
  // team
  const myAreas = terr.footprint.filter(f => f.sessions).map(f => f.area);
  const shared = a => myAreas.some(m => m === a || m.startsWith(a + '/') || a.startsWith(m + '/'));
  const ppl = (team?.people || []).slice(0, 12);
  const sums = team?.summaries || {};
  const card = p => {
    const wmax = Math.max(1, ...p.weeks), sm = sums[p.email];
    return `<div class="pcard">
      <div class="top"><span class="av" style="background:${p.me ? 'var(--accent)' : 'var(--text-3)'}">${esc(initials(p.name))}</span>
        <span class="nm"><a href="#" data-author="${esc(p.author)}">${esc(p.name)}</a>${p.me ? ' <span class="badge">You</span>' : ''}</span>
        <span class="st" title="${p.added.toLocaleString()} lines added, ${p.deleted.toLocaleString()} removed">${plural(p.commits, 'commit')} · +${kfmt(p.added)} −${kfmt(p.deleted)}</span></div>
      <div class="spark" title="Commits per week, last ${team.days} days (oldest → newest)">${p.weeks.map(w => `<i class="${w ? '' : 'z'}" style="height:${w ? Math.max(12, w / wmax * 100) : 8}%"></i>`).join('')}</div>
      ${sm ? `<div class="focus">${esc(sm.focus)}</div><p class="sum">${esc(sm.summary)}</p>` : team.summarizing ? `<div class="shimmer" style="height:34px;border-radius:6px"></div>` : ''}
      <div class="areas">${p.areas.map(a => `<button class="chip ${shared(a.area) ? 'shared' : ''}" data-area="${esc(a.area)}" title="${a.commits} commits${shared(a.area) ? ' · you’ve worked here too' : ''}">${esc(a.area)}</button>`).join('')}</div>
      <details><summary>Recent commits · last ${rel(p.last * 1000)}</summary><ul>${p.subjects.slice(0, 6).map(c => `<li>${esc(c.subject)} <span class="muted">${rel(c.ts * 1000)}</span></li>`).join('')}</ul></details>
    </div>`;
  };
  return head(`Codebase · ${esc(repo)}`, `You’ve touched ${Math.round(terr.coverage * 100)}% of the code — ${terr.footprint.filter(f => f.sessions).length} of ${terr.footprint.length} areas across ${plural(terr.sessions, 'session')}${S.scope ? ' in this scope' : ''}.`, seg) +
    `<div class="grid2">
      <div class="card"><h3>Your footprint</h3><p class="cap">Largest areas of ${esc(repo)}; filled bars are areas your sessions read or edited. Click an area to open it in Code.</p>
        <div class="legend-row"><span><i class="sw" style="background:${exploredCol}"></i>Worked in</span><span><i class="sw" style="background:${otherCol}"></i>Not yet</span></div><div class="foot">${footHtml}</div></div>
      <div class="card"><h3>Unexplored territory</h3><p class="cap">Areas you haven’t touched, ranked by how close they are to code and conversations you have — with who to talk to.</p><div class="sugg">${sugg}</div></div>
    </div>` +
    ((g => g && g.gaps.length ? `<div class="card gaps-card" style="margin-bottom:12px"><div class="gaps-head"><div><h3>Knowledge gaps</h3><p class="cap">Areas mostly written by people with no commits in ${esc(repo)} for ${g.inactive_days}+ days — who to ask instead.</p></div><button class="btn" data-gaps="${esc(repo)}">See all ${g.gaps.length}</button></div>${g.gaps.slice(0, 3).map(x => gapItemHtml(x, repo)).join('')}</div>` : '')(peekGaps(repo))) +
    head('Team activity', `What people have been shipping in ${esc(repo)} over the last ${team?.days || 90} days${team?.summarizing ? ' · summarizing with ' + esc(S.status.llm || 'the local model') + '…' : ''}. Highlighted areas overlap with yours.`) +
    `<div class="team" style="margin-bottom:12px">${ppl.map(card).join('') || '<div class="empty">No commits in this window.</div>'}</div>`;
}
function drawActivity(ss) {
  const svg = $('#actchart'); const W = svg.clientWidth || 600, H = 220, pl = 28, pb = 20, pt = 8;
  if (!ss.length) { svg.innerHTML = ''; return; }
  const bin = actBin(ss), t0 = Math.floor(Math.min(...ss.map(s => s.t0)) / bin) * bin, t1 = Math.max(...ss.map(s => s.t1));
  const nb = Math.max(1, Math.ceil((t1 - t0 + 1) / bin));
  const bins = Array.from({length: nb}, (_, i) => ({t: t0 + i * bin, by: new Map(), n: 0}));
  ss.forEach(s => { const b = bins[Math.min(nb - 1, Math.floor((s.t0 - t0) / bin))]; b.by.set(cl(s), (b.by.get(cl(s)) || 0) + 1); b.n++; });
  const max = Math.max(1, ...bins.map(b => b.n)), bw = (W - pl) / nb, ih = H - pb - pt;
  const out = [];
  const step = Math.max(1, Math.ceil(max / 3));
  for (let v = 0; v <= max; v += step) { const y = H - pb - v / max * ih; out.push(`<g class="grid"><line x1="${pl}" x2="${W}" y1="${y}" y2="${y}"/></g><text x="${pl - 6}" y="${y + 3}" text-anchor="end">${v}</text>`); }
  bins.forEach((b, i) => {
    let y = H - pb; const x = pl + i * bw + 1, w = Math.max(1, bw - 2);
    [...b.by.keys()].sort((a, c) => a - c).forEach(k => {
      const h = b.by.get(k) / max * ih;
      out.push(`<rect x="${x}" y="${y - h}" width="${w}" height="${Math.max(0, h - 1.5)}" fill="${slotColor(k)}" rx="${w > 6 ? 2 : 0}" data-i="${i}" data-k="${k}"/>`); y -= h;
    });
    out.push(`<rect x="${x - 1}" y="${pt}" width="${bw}" height="${ih}" fill="transparent" data-i="${i}" style="cursor:pointer"/>`);
  });
  const nt = Math.min(6, nb);
  for (let j = 0; j < nt; j++) { const i = Math.round(j * (nb - 1) / Math.max(1, nt - 1)); out.push(`<text x="${pl + i * bw + bw / 2}" y="${H - 5}" text-anchor="middle">${fmtD(bins[i].t)}</text>`); }
  svg.innerHTML = out.join('');
  $('#actleg').innerHTML = TOPICS().filter(t => ss.some(s => cl(s) === t.id)).map(t => `<span><i class="sw" style="background:${slotColor(t.id)}"></i>${esc(t.name)}</span>`).join('');
  svg.onmousemove = e => {
    const i = e.target.dataset?.i; if (i == null) return tip(null);
    const b = bins[+i];
    tip(`<b>${bin === DAY ? fmtDY(b.t) : 'Week of ' + fmtD(b.t)}</b><div class="m">${plural(b.n, 'session')}</div><table>${[...b.by.entries()].sort((a, c) => c[1] - a[1]).map(([k, n]) => `<tr><td><i class="sw" style="background:${slotColor(k)}"></i> ${esc(topicOf(k)?.name || 'Other')}</td><td>${n}</td></tr>`).join('')}</table>`, e.clientX, e.clientY);
  };
  svg.onmouseleave = () => tip(null);
  svg.onclick = e => { const i = e.target.dataset?.i; if (i == null) return; const b = bins[+i]; S.f.range = [b.t, b.t + bin - 1]; refresh(); };
}
function drawHeat(ss) {
  const svg = $('#heat'); const W = svg.clientWidth || 400, H = 220, pl = 30, pb = 18;
  const g = Array.from({length: 7}, () => new Array(24).fill(0));
  ss.forEach(s => { const d = new Date(s.t0); g[(d.getDay() + 6) % 7][d.getHours()]++; });
  const max = Math.max(1, ...g.flat()), cw = (W - pl) / 24, ch = (H - pb) / 7;
  const days = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
  const out = [];
  g.forEach((row, r) => {
    out.push(`<text x="${pl - 6}" y="${r * ch + ch / 2 + 3}" text-anchor="end">${days[r]}</text>`);
    row.forEach((v, c) => out.push(`<rect x="${pl + c * cw + 1}" y="${r * ch + 1}" width="${cw - 2}" height="${ch - 2}" rx="3" fill="${v ? seqColor(Math.sqrt(v / max)) : pal().heat0}" data-r="${r}" data-c="${c}"/>`));
  });
  [0, 6, 12, 18, 23].forEach(h => out.push(`<text x="${pl + h * cw + cw / 2}" y="${H - 4}" text-anchor="middle">${h}h</text>`));
  svg.innerHTML = out.join('');
  svg.onmousemove = e => { const r = e.target.dataset?.r; if (r == null) return tip(null); const c = +e.target.dataset.c; tip(`<b>${days[r]} ${String(c).padStart(2, '0')}:00</b><div class="m">${plural(g[r][c], 'session')} started</div>`, e.clientX, e.clientY); };
  svg.onmouseleave = () => tip(null);
}
function drawTopicBars(ss) {
  const cnt = {}, last = {}; ss.forEach(s => { const c = cl(s); cnt[c] = (cnt[c] || 0) + 1; last[c] = Math.max(last[c] || 0, s.t1); });
  const max = Math.max(1, ...Object.values(cnt));
  $('#topicbars').innerHTML = `<div class="rows">${TOPICS().filter(t => cnt[t.id]).sort((a, b) => cnt[b.id] - cnt[a.id]).map(t => `
    <button class="row" data-tid="${t.id}"><i class="sw" style="background:${slotColor(t.id)}"></i><span class="name"><b>${esc(t.name)}</b></span><span class="n">${cnt[t.id]} · ${rel(last[t.id])}</span>
    <span class="sub">${esc(t.description || t.keywords)}</span><span class="bar"><i style="width:${cnt[t.id] / max * 100}%;background:${slotColor(t.id)}"></i></span></button>`).join('')}</div>`;
}
function drawProjTable(ss) {
  const by = new Map();
  ss.forEach(s => { const r = by.get(s.project) || {n: 0, m: 0, last: 0, topics: {}, agents: new Set()}; r.n++; r.m += s.n || 0; r.last = Math.max(r.last, s.t1); r.topics[cl(s)] = (r.topics[cl(s)] || 0) + 1; r.agents.add(s.source); by.set(s.project, r); });
  $('#projtbl').innerHTML = `<tr><th>Project</th><th class="num">Sessions</th><th class="num">Moments</th><th>Main topic</th><th>Last active</th></tr>` +
    [...by].sort((a, b) => b[1].n - a[1].n).map(([p, r]) => {
      const top = Object.entries(r.topics).sort((a, b) => b[1] - a[1])[0]?.[0];
      const t = topicOf(+top);
      return `<tr class="click" data-p="${esc(p)}"><td><b>${esc(p)}</b> <span class="muted" style="font-size:11px">${[...r.agents].map(a => AGENT_NAME[a] || a).join(', ')}</span></td><td class="num">${r.n}</td><td class="num">${r.m}</td><td>${t ? `<i class="sw" style="background:${slotColor(t.id)}"></i> ${esc(t.name)}` : ''}</td><td class="muted">${rel(r.last)}</td></tr>`;
    }).join('');
  $$('#projtbl tr[data-p]').forEach(tr => tr.onclick = () => { S.f.projects = new Set([tr.dataset.p]); refresh(); });
}

/* ============================== command palette ============================== */
const pl = {items: [], sel: 0, q: '', seq: 0};
// Search lives at the top of the left panel: a query swaps the filters for results; clearing it brings them back.
async function openPalette(prefill = null, {focus = true} = {}) {
  if (S.view === 'settings') await setView(dataType());
  toggleSide(true);
  const i = $('#palin');
  if (prefill != null) i.value = prefill;
  if (focus) { i.focus(); i.select(); }
  palSearch();
}
function closePalette() { const i = $('#palin'); if (i.value) { i.value = ''; palSearch(); } i.blur(); }
const palFind = debounce(async (q, seq) => {
  try {
    const r = await api(`/api/find?limit=40&q=${enc(q)}&scope=${enc(palScope())}`);
    if (seq !== pl.seq) return;
    pl.found = r; palRender(); setDataSearch(r.results || []);
  } catch { if (seq === pl.seq) { pl.found = {results: []}; palRender(); } }
}, 160);
function palSearch() {
  pl.q = $('#palin').value; pl.seq++; pl.found = null; pl.sel = 0;
  const on = !!pl.q.trim();
  document.body.classList.toggle('searching', on); $('#palres').hidden = !on;
  if (!on) { pl.items = []; $('#palres').innerHTML = ''; setDataSearch(null); return; }
  palRender();
  const q = pl.q.trim();
  if (q && !q.startsWith('who:') && !q.startsWith('>') && q.replace(/[^\w]/g, '').length >= 2) palFind(q, pl.seq);
}
// snippet with server-provided highlight ranges, escaped piecewise
function hlSnip(t, hl) {
  if (!t) return '';
  let out = '', at = 0;
  for (const [a, b] of (hl || []).filter(([a, b]) => b > a).sort((x, y) => x[0] - y[0])) { if (a < at) continue; out += esc(t.slice(at, a)) + '<mark>' + esc(t.slice(a, b)) + '</mark>'; at = b; }
  return out + esc(t.slice(at));
}
const FIND_GROUP = {file: 'Files', code: 'Code', session: 'Agent sessions'};
const FIND_IC = {file: '▤', code: '⌗', session: '◌'};
const FOLDER_IC = '<svg viewBox="0 0 24 24" width="15" height="15"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2Z" fill="currentColor" opacity=".85"/></svg>';
function findItem(r) {
  const ex = r.match === 'exact' || r.match === 'both';
  if (r.is_dir) return {ic: FOLDER_IC, title: r.title, titleHtml: esc(r.title) + '<span class="ex">folder</span>', subHtml: `<span style="font-size:11.5px">${esc(r.subtitle)}</span>`, rt: 'Reveal', run: () => openOnMac(r.id, 'reveal')};
  return {ic: FIND_IC[r.kind], title: r.title + (r.line && r.kind === 'code' ? `:${r.line}` : ''), titleHtml: esc(r.title) + (r.kind === 'code' && r.line ? `<span class="muted">:${r.line}</span>` : '') + (ex ? '<span class="ex">exact</span>' : ''),
          subHtml: `<span style="font-family:var(--mono);font-size:11px">${esc(r.subtitle)}</span>${r.snippet ? ' — ' + hlSnip(r.snippet, r.highlights) : ''}`, run: () => openItem(r.id)};
}
function palRender() {
  const raw = pl.q.trim(), q = raw.toLowerCase(), items = [];
  const add = (group, it) => items.push({group, ...it});
  if (raw.startsWith('who:')) {
    const t = raw.slice(4).trim();
    add('Code', {ic: '👤', title: t ? `Who knows about “${t}”?` : 'Type a topic, e.g. who: billing retries', sub: `Ranks people by blame on the most relevant code in ${S.code.repo ? repoLabel(S.code.repo) : 'your repos'}`, run: () => t && whoKnows(t)});
  } else if (raw) {
    if (pl.found) {
      const rs = pl.found.results || [];
      const sc = pl.found.scope;
      if (sc && sc.name !== 'all') add('Scope', {ic: '◎', title: `Searching in ${sc.name}`, sub: sc.description || '', rt: 'change ▸', run: () => { $('#palscope').focus(); $('#palscope').showPicker?.(); }});
      rs.filter(r => r.is_dir).slice(0, 4).forEach(r => add('Folders', findItem(r)));
      for (const k of ['file', 'code', 'session']) rs.filter(r => r.kind === k && !r.is_dir).slice(0, 6).forEach(r => add(FIND_GROUP[k], findItem(r)));
      if (!rs.length) add('Search', {ic: '∅', title: 'No matching files, code or sessions', sub: pl.found.query?.exact?.length ? 'Exact phrases must appear word for word; try fewer quotes.' : 'Try other words, or "quotes" for exact text.', run: () => {}});
    } else {
      const local = S.sessions.filter(s => (s.title + ' ' + (s.summary || '') + ' ' + s.project + ' ' + (s.tags || '')).toLowerCase().includes(q)).slice(0, 4);
      local.forEach(s => add('Agent sessions', sessItem(s)));
      add('Search', {ic: '…', title: 'Searching files, code and sessions…', sub: '', run: () => {}});
    }
    { const T = dataTree(), keys = [...T.roots, ...[...T.children.values()].flat()]; keys.filter(k => k.split('/').pop().toLowerCase().includes(q)).sort((x, y) => x.length - y.length).slice(0, 4).forEach(k => add('Folders', {ic: FOLDER_IC, title: k.split('/').pop(), sub: '~/' + k + (S.data.repoKeys.has(k) ? ' · git repository' : ''), run: () => setView('data').then(() => toggleDataFolder(k, true))})); }
    if (S.code.points.length) S.code.people.filter(p => p.author.toLowerCase().includes(q)).slice(0, 3).forEach(p => add('People', {ic: '👤', title: person(p.author), sub: `${(p.share * 100).toFixed(1)}% of ${repoLabel(S.code.repo)} · ${p.dirs.slice(0, 2).join(', ')}`, run: () => setView('data').then(() => openPerson(p.author))}));
    add('People', {ic: '👤', title: `Who knows about “${raw.replace(/"/g, '')}”?`, sub: 'Find the people behind the most relevant code', run: () => whoKnows(raw.replace(/"/g, ''))});
  } else {
    S.sessions.slice().sort((a, b) => b.t1 - a.t1).slice(0, 5).forEach(s => add('Recent sessions', sessItem(s)));
  }
  const cmds = [['Show code', '1', () => setView('code')], ['Show files', '', () => setView('files')], ['Show agent sessions', '', () => setView('map')], ['Go to Insights', '2', () => setView('insights')], ['Open Settings', '3', () => setView('settings')],
    ['Toggle light / dark', 'T', toggleTheme], ['Clear all filters', '', clearFilters], ['Fit map to view', 'F', () => gl.fit(visibleIdx())], ['Keyboard shortcuts', '?', () => ($('#helpmodal').hidden = false)]];
  cmds.filter(([t]) => !q || t.toLowerCase().includes(q.replace(/^>/, '').trim())).slice(0, raw ? 3 : 7).forEach(([t, k, run]) => add('Commands', {ic: '›', title: t, rt: k, run}));
  pl.items = items; pl.sel = Math.min(pl.sel, items.length - 1);
  let g = '';
  $('#palres').innerHTML = items.map((it, i) => `${it.group !== g ? `<div class="pal-group">${(g = it.group)}</div>` : ''}<div class="pal-item ${i === pl.sel ? 'sel' : ''}" data-i="${i}"><span class="ic">${it.ic.startsWith('<') ? it.ic : esc(it.ic)}</span><div class="tx"><b>${it.titleHtml || esc(it.title)}</b>${it.subHtml ? `<small>${it.subHtml}</small>` : it.sub ? `<small>${esc(it.sub)}</small>` : ''}</div>${it.rt ? `<span class="rt">${it.rt.startsWith('<') ? it.rt : esc(it.rt)}</span>` : ''}</div>`).join('') || '<div class="empty">No results</div>';
  $$('.pal-item', $('#palres')).forEach(el => { el.onmouseenter = () => { pl.sel = +el.dataset.i; $$('.pal-item').forEach(x => x.classList.toggle('sel', x === el)); }; el.onclick = () => palRun(+el.dataset.i); });
}
const sessItem = s => ({ic: `<i class="sw" style="background:${slotColor(cl(s))}"></i>`, title: s.title, sub: s.summary || '', rt: `${esc(s.project)} · ${rel(s.t1)}`, run: () => openSession(s.id)});
function palRun(i) {  // results stay so you can open the next one; on phones the panel gets out of the way
  const it = pl.items[i]; if (!it) return;
  if (innerWidth < 860) toggleSide(false);
  it.run();
}
$('#palin').addEventListener('input', palSearch);
$('#palscope').addEventListener('change', () => { $('#palscope').dataset.touched = '1'; $('#palin').focus(); palSearch(); });
$('#palin').addEventListener('keydown', e => {
  if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
    e.preventDefault(); pl.sel = (pl.sel + (e.key === 'ArrowDown' ? 1 : -1) + pl.items.length) % pl.items.length;
    $$('.pal-item').forEach((x, j) => x.classList.toggle('sel', j === pl.sel)); $('.pal-item.sel')?.scrollIntoView({block: 'nearest'});
  } else if (e.key === 'Enter') {
    e.preventDefault();
    palRun(pl.sel);
  } else if (e.key === 'Escape') { e.stopPropagation(); closePalette(); }
});

/* ============================== theme, keys, status ============================== */
const APPEARANCES = ['system', 'light', 'dark'];
let appearance = (() => { try { const t = localStorage.getItem('pensieve.theme'); return APPEARANCES.includes(t) ? t : 'system'; } catch { return 'system'; } })();
function applyAppearance(mode, persist = false) {
  if (!APPEARANCES.includes(mode)) mode = 'system';
  appearance = mode;
  if (mode === 'system') delete document.documentElement.dataset.theme; else document.documentElement.dataset.theme = mode;
  try { localStorage.setItem('pensieve.theme', mode); } catch {}
  gl.setTheme(); recolor(); renderSidebar(); renderTimeline(); syncThemeLabel();
  if (S.view === 'insights') renderInsights(); if (S.view === 'settings') renderSettings();
  if (persist) fetch('/api/settings', {method: 'PUT', headers: {'content-type': 'application/json'}, body: JSON.stringify({appearance: mode})}).catch(() => {});
}
// T flips between light and dark from whatever is showing now
const toggleTheme = () => applyAppearance(theme() === 'dark' ? 'light' : 'dark', true);
const syncThemeLabel = () => $$('[data-app]').forEach(b => b.classList.toggle('on', b.dataset.app === appearance));
$$('#appseg [data-app]').forEach(b => b.onclick = () => applyAppearance(b.dataset.app, true));
async function loadAppearance() {
  try { const r = await api('/api/settings'); if (APPEARANCES.includes(r.settings.appearance) && r.settings.appearance !== appearance) applyAppearance(r.settings.appearance); } catch {}
}
// Fit and auto-rotate sit beside the ••• menu, on every map view
const syncRotateBtn = () => { const b = $('#rotbtn'); b.classList.toggle('on', gl.controls.autoRotate); b.setAttribute('aria-pressed', String(gl.controls.autoRotate)); };
$('#fitbtn').onclick = () => gl.fit(visibleIdx());
$('#rotbtn').onclick = () => { gl.controls.autoRotate = !gl.controls.autoRotate; store.set('autorotate', gl.controls.autoRotate); syncRotateBtn(); };
syncRotateBtn();
function closeMenu() { $('#gearmenu').hidden = true; $('#gearbtn').setAttribute('aria-expanded', 'false'); }
$('#gearbtn').onclick = e => { e.stopPropagation(); const m = $('#gearmenu'); m.hidden = !m.hidden; $('#gearbtn').setAttribute('aria-expanded', String(!m.hidden)); syncThemeLabel(); };
document.addEventListener('mouseover', e => {  // full text on hover for anything shortened with an ellipsis
  const el = e.target.closest?.('.name,.grow,.sub,.s,.tx b,.tx small,.src-d,.src-l,.lbl,.chip,.k,.d,.fmeta span,.pal-group,.seg button,.badge,h2,h3,code');
  if (el && !el.title && (el.scrollWidth > el.clientWidth + 1 || el.scrollHeight > el.clientHeight + 1)) el.title = el.textContent.trim();
});
document.addEventListener('pointerdown', e => { if (!$('#gearmenu').hidden && !e.target.closest('#gearmenu,#gearbtn')) closeMenu(); });
$$('#gearmenu [data-go]').forEach(b => b.onclick = () => {
  closeMenu(); const g = b.dataset.go;
  if (g !== 'settings') { S.setTab = g; store.set('setTab', g); }
  S.view === 'settings' ? renderSettings() : setView('settings');
});
function toggleSide(open) {
  const on = open ?? !document.body.classList.contains('side-open');
  document.body.classList.toggle('side-open', on); store.set('sideOpen', on);
  setTimeout(() => { if (S.view === 'map') renderTimeline(); }, 260);
}
$('#sidetoggle').onclick = () => toggleSide();
addEventListener('resize', debounce(() => { if (S.view === 'map') renderTimeline(); }, 200));
toggleSide(innerWidth >= 900 ? store.get('sideOpen', true) : false);
if (window.pensieveNative === true) {
  document.body.classList.add('native');
  // the window can be dragged by the empty top bar and by invisible strips at its edges (the app does the dragging)
  document.body.insertAdjacentHTML('beforeend', '<div class="dragedge t" data-drag></div><div class="dragedge l" data-drag></div><div class="dragedge r" data-drag></div><div class="dragedge b" data-drag></div>');
  $('#top').setAttribute('data-drag', '');
  const dragSpot = e => e.target.closest('[data-drag]') && !e.target.closest('button,a,input,select,textarea,label,nav,[role=menu],.menu,.glass');
  const win = msg => window.webkit?.messageHandlers?.pensieveWindow?.postMessage(msg);
  document.addEventListener('mousedown', e => { if (e.button === 0 && dragSpot(e)) win({drag: true}); }, true);
}
$('#helpmodal').addEventListener('click', e => { if (e.target.id === 'helpmodal' || e.target.dataset.close != null) $('#helpmodal').hidden = true; });
document.addEventListener('keydown', e => {
  const typing = /INPUT|TEXTAREA/.test(document.activeElement?.tagName);
  if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') { e.preventDefault(); openPalette(); return; }
  if (e.key === 'Escape') {
    if (!$('#helpmodal').hidden) return ($('#helpmodal').hidden = true);
    if (!$('#gearmenu').hidden) return closeMenu();
    if (S.view === 'settings' && !typing) return setView(S.prevView || 'code');
    if (typing) return document.activeElement.blur();
    if ($('#drawer').classList.contains('open')) return closeDrawer();
    if (S.highlight || S.code.highlight || S.files.highlight) { S.highlight = null; S.code.highlight = null; S.files.highlight = null; S.code.focus = null; refreshStates(); renderToolbar(); renderSidebar(); return; }
    return;
  }
  if (typing || e.metaKey || e.ctrlKey || e.altKey) return;
  const k = e.key.toLowerCase();
  if (k === '/') { e.preventDefault(); openPalette(); }
  else if (k === '1') setView(dataType()); else if (k === '2') setView('insights'); else if (k === '3') setView('settings');
  else if (k === 'f') gl.fit(visibleIdx());
  else if (k === 't') toggleTheme();
  else if (k === '?') $('#helpmodal').hidden = false;
});
matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => { if (appearance !== 'system') return; gl.setTheme(); recolor(); renderTimeline(); renderSidebar(); if (S.view === 'insights') renderInsights(); });

function renderStatus() {
  const st = S.status, pill = $('#statuspill'), label = $('span', pill);
  const busy = st.layout || st.insights_running || st.indexing || st.summarized_sessions < st.sessions || st.topics_named < st.topics;
  const state = st.message?.startsWith('error') ? 'off' : st.llm_offline ? 'warn' : busy ? 'busy' : 'live';
  pill.className = 'menu-status ' + state; $('#gearbtn').dataset.state = state;
  if (st.llm_offline) {
    label.textContent = 'Language model offline';
    pill.title = `No language model at ${st.llm_url || 'the LLM URL'}. Start that server, or pick another in Settings → AI & Insights, for summaries, topic names and insights. Search, maps, files and code views still work.`;
    return;
  }
  label.textContent = st.layout ? 'Laying out map…' : st.summarized_sessions < st.sessions ? `Summarizing ${st.summarized_sessions}/${st.sessions}` :
    st.topics_named < st.topics ? 'Naming topics…' : st.insights_running ? 'Generating insights…' : st.indexing && st.message && st.message !== 'idle' ? st.message[0].toUpperCase() + st.message.slice(1) + '…' : `Live · ${plural(st.files || 0, 'file')} · ${plural(st.sessions || 0, 'session')}`;
  $('#gearbtn').title = label.textContent;
  pill.title = `${(st.files || 0).toLocaleString()} files · ${(st.repos || 0)} repos · ${(st.sessions || 0)} sessions · ${(st.chunks || 0).toLocaleString()} moments · ${(st.code_chunks || 0).toLocaleString()} code chunks\nSummaries: ${st.summarized_sessions}/${st.sessions} sessions, ${st.summarized_chunks?.toLocaleString()} moments\nModels: ${st.llm} + ${st.embed}`;
}
$('#statuspill').onclick = () => toast(S.status.llm_offline ? $('#statuspill').title : $('#statuspill').title.split('\n')[0]);

/* ============================== data loading + live updates ============================== */
async function loadSessions() {
  const [pts, topics, projects] = await Promise.all([api('/api/points?level=session'), api('/api/topics'), api('/api/projects')]);
  const prev = new Map(S.sessions.map(s => [s.id, s.updated]));
  S.sessions = pts.points.map(s => ({...s, t0: T(s.started), t1: T(s.updated) || T(s.started)}));
  S.byId = new Map(S.sessions.map(s => [s.id, s]));
  S.topics = topics; S.topicById = new Map(topics.map(t => [t.id, t]));
  S.projects = projects;
  buildTree();
  // a session's code area: the repo folder (two levels deep) it touched most
  S.sessions.forEach(s => {
    const c = new Map(); (s.dirs || []).forEach(d => { const k = d.split('/').slice(0, 3).join('/'); c.set(k, (c.get(k) || 0) + 1); });
    s.area = [...c].sort((a, b) => b[1] - a[1] || a[0].length - b[0].length)[0]?.[0] || null;
  });
  const ac = new Map(); S.sessions.forEach(s => s.area && ac.set(s.area, (ac.get(s.area) || 0) + 1));
  S.areaSlot = new Map([...ac].sort((a, b) => b[1] - a[1]).slice(0, 7).map(([k], i) => [k, i]));
  S.projectSlot = new Map(projects.slice(0, 7).map((p, i) => [p.name, i]));
  const ts = S.sessions.map(s => s.t1).filter(x => !isNaN(x));
  S.tRange = [Math.min(...S.sessions.map(s => s.t0).filter(x => !isNaN(x))), Math.max(...ts)];
  return prev.size ? S.sessions.filter(s => prev.get(s.id) !== s.updated).map(s => s.id) : [];
}
async function reloadAll(pulse = true) {
  const changed = await loadSessions();
  if (S.scope && changed.length) await syncScope(true);
  if (S.chunks) S.chunks = (await api('/api/points?level=chunk')).points;
  if (pulse && changed.length) {
    changed.forEach(id => S.pulse.add(id));
    setTimeout(() => { changed.forEach(id => S.pulse.delete(id)); if (S.view === 'map') refreshStates(); }, 6000);
  }
  if (S.view === 'map') { const c = gl.saveCam(); buildMap(); gl.loadCam(c); }
  refresh();
}
function connect() {
  const es = new EventSource('/api/events');
  es.onmessage = e => handleEvent(JSON.parse(e.data)).catch(() => {});  // server restarts are transient
  es.onerror = () => { $('#statuspill').className = 'menu-status off'; $('#gearbtn').dataset.state = 'off'; $('span', $('#statuspill')).textContent = 'Reconnecting…'; };
}
async function handleEvent(m) {
  {
    if (m.type === 'status') { Object.assign(S.status, m); pollStatus(); }
    else if (m.type === 'updated') { reloadAll(); reloadDataSoon(); refetchSourcesSoon(); }
    else if (m.type === 'summaries') { await loadSessions(); renderSidebar(); pollStatus(); }
    else if (m.type === 'topics') { await loadSessions(); renderSidebar(); if (S.view === 'map') renderLabels(); if (S.view === 'insights') renderInsights(); pollStatus(); }
    else if (m.type === 'insights') { await loadInsights(); if (S.view === 'insights') renderInsights(); toast('New insights are ready'); pollStatus(); }
    else if (m.type === 'files') { S.files.stale = true; reloadFilesSoon(); reloadDataSoon(); refetchSourcesSoon(); }
    else if (m.type === 'settings') { loadScopes(); loadAppearance(); loadAI().then(refreshAIViews); if (S.set && !setDirty().length) { await loadSettings(true); if (S.view === 'settings') renderSettings(); } else refetchSourcesSoon(); }
    else if (m.type === 'repos') { refetchSourcesSoon(); reloadDataSoon(); await loadRepos(); if (S.view === 'code') { await loadRepo(S.code.repo); buildMap(); renderSidebar(); } }
    else if (m.type === 'toast') toast(m.message);
    else if (m.type === 'data_topics') { await loadDataTopics(); if (S.view === 'data') renderLabels(); }
    else if (m.type === 'scoped_topics') { if (S.scope) applyScope(true); }
    else if (m.type === 'team') { if (S.ins.repo === m.repo) { S.ins.key = null; if (S.view === 'insights') loadCodebase().then(renderInsights); } }
  }
}
const reloadDataSoon = debounce(async () => {
  S.data.stale = true; if (S.view !== 'data') return;
  await loadData(); const c = gl.saveCam(); buildMap(); gl.loadCam(c); renderSidebar();
}, 2500);
const reloadFilesSoon = debounce(async () => {
  if (S.view !== 'files') return;
  await loadFiles(); const c = gl.saveCam(); buildMap(); gl.loadCam(c); renderSidebar();
}, 1500);
const pollStatus = debounce(async () => { try { Object.assign(S.status, await api('/api/status')); renderStatus(); } catch {} }, 300);

async function init() {
  try {
    await Promise.all([loadSessions(), loadInsights(), loadData(), api('/api/status').then(s => Object.assign(S.status, s))]);
  } catch (e) { $('#loadingtext').textContent = 'Could not reach the Pensieve server.'; return; }
  renderStatus();
  if (!S.sessions.length) $('#loadingtext').textContent = 'Indexing your sessions — points appear as they are embedded…';
  document.body.dataset.view = S.view;
  syncTabs(S.view);
  renderToolbar(); buildMap(); refresh();
  if (!gl.controls.autoRotate || store.get('hinted', false)) $('#hint').classList.add('gone');
  setTimeout(() => $('#hint').classList.add('gone'), 9000);
  connect();
  await loadRepos().catch(() => {});
  let v = store.get('view', 'data'); if (LEGACY_TYPE[v]) v = 'data';
  loadScopes(); loadAppearance(); syncThemeLabel(); loadAI().then(() => S.view === 'insights' && renderInsights());
  if (location.hash.length > 1) await route();
  else if (v !== S.view) await setView(v);
}
init();
