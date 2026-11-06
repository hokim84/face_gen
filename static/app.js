// NPC 초상화 생성기 웹 UI (빌드 없이 동작하는 순수 JS)
"use strict";

// --------------------------------------------------------------------------- //
// 유틸
// --------------------------------------------------------------------------- //
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];

/** h("div", {class: "x", onclick: fn}, "텍스트", childEl) */
function h(tag, attrs = {}, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "text") el.textContent = v;
    else if (k === "style" && typeof v === "object") Object.assign(el.style, v);
    else if (k === "dataset") Object.assign(el.dataset, v);
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k in el && typeof v !== "string") el[k] = v;
    else if (k === "value") el.value = v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const c of kids.flat()) if (c != null && c !== false) el.append(c instanceof Node ? c : String(c));
  return el;
}

async function api(method, url, body, headers = {}) {
  const opt = { method, headers: { ...headers } };
  if (body !== undefined) {
    opt.headers["Content-Type"] = "application/json";
    opt.body = JSON.stringify(body);
  }
  let res;
  try {
    res = await fetch(url, opt);
  } catch (e) {
    throw new Error("웹 UI 서버에 연결할 수 없습니다");
  }
  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = text; }
  if (!res.ok) {
    let msg = (data && (data.message || data.detail)) || `오류 ${res.status}`;
    if (Array.isArray(msg)) msg = msg.map(d => `${(d.loc || []).join(".")}: ${d.msg}`).join(", ");
    const err = new Error(typeof msg === "string" ? msg : JSON.stringify(msg));
    err.errors = data && data.errors;
    throw err;
  }
  return data;
}

function toast(msg, kind = "info", ms = 3500) {
  const t = h("div", { class: `toast ${kind}` }, msg);
  $("#toasts").append(t);
  setTimeout(() => t.remove(), ms);
}

function fmtDur(sec) {
  if (sec == null || !isFinite(sec)) return "–";
  if (sec < 60) return `${sec.toFixed(1)}초`;
  const s = Math.round(sec);
  if (s < 3600) return `${Math.floor(s / 60)}분 ${s % 60}초`;
  return `${Math.floor(s / 3600)}시간 ${Math.floor((s % 3600) / 60)}분`;
}
const fmtTime = ts => { const d = new Date(ts * 1000); return [d.getHours(), d.getMinutes(), d.getSeconds()].map(n => String(n).padStart(2, "0")).join(":"); };
/** replaceChildren 은 null 을 "null" 글자로 넣으므로 걸러서 채움 */
const fill = (el, ...kids) => el.replaceChildren(...kids.flat().filter(k => k != null && k !== false));
const debounce = (fn, ms) => { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; };
const deepCopy = o => JSON.parse(JSON.stringify(o));
const store = {
  get(k, d) { try { const v = localStorage.getItem("npcui." + k); return v == null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem("npcui." + k, JSON.stringify(v)); } catch { /* 무시 */ } },
};

const STATUS_LABEL = { none: "미생성", base: "후처리 대기", done: "완료", approved: "승인됨" };
const MODE_LABEL = { base: "이미지 생성", full: "전체 생성", expression: "표정 재생성",
  expressions: "표정 생성", layers: "레이어 분리",
  parts_gen: "파츠 소스 생성", parts_split: "파츠 레이어 분리" };
const isPartsJob = j => j.mode.startsWith("parts_");
const badge = st => h("span", { class: `badge ${st}` }, STATUS_LABEL[st] || st);

/** pipeline.expression_list 와 같은 규칙 */
function expressionList(row, ex) {
  if ((row.image_type || "character") !== "character") return [];
  const custom = (row.expressions || "").trim();
  let names = [];
  if (custom) names = custom.split("|").map(s => s.trim()).filter(Boolean);
  else if (ex) {
    const pers = (row.personality || "").trim() || ex.default_personality;
    for (let p of pers.split("|")) {
      p = p.trim();
      if (!(p in ex.personalities)) p = ex.default_personality;
      for (const n of ex.personalities[p] || []) if (!names.includes(n)) names.push(n);
    }
  }
  if (!names.includes("neutral")) names.unshift("neutral");
  return names;
}

// --------------------------------------------------------------------------- //
// 상태
// --------------------------------------------------------------------------- //
const S = {
  tab: "characters",
  imageType: store.get("imageType", "character"),
  rows: [],                 // characters.csv
  portraits: new Map(),     // id -> 요약 (상태, 썸네일)
  portraitOrder: [],
  ex: null,                 // expressions.json
  selected: new Set(),
  lastClicked: null,
  jobs: new Map(),
  queue: null,
  comfy: null,
};

const IMAGE_TYPES = { character: "주인공/조연", npc: "NPC", monster: "몬스터" };
const typeOf = row => row.image_type || "character";
function selectImageType(kind) {
  if (!(kind in IMAGE_TYPES)) kind = "character";
  if (Chars.editing) Chars.commit($(".cell-input")?.value ?? "");
  if (!$("#ai-dialog").hidden) CharacterAI.close();
  S.imageType = kind;
  store.set("imageType", kind);
  Chars.sel.clear();
  Gallery.personality = "";
  Gallery.selected.clear();
  Gallery.closeDelete();
  $("#gal-personality").value = "";
  $$("[data-image-type]").forEach(b => b.classList.toggle("active", b.dataset.imageType === kind));
  $("#type-guide").textContent = `${IMAGE_TYPES[kind]} · ` + (kind === "monster"
    ? "외형과 구도를 자유롭게 설명해 이미지를 만드세요."
    : "이미지 생성 → 검수 → 필요한 후처리. 전체 생성으로 모든 단계를 이어서 실행할 수도 있습니다.");
  $("#btn-gen-expressions").hidden = kind !== "character";
  $("#btn-gen-layers").hidden = $("#btn-gen-full").hidden = kind === "monster";
  $("#gal-personality").hidden = kind !== "character";
  $("#btn-gen-base").classList.toggle("primary", kind === "monster");
  if (["characters", "gallery"].includes(S.tab)) showTab(S.tab);
  else showTab("characters");
}

// --------------------------------------------------------------------------- //
// 탭
// --------------------------------------------------------------------------- //
const TABS = ["characters", "queue", "gallery", "parts", "settings"];

function showTab(name) {
  if (!TABS.includes(name)) name = "characters";
  S.tab = name;
  for (const t of TABS) $(`#tab-${t}`).hidden = t !== name;
  $$(".tab-btn").forEach(b => b.classList.toggle("active", b.dataset.tab === name));
  if (location.hash !== "#" + name) history.replaceState(null, "", "#" + name);
  if (name === "characters") renderCharacters();
  if (name === "queue") renderQueue();
  if (name === "gallery") renderGallery();
  if (name === "parts") Parts.open();
  if (name === "settings") Settings.open();
}

// --------------------------------------------------------------------------- //
// 데이터 로드
// --------------------------------------------------------------------------- //
async function loadRows() { await Chars.load(); }

async function loadPortraits() {
  try {
    const list = await api("GET", "/api/portraits");
    S.portraits = new Map(list.map(p => [p.id, p]));
    S.portraitOrder = list.map(p => p.id);
  } catch (e) {
    toast("결과 목록을 읽지 못했습니다: " + e.message, "error");
  }
}

async function loadExpressions() {
  try { S.ex = await api("GET", "/api/expressions"); } catch (e) { toast("expressions.json 오류: " + e.message, "error"); }
}

async function loadJobs() {
  const d = await api("GET", "/api/jobs");
  S.jobs = new Map(d.jobs.map(j => [j.id, j]));
  S.queue = d.queue;
  renderQueueHeader();
  if (S.tab === "queue") renderQueue();
}

const refreshPortraits = debounce(async () => {
  await loadPortraits();
  if (S.tab === "gallery") renderGallery();
  if (S.tab === "characters" && !Chars.editing) renderCharacters();
}, 400);

async function pollStatus() {
  try {
    const st = await api("GET", "/api/status");
    S.comfy = st.comfy;
    S.queue = st.queue;
    renderComfyPill();
    renderConfigBanner(st.config_issues || []);
    renderQueueHeader();
  } catch {
    S.comfy = null;
    renderComfyPill();
  }
}

/** config.json 의 모델 이름이 ComfyUI 에 없으면 생성 전에 미리 경고 */
function renderConfigBanner(issues) {
  const b = $("#config-banner");
  b.hidden = issues.length === 0;
  if (!issues.length) return;
  fill(b, h("b", {}, "설정 확인 필요: "), issues.join(" · "), " ",
    h("button", { class: "linkish", onclick: () => showTab("settings") }, "설정에서 고치기"));
}

function renderComfyPill() {
  const pill = $("#comfy-pill");
  const c = S.comfy;
  pill.classList.toggle("ok", !!(c && c.ok));
  pill.classList.toggle("bad", !c || !c.ok);
  $("span", pill).textContent = !c ? "서버 응답 없음" : c.ok ? "ComfyUI 연결됨" : "ComfyUI 연결 안 됨";
  pill.title = c ? `${c.url}${c.ok ? ` · ${c.latency_ms}ms` : `\n${c.error || ""}`}` : "";
}

// --------------------------------------------------------------------------- //
// 1. 캐릭터 표 (편집은 화면 안의 초안에 하고 [저장]으로 characters.csv 에 기록)
// --------------------------------------------------------------------------- //
function statusOf(id) { return (S.portraits.get(id) || {}).status || "none"; }

const CHAR_COLS = [
  { key: "id", label: "id", cls: "mono", w: 110 },
  { key: "name", label: "이름", w: 90 },
  { key: "image_type", label: "이미지 유형", w: 130, type: "image_type" },
  { key: "prompt", label: "몬스터 설명·구도 태그", w: 360 },
  { key: "subject", label: "대상", w: 70, list: ["1girl", "1boy", "1other"] },
  { key: "age", label: "나이", w: 110 },
  { key: "hair", label: "머리", w: 200 },
  { key: "eyes", label: "눈", w: 110 },
  { key: "outfit", label: "옷", w: 190 },
  { key: "extra", label: "기타", w: 140 },
  { key: "personality", label: "성격", w: 90, type: "personality" },
  { key: "expressions", label: "표정 직접 지정", w: 140, ph: "비우면 성격대로" },
  { key: "pose", label: "포즈", w: 100, ph: "기본", list: () => Chars.poses },
  { key: "seed", label: "seed", cls: "mono", w: 100, ph: "자동" },
];
const ROW_ID_RE = /^[\p{L}\p{N}_-]+$/u;

const Chars = {
  draft: [],          // [{_k, _orig(저장된 행 또는 null), ...열}]
  columns: [],        // CSV 열 순서
  poses: [],          // ControlNet 포즈 템플릿 이름 (pose 칸 자동 완성)
  version: null,      // 불러온 파일의 버전 (저장 시 충돌 확인)
  savedJSON: "[]",
  sel: new Set(),     // 선택한 행의 _k
  lastK: null,
  editing: null,      // {k, key}
  nextK: 1,

  // ---- 데이터 ---- //
  async load(keepSelection = true) {
    let res;
    try {
      res = await fetch("/api/characters");
    } catch {
      toast("웹 UI 서버에 연결할 수 없습니다", "error");
      return;
    }
    const data = await res.json();
    if (!res.ok) {
      toast("characters.csv 를 읽지 못했습니다: " + (data.message || res.status), "error", 6000);
      S.rows = [];
      return;
    }
    const selIds = keepSelection ? new Set(this.draft.filter(r => this.sel.has(r._k)).map(r => r.id)) : new Set();
    this.version = res.headers.get("X-Version");
    try { this.columns = JSON.parse(res.headers.get("X-Columns")); } catch { this.columns = CHAR_COLS.map(c => c.key); }
    S.rows = data;
    this.savedJSON = JSON.stringify(data);
    this.draft = data.map(r => ({ _k: this.nextK++, _orig: r, ...r }));
    this.sel = new Set(this.draft.filter(r => selIds.has(r.id)).map(r => r._k));
    this.editing = null;
    this.showErrors();
    api("GET", "/api/poses").then(d => { this.poses = d.poses.map(n => n.replace(/\.png$/i, "")); }).catch(() => {});
  },

  strip() {
    return this.draft.map(r => Object.fromEntries(this.columns.map(c => [c, (r[c] ?? "").toString()])));
  },
  dirty() { return JSON.stringify(this.strip()) !== this.savedJSON; },
  byK(k) { return this.draft.find(r => r._k === k); },
  allCols() {
    const extra = this.columns.filter(c => !CHAR_COLS.some(d => d.key === c)).map(c => ({ key: c, label: c, w: 120 }));
    const hidden = S.imageType === "monster" ? ["subject", "age", "hair", "eyes", "outfit", "extra", "personality", "expressions", "pose"]
      : S.imageType === "npc" ? ["prompt", "personality", "expressions"] : ["prompt"];
    return [...CHAR_COLS.filter(c => !hidden.includes(c.key)), ...extra];
  },
  /** 결과물 폴더가 있는 행은 id 를 바꿀 수 없음 (폴더 이름이므로) */
  idLocked(r) { return !!r._orig && statusOf(r._orig.id) !== "none"; },

  // ---- 검증 (칸 색칠용) ---- //
  problems() {
    const out = new Map();  // _k -> {key: ["error"|"warn", 메시지]}
    const seen = new Map();
    const pers = S.ex ? S.ex.personalities : {};
    const exprs = S.ex ? S.ex.expressions : {};
    const put = (r, key, lvl, msg) => { if (!out.has(r._k)) out.set(r._k, {}); out.get(r._k)[key] = [lvl, msg]; };
    for (const r of this.draft) {
      if (!(typeOf(r) in IMAGE_TYPES)) put(r, "image_type", "error", "character / npc / monster 중 선택하세요");
      if (typeOf(r) === "monster" && !(r.prompt || "").trim()) put(r, "prompt", "error", "몬스터 설명 태그를 입력하세요");
      const id = (r.id || "").trim();
      if (!id) put(r, "id", "error", "id 가 비어 있습니다");
      else if (!ROW_ID_RE.test(id)) put(r, "id", "error", "글자/숫자/_/- 만 쓸 수 있습니다 (폴더 이름)");
      else if (seen.has(id.toLowerCase())) { put(r, "id", "error", "id 가 겹칩니다"); put(seen.get(id.toLowerCase()), "id", "error", "id 가 겹칩니다"); }
      else seen.set(id.toLowerCase(), r);
      const seed = (r.seed || "").trim();
      if (seed && !/^\d+$/.test(seed)) put(r, "seed", "error", "0 이상의 정수");
      const bad = (typeOf(r) === "character" ? r.personality || "" : "").split("|").map(s => s.trim()).filter(p => p && !(p in pers));
      if (bad.length) put(r, "personality", "warn", `없는 성격: ${bad.join(", ")} → 기본 성격 사용`);
      const names = (typeOf(r) === "character" ? r.expressions || "" : "").split("|").map(s => s.trim()).filter(Boolean);
      const badName = names.filter(n => !/^[\w-]+$/.test(n));
      const unknown = names.filter(n => /^[\w-]+$/.test(n) && !(n in exprs));
      if (badName.length) put(r, "expressions", "error", `이름에 쓸 수 없는 글자: ${badName.join(", ")}`);
      else if (unknown.length) put(r, "expressions", "warn", `expressions.json 에 없는 표정: ${unknown.join(", ")} (이름을 태그로 사용)`);
    }
    return out;
  },

  // ---- 그리기 ---- //
  filtered() {
    const q = $("#char-search").value.trim().toLowerCase();
    const st = $("#char-status").value;
    return this.draft.filter(r => {
      if (typeOf(r) !== S.imageType) return false;
      if (!r._orig) return true;  // 새로 추가한 행은 항상 보임
      if (st && statusOf(r._orig.id) !== st) return false;
      if (!q) return true;
      return this.columns.some(k => (r[k] || "").toString().toLowerCase().includes(q));
    });
  },

  render() {
    // 편집 중인 칸이 있으면 값을 먼저 반영 (다시 그리면 입력 칸이 사라지므로)
    const inp = $(".cell-input");
    if (this.editing && inp) {
      const r = this.byK(this.editing.k);
      if (r) r[this.editing.key] = inp.value.trim();
    }
    this.editing = null;
    const cols = this.allCols();
    const thead = $("#char-table thead");
    fill(thead, h("tr", {},
      h("th", { class: "col-check" }, h("input", { type: "checkbox", id: "char-check-all", title: "보이는 행 모두 선택" })),
      h("th", {}, "상태"),
      ...cols.map(c => h("th", { style: { minWidth: c.w + "px" } }, c.label))));
    const rows = this.filtered();
    const probs = this.problems();
    const frag = document.createDocumentFragment();
    for (const r of rows) frag.append(this.rowEl(r, cols, probs.get(r._k)));
    $("#char-table tbody").replaceChildren(frag);
    const empty = $("#char-empty");
    empty.hidden = rows.length > 0;
    empty.textContent = this.draft.length ? "검색 결과가 없습니다" : "캐릭터가 없습니다. [+ 추가] 또는 [가져오기]로 시작하세요.";
    const all = $("#char-check-all");
    const on = rows.filter(r => this.sel.has(r._k)).length;
    all.checked = rows.length > 0 && on === rows.length;
    all.indeterminate = on > 0 && on < rows.length;
    this.updateBar(probs);
  },

  rowEl(r, cols, prob = {}) {
    const st = r._orig ? statusOf(r._orig.id) : "new";
    const tr = h("tr", { class: (this.sel.has(r._k) ? "selected " : "") + (r._orig ? "" : "new-row"), dataset: { k: r._k } },
      h("td", { class: "col-check" }, h("input", { type: "checkbox", checked: this.sel.has(r._k), tabIndex: -1 })),
      h("td", { class: "st" }, st === "new" ? h("span", { class: "badge new" }, "새 행")
        : h("button", { class: "badge-btn", title: "검수 화면 열기" }, badge(st))));
    for (const c of cols) {
      const v = (r[c.key] ?? "").toString();
      const changed = !r._orig || (r._orig[c.key] ?? "") !== v;
      const p = prob[c.key];
      const locked = c.key === "id" && this.idLocked(r);
      const td = h("td", {
        class: ["cell", c.cls, changed && r._orig ? "changed" : "", p ? p[0] : "", locked ? "locked" : ""].filter(Boolean).join(" "),
        dataset: { key: c.key },
        title: p ? p[1] : locked ? "결과물이 있어 id 를 바꿀 수 없습니다 (output 폴더 이름)" : v,
      }, (c.key === "image_type" ? IMAGE_TYPES[v || "character"] : v) || (c.ph ? h("span", { class: "ph" }, c.key === "personality" ? "" : c.ph) : ""));
      if (c.key === "personality" && !v) td.append(h("span", { class: "ph" }, `기본(${S.ex ? S.ex.default_personality : "?"})`));
      tr.append(td);
    }
    return tr;
  },

  rerenderRow(k) {
    const old = $(`#char-table tbody tr[data-k="${k}"]`);
    const r = this.byK(k);
    if (!old || !r) return this.render();
    const probs = this.problems();
    old.replaceWith(this.rowEl(r, this.allCols(), probs.get(k)));
    this.updateBar(probs);
  },

  updateBar(probs = this.problems()) {
    const dirty = this.dirty();
    const added = this.draft.filter(r => !r._orig).length;
    const removed = S.rows.length - this.draft.filter(r => r._orig).length;
    const changed = this.draft.filter(r => r._orig && this.columns.some(c => (r._orig[c] ?? "") !== (r[c] ?? ""))).length;
    const errs = [...probs.values()].filter(p => Object.values(p).some(([l]) => l === "error")).length;
    const el = $("#char-dirty");
    const parts = [added && `추가 ${added}`, changed && `수정 ${changed}`, removed && `삭제 ${removed}`].filter(Boolean);
    el.textContent = dirty ? `저장하지 않은 변경: ${parts.join(" · ") || "순서 변경"}` + (errs ? ` · 오류 ${errs}행` : "") : "변경 사항 없음";
    el.className = dirty ? (errs ? "err" : "dirty") : "muted";
    $("#btn-char-save").disabled = !dirty;
    $("#btn-char-revert").disabled = !dirty;
    const n = this.sel.size;
    $("#char-selection").textContent = n ? `${n}명 선택 / 전체 ${this.draft.length}명` : `전체 ${this.draft.length}명`;
    $("#btn-gen-base").disabled = $("#btn-gen-full").disabled = n === 0;
    $("#btn-gen-expressions").disabled = $("#btn-gen-layers").disabled = n === 0;
    $("#btn-row-dup").disabled = $("#btn-row-del").disabled = n === 0;
  },

  showErrors(title, list, kind = "error") {
    const box = $("#char-errors");
    if (!list || !list.length) { box.hidden = true; return; }
    box.className = "errorbox" + (kind === "warn" ? " warnbox" : "");
    fill(box, h("b", {}, title), h("button", { class: "icon-btn close", onclick: () => { box.hidden = true; } }, "✕"),
      h("ul", {}, ...list.map(e => h("li", {}, e))));
    box.hidden = false;
  },

  // ---- 칸 편집 ---- //
  startEdit(k, key) {
    if (this.editing) this.commit($(".cell-input")?.value ?? "");  // 편집 중이던 칸 먼저 확정
    const r = this.byK(k);
    const td = $(`#char-table tbody tr[data-k="${k}"] td[data-key="${key}"]`);
    if (!r || !td) { this.editing = null; return; }
    if (key === "id" && this.idLocked(r)) { toast("결과물이 있는 캐릭터는 id 를 바꿀 수 없습니다 (output 폴더 이름)", "warn"); return; }
    this.editing = { k, key };
    const col = this.allCols().find(c => c.key === key);
    const cur = (r[key] ?? "").toString();
    let input;
    if (col.type === "image_type") {
      input = h("select", {}, ...Object.entries(IMAGE_TYPES).map(([key, label]) => h("option", { value: key }, label)));
      input.value = cur || "character";
      input.addEventListener("change", () => this.commit(input.value));
    } else if (col.type === "personality") {
      const names = S.ex ? Object.keys(S.ex.personalities) : [];
      input = h("select", {},
        h("option", { value: "" }, `(기본: ${S.ex ? S.ex.default_personality : "?"})`),
        ...names.map(n => h("option", { value: n }, n)),
        cur && !names.includes(cur) ? h("option", { value: cur }, `${cur} (현재 값)`) : null);
      input.value = cur;
      input.addEventListener("change", () => this.commit(input.value));
    } else {
      input = h("input", { type: "text", value: cur, placeholder: col.ph || "", spellcheck: false });
      const list = typeof col.list === "function" ? col.list() : col.list;
      if (list && list.length) {
        const dl = h("datalist", { id: "dl-" + key }, ...list.map(v => h("option", { value: v })));
        td.append(dl);
        input.setAttribute("list", "dl-" + key);
      }
    }
    input.className = "cell-input";
    input.addEventListener("keydown", e => {
      if (e.key === "Enter") { e.preventDefault(); this.commit(input.value, "down"); }
      else if (e.key === "Tab") { e.preventDefault(); this.commit(input.value, e.shiftKey ? "left" : "right"); }
      else if (e.key === "Escape") { e.preventDefault(); this.cancel(); }
    });
    input.addEventListener("blur", () => { if (this.editing && this.editing.k === k && this.editing.key === key) this.commit(input.value); });
    td.replaceChildren(input, ...(td.querySelector("datalist") ? [td.querySelector("datalist")] : []));
    td.classList.add("editing");
    input.focus();
    if (input.select) input.select();
  },

  commit(value, move = null) {
    const ed = this.editing;
    if (!ed) return;
    this.editing = null;
    const r = this.byK(ed.k);
    if (!r) return;
    const v = (value ?? "").trim();
    const changed = (r[ed.key] ?? "") !== v;
    r[ed.key] = v;
    if (changed && ed.key === "image_type") { this.sel.delete(ed.k); this.render(); }
    else if (changed && ed.key === "id") this.render(); else this.rerenderRow(ed.k);
    if (move) this.moveEdit(ed, move);
  },

  cancel() {
    const ed = this.editing;
    this.editing = null;
    if (ed) this.rerenderRow(ed.k);
  },

  moveEdit(ed, dir) {
    const rows = this.filtered();
    const cols = this.allCols().map(c => c.key);
    let ri = rows.findIndex(r => r._k === ed.k), ci = cols.indexOf(ed.key);
    if (dir === "right") { ci++; if (ci >= cols.length) { ci = 0; ri++; } }
    if (dir === "left") { ci--; if (ci < 0) { ci = cols.length - 1; ri--; } }
    if (dir === "down") ri++;
    const r = rows[ri];
    if (!r) return;
    if (cols[ci] === "id" && this.idLocked(r)) ci = Math.min(ci + (dir === "left" ? -1 : 1), cols.length - 1);
    if (ci < 0) return;
    this.startEdit(r._k, cols[ci]);
  },

  // ---- 행 추가/복제/삭제 ---- //
  /** 기존 id 패턴(예: npc_0042)을 따라 다음 번호 */
  nextIds(n) {
    const used = new Set(this.draft.map(r => (r.id || "").toLowerCase()));
    for (const id of S.portraits.keys()) used.add(id.toLowerCase());
    let prefix = S.imageType === "monster" ? "monster_" : "npc_", width = 4, max = 0;
    for (const r of this.draft) {
      if (S.imageType && S.imageType !== "character" && typeOf(r) !== S.imageType) continue;
      const m = /^(.*?)(\d+)$/.exec(r.id || "");
      if (m && +m[2] >= max) { prefix = m[1]; width = m[2].length; max = +m[2]; }
    }
    const ids = [];
    for (let i = max + 1; ids.length < n; i++) {
      const id = prefix + String(i).padStart(width, "0");
      if (!used.has(id.toLowerCase())) { ids.push(id); used.add(id.toLowerCase()); }
    }
    return ids;
  },

  add() {
    const [id] = this.nextIds(1);
    const row = { _k: this.nextK++, _orig: null, ...Object.fromEntries(this.columns.map(c => [c, ""])), id, subject: "1girl", image_type: S.imageType };
    this.draft.push(row);
    this.sel = new Set([row._k]);
    this.render();
    const tr = $(`#char-table tbody tr[data-k="${row._k}"]`);
    tr?.scrollIntoView({ block: "center" });
    this.startEdit(row._k, "name");
  },

  addGenerated(rows) {
    if (this.editing) this.commit($(".cell-input")?.value ?? "");
    const ids = this.nextIds(rows.length);
    const added = rows.map((row, i) => ({
      ...Object.fromEntries(this.columns.map(c => [c, ""])), ...row,
      image_type: row.image_type || S.imageType || "character",
      id: ids[i], seed: "", expressions: "", _k: this.nextK++, _orig: null,
    }));
    this.draft.push(...added);
    this.sel = new Set(added.map(row => row._k));
    $("#char-search").value = "";
    $("#char-status").value = "";
    this.render();
    $(`#char-table tbody tr[data-k="${added[0]._k}"]`)?.scrollIntoView({ block: "center" });
    toast(`${added.length}명을 초안에 추가했습니다. [저장]해야 CSV에 기록됩니다.`, "ok", 6000);
  },

  duplicate() {
    const picked = this.draft.filter(r => this.sel.has(r._k));
    if (!picked.length) return;
    const ids = this.nextIds(picked.length);
    const newSel = new Set();
    picked.forEach((src, i) => {
      const copy = { ...src, _k: this.nextK++, _orig: null, id: ids[i], seed: "" };
      this.draft.splice(this.draft.indexOf(src) + 1, 0, copy);
      newSel.add(copy._k);
    });
    this.sel = newSel;
    this.render();
    toast(`${picked.length}명을 복제했습니다 (seed 는 비워서 다른 그림이 나옵니다). 저장해야 반영됩니다.`, "ok");
  },

  remove() {
    const n = this.sel.size;
    if (!n) return;
    const withOutput = this.draft.filter(r => this.sel.has(r._k) && r._orig && statusOf(r._orig.id) !== "none").length;
    this.draft = this.draft.filter(r => !this.sel.has(r._k));
    this.sel.clear();
    this.render();
    toast(`${n}명을 표에서 뺐습니다. [저장] 전까지는 [되돌리기]로 복구할 수 있습니다.`
      + (withOutput ? ` 결과물 폴더(${withOutput}명)는 지우지 않습니다.` : ""), "warn", 6000);
  },

  revert() {
    this.draft = S.rows.map(r => ({ _k: this.nextK++, _orig: r, ...r }));
    this.sel.clear();
    this.editing = null;
    this.showErrors();
    this.render();
  },

  async save() {
    if (this.editing) this.commit($(".cell-input")?.value ?? "");
    let res, data;
    this.saving = true;  // 저장하는 동안 들어오는 우리 자신의 SSE "characters" 이벤트는 무시
    try {
      res = await fetch("/api/characters", {
        method: "PUT",
        headers: { "Content-Type": "application/json", "X-Base-Version": this.version || "" },
        body: JSON.stringify(this.strip()),
      });
      data = await res.json();
    } catch {
      this.saving = false;
      return toast("웹 UI 서버에 연결할 수 없습니다", "error");
    }
    if (!res.ok) {
      this.saving = false;
      if (res.status === 409) {
        this.showErrors("저장하지 못했습니다", [data.message]);
      } else {
        this.showErrors(data.message || "저장하지 못했습니다", data.errors || [data.message]);
      }
      return toast("저장하지 못했습니다", "error");
    }
    await this.load();
    this.saving = false;
    await loadPortraits();
    this.render();
    if (data.warnings && data.warnings.length) this.showErrors(`저장했습니다. 경고 ${data.warnings.length}건:`, data.warnings, "warn");
    toast(`characters.csv 를 저장했습니다 (${data.count}명)`, "ok");
  },

  // ---- CSV 가져오기 / 내보내기 ---- //
  async importFile(file) {
    let data;
    try {
      const res = await fetch("/api/characters/import", { method: "POST", headers: { "Content-Type": "text/csv" }, body: await file.arrayBuffer() });
      data = await res.json();
      if (!res.ok) throw new Error(data.message || res.status);
    } catch (e) {
      return toast("CSV 를 읽지 못했습니다: " + e.message, "error", 6000);
    }
    const byId = new Map(this.draft.map(r => [(r.id || "").toLowerCase(), r]));
    const same = data.rows.filter(r => byId.has(r.id.toLowerCase())).length;
    fill($("#imp-body"),
      h("p", {}, h("b", {}, file.name), ` · 인코딩 ${data.encoding} · ${data.rows.length}명`),
      h("ul", { class: "imp-stats" },
        h("li", {}, `지금 표와 같은 id: ${same}명`),
        h("li", {}, `새 id: ${data.rows.length - same}명`),
        data.unknown_columns.length ? h("li", {}, `추가 열(그대로 보존): ${data.unknown_columns.join(", ")}`) : null),
      data.errors.length ? h("div", { class: "errorbox" }, h("b", {}, `오류 ${data.errors.length}건 — 가져온 뒤 표에서 고쳐야 저장할 수 있습니다`),
        h("ul", {}, ...data.errors.slice(0, 8).map(e => h("li", {}, e)))) : null,
      data.warnings.length ? h("p", { class: "muted small" }, `경고 ${data.warnings.length}건 (없는 성격/표정 이름 등)`) : null,
      h("p", { class: "muted small" }, "합치기: 같은 id 는 파일 내용으로 덮어쓰고 새 id 는 뒤에 추가합니다. 전부 바꾸기: 표를 파일 내용으로 바꿉니다. 어느 쪽이든 [저장]을 눌러야 characters.csv 에 기록됩니다."));
    const dlg = $("#import-dialog");
    dlg.hidden = false;
    const close = () => { dlg.hidden = true; };
    $("#imp-merge").onclick = () => { this.applyImport(data, "merge"); close(); };
    $("#imp-replace").onclick = () => { this.applyImport(data, "replace"); close(); };
    $$("#import-dialog [data-close]").forEach(el => { el.onclick = close; });
  },

  applyImport(data, mode) {
    for (const c of data.columns) if (!this.columns.includes(c)) this.columns.push(c);
    const savedById = new Map(S.rows.map(r => [r.id.toLowerCase(), r]));
    const fillCols = r => Object.fromEntries(this.columns.map(c => [c, r[c] ?? ""]));
    if (mode === "replace") {
      this.draft = data.rows.map(r => ({ _k: this.nextK++, _orig: savedById.get(r.id.toLowerCase()) || null, ...fillCols(r) }));
    } else {
      const byId = new Map(this.draft.map(r => [(r.id || "").toLowerCase(), r]));
      for (const r of data.rows) {
        const hit = byId.get(r.id.toLowerCase());
        if (hit) for (const c of data.columns) hit[c] = r[c] ?? "";
        else this.draft.push({ _k: this.nextK++, _orig: null, ...fillCols(r) });
      }
    }
    this.sel.clear();
    this.render();
    toast(`${data.rows.length}명을 가져왔습니다. 확인한 뒤 [저장]을 누르세요.`, "ok", 5000);
  },

  exportCsv() {
    if (this.dirty()) toast("저장하지 않은 변경은 빠집니다. 저장된 characters.csv 를 내려받습니다.", "warn", 5000);
    location.href = "/api/characters/export";
  },

  /** SSE: 표가 바뀜 (다른 창에서 저장, 또는 base 를 새 seed 로 뽑아 seed 칸이 기록됨) */
  async onExternalChange(ev) {
    if (this.saving || ev.version === this.version) return;
    if (!this.dirty() && !this.editing) {
      await this.load();
      if (S.tab === "characters") this.render();
      return;
    }
    if (ev.changes) {  // 편집 중이면 seed 만 초안에 반영하고 버전을 갱신
      for (const ch of ev.changes) {
        const r = this.draft.find(x => x._orig && x._orig.id === ch.id);
        const saved = S.rows.find(x => x.id === ch.id);
        if (saved) saved.seed = ch.seed;
        if (r) {
          if ((r.seed ?? "") === (r._orig.seed ?? "")) r.seed = ch.seed;
          r._orig.seed = ch.seed;
        }
      }
      this.savedJSON = JSON.stringify(S.rows);
      this.version = ev.version;
      if (S.tab === "characters" && !this.editing) this.render();
    } else {
      toast("다른 곳에서 characters.csv 가 저장되었습니다. 지금 변경을 저장하려면 먼저 다시 불러와야 합니다.", "warn", 7000);
    }
  },

  selectedSavedIds() {
    return this.draft.filter(r => this.sel.has(r._k) && r._orig).map(r => r._orig.id);
  },

  setup() {
    $("#char-search").addEventListener("input", debounce(() => this.render(), 120));
    $("#char-status").addEventListener("change", () => this.render());
    const table = $("#char-table");
    table.addEventListener("change", e => {
      if (e.target.id !== "char-check-all") return;
      for (const r of this.filtered()) e.target.checked ? this.sel.add(r._k) : this.sel.delete(r._k);
      this.render();
    });
    const tbody = $("#char-table tbody");
    // 칸 편집은 mousedown 에서 시작 (blur 로 행이 다시 그려지면서 click 이 사라지는 것을 막음)
    tbody.addEventListener("mousedown", e => {
      if (e.button !== 0 || e.target.closest(".cell-input")) return;
      const td = e.target.closest("td[data-key]");
      if (!td) return;
      e.preventDefault();
      this.startEdit(+td.closest("tr").dataset.k, td.dataset.key);
    });
    tbody.addEventListener("click", e => {
      const tr = e.target.closest("tr");
      if (!tr || e.target.closest(".cell-input") || e.target.closest("td[data-key]")) return;
      const k = +tr.dataset.k;
      const r = this.byK(k);
      if (e.target.closest(".badge-btn")) {
        const ids = this.filtered().filter(x => x._orig).map(x => x._orig.id);
        return Detail.open(r._orig.id, ids);
      }
      // 체크박스/상태 칸: 선택 (Shift+클릭으로 범위)
      const rows = this.filtered();
      if (e.shiftKey && this.lastK != null) {
        const a = rows.findIndex(x => x._k === this.lastK), b = rows.findIndex(x => x._k === k);
        if (a >= 0 && b >= 0) {
          const on = !this.sel.has(k);
          for (const x of rows.slice(Math.min(a, b), Math.max(a, b) + 1)) on ? this.sel.add(x._k) : this.sel.delete(x._k);
        }
      } else {
        this.sel.has(k) ? this.sel.delete(k) : this.sel.add(k);
      }
      this.lastK = k;
      const el = $(`#char-table tbody tr[data-k="${k}"]`);
      if (e.shiftKey) this.render(); else { el.classList.toggle("selected", this.sel.has(k)); el.querySelector("input").checked = this.sel.has(k); this.updateBar(); }
    });
    $("#btn-row-add").addEventListener("click", () => this.add());
    $("#btn-ai-add").addEventListener("click", () => CharacterAI.open());
    $("#btn-row-dup").addEventListener("click", () => this.duplicate());
    $("#btn-row-del").addEventListener("click", () => this.remove());
    $("#btn-char-save").addEventListener("click", () => this.save());
    $("#btn-char-revert").addEventListener("click", () => this.revert());
    $("#btn-char-reload").addEventListener("click", async () => {
      await Promise.all([this.load(), loadPortraits()]);
      this.render();
      toast(`characters.csv 를 다시 읽었습니다 (${S.rows.length}명)`, "ok");
    });
    $("#btn-csv-import").addEventListener("click", () => $("#csv-file").click());
    $("#csv-file").addEventListener("change", e => {
      const f = e.target.files[0];
      e.target.value = "";
      if (f) this.importFile(f);
    });
    $("#btn-csv-export").addEventListener("click", () => this.exportCsv());
    $("#btn-gen-base").addEventListener("click", () => submitJobs("base"));
    $("#btn-gen-full").addEventListener("click", () => submitJobs("full"));
    $("#btn-gen-expressions").addEventListener("click", () => submitJobs("expressions"));
    $("#btn-gen-layers").addEventListener("click", () => submitJobs("layers"));
    document.addEventListener("keydown", e => {
      if (e.key === "Escape" && !$("#import-dialog").hidden) { $("#import-dialog").hidden = true; return; }
      if (!$("#ai-dialog").hidden) {
        if (e.key === "Escape") CharacterAI.close();
        return;
      }
      if (S.tab !== "characters" || !$("#detail").hidden || e.target.matches("input, textarea, select")) return;
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s") { e.preventDefault(); if (this.dirty()) this.save(); }
    });
  },
};

function renderCharacters() { Chars.render(); }

const CharacterAI = {
  running: false,
  generation: 0,
  promptType: null,
  savePrompt() {
    if (this.promptType) store.set(`ai.prompt.${this.promptType}`, $("#ai-description").value);
  },
  open() {
    if (Chars.editing) Chars.commit($(".cell-input")?.value ?? "");
    this.promptType = S.imageType || "character";
    const cached = store.get(`ai.prompt.${this.promptType}`, "");
    $("#ai-description").value = typeof cached === "string" ? cached : "";
    $("#ai-dialog").hidden = false;
    $("#ai-error").hidden = true;
    this.update();
    $("#ai-description").focus();
  },
  close() {
    this.savePrompt();
    this.generation++;
    $("#ai-dialog").hidden = true;
    $("#btn-ai-add").focus();
  },
  update() {
    $("#ai-run").disabled = this.running;
    $("#ai-description").disabled = this.running;
    $("#ai-count").disabled = this.running;
    $("#ai-status").textContent = this.running
      ? "캐릭터 설정을 생성 중입니다… 최대 5분 정도 걸릴 수 있습니다. 닫으면 결과를 추가하지 않습니다." : "";
  },
  async run() {
    if (this.running) return;
    this.savePrompt();
    const description = $("#ai-description").value.trim();
    const count = Number($("#ai-count").value);
    if (!description || !Number.isInteger(count) || count < 1 || count > 50) {
      $("#ai-error").textContent = "설명과 1~50 사이의 정수 인원수를 입력하세요.";
      $("#ai-error").hidden = false;
      return;
    }
    const generation = ++this.generation;
    this.running = true;
    $("#ai-error").hidden = true;
    this.update();
    try {
      const kind = S.imageType || "character";
      const data = await api("POST", "/api/characters/generate", { description, count, image_type: kind });
      if (generation !== this.generation || $("#ai-dialog").hidden) return;
      const presets = S.ex?.personalities || {};
      if (!Array.isArray(data.rows) || data.rows.length !== count || (kind !== "monster" && data.rows.some(r => !(r.personality in presets)))) {
        throw new Error("생성 결과 또는 성격 설정이 변경되었습니다. 다시 생성하세요.");
      }
      Chars.addGenerated(data.rows);
      this.close();
    } catch (err) {
      if (generation !== this.generation || $("#ai-dialog").hidden) return;
      $("#ai-error").textContent = err.message;
      $("#ai-error").hidden = false;
    } finally {
      this.running = false;
      this.update();
    }
  },
};

$("#ai-form").addEventListener("submit", event => { event.preventDefault(); CharacterAI.run(); });
$("#ai-description").addEventListener("input", () => CharacterAI.savePrompt());
$$("#ai-dialog [data-close]").forEach(el => el.addEventListener("click", () => CharacterAI.close()));

async function submitJobs(mode, ids, extra = {}) {
  if (!ids) {
    if (Chars.dirty()) return toast("표에 저장하지 않은 변경이 있습니다. 먼저 [저장]하세요.", "warn", 5000);
    ids = Chars.selectedSavedIds();
  }
  if (!ids.length) return toast("캐릭터를 먼저 선택하세요", "warn");
  const force = extra.force ?? (mode !== "expression" && $("#char-force").checked);
  try {
    const res = await api("POST", "/api/jobs", { ids, mode, ...extra, force });
    let msg = `${MODE_LABEL[mode]} ${res.added.length}건을 큐에 추가했습니다`;
    if (res.duplicates) msg += ` (이미 대기 중인 ${res.duplicates}건 제외)`;
    toast(msg, res.added.length ? "ok" : "warn");
    return res;
  } catch (e) {
    toast(e.message, "error", 6000);
  }
}

// --------------------------------------------------------------------------- //
// 2. 생성 큐
// --------------------------------------------------------------------------- //
let queueRenderPending = false;
function scheduleQueueRender() {
  if (queueRenderPending) return;
  queueRenderPending = true;
  requestAnimationFrame(() => {
    queueRenderPending = false;
    renderQueueHeader();
    if (S.tab === "queue") renderQueue();
    if (S.tab === "parts") Parts.renderStatus();
    if (Detail.id) Detail.renderJobs();
  });
}

function renderQueueHeader() {
  const q = S.queue;
  if (!q) return;
  const active = q.counts.pending + q.counts.running;
  const b = $("#queue-badge");
  b.hidden = active === 0;
  b.textContent = active;
  let mini = "";
  if (q.paused) mini = "⏸ 일시정지";
  else if (q.running) mini = `생성 중 ${q.images_done}/${q.images_total}장` + (q.eta_sec != null ? ` · 남은 ${fmtDur(q.eta_sec)}` : "");
  $("#queue-mini").textContent = mini;
}

const MAX_LIST = 150;

function jobLine(j, opts = {}) {
  const li = h("li", {},
    h("span", { class: "jl-main", title: `${j.cid} ${j.name}` },
      h("span", { class: "jl-id" }, j.cid),
      j.name, " ", h("span", { class: "muted small" }, IMAGE_TYPES[j.image_type] || ""), " ",
      h("span", { class: "jl-mode" }, MODE_LABEL[j.mode] + (j.expression ? ` · ${j.expression}` : "") + (j.force ? " · 다시" : "") + (j.new_seed ? " · 새 seed" : "")),
      opts.err ? h("span", { class: "jl-err" }, opts.err) : null),
    opts.tag ? h("span", { class: "jl-tag" }, opts.tag) : null,
    opts.button || null);
  return li;
}

function renderQueue() {
  const q = S.queue;
  if (!q) return;
  const jobs = [...S.jobs.values()];
  const by = { pending: [], running: [], done: [], failed: [] };
  for (const j of jobs) (by[j.status === "cancelled" ? "failed" : j.status] || []).push(j);

  // 진행률
  const pct = q.images_total ? (q.images_done / q.images_total) * 100 : 0;
  $("#q-bar").style.width = pct.toFixed(1) + "%";
  $(".progress").classList.toggle("paused", q.paused);
  const st = $("#q-state");
  st.className = "state-label " + (q.paused ? "paused" : q.running ? "running" : "");
  st.textContent = q.paused ? "일시정지됨" : q.running ? "생성 중" : by.pending.length ? "대기 중" : "대기 중인 작업 없음";
  $("#q-count").textContent = q.images_total ? `${q.images_done} / ${q.images_total}장 (${pct.toFixed(0)}%)` : "";
  $("#q-avg").textContent = q.avg_sec != null ? fmtDur(q.avg_sec) : "–";
  $("#q-eta").textContent = q.eta_sec != null && (by.pending.length || by.running.length) ? fmtDur(q.eta_sec) : "–";
  $("#q-n-pending").textContent = by.pending.length;
  $("#q-n-done").textContent = by.done.length;
  $("#q-n-failed").textContent = by.failed.length;
  $("#btn-pause").hidden = q.paused;
  $("#btn-resume").hidden = !q.paused;
  $("#btn-cancel").disabled = !(by.pending.length || by.running.length);
  $("#btn-clear").disabled = !(by.done.length || by.failed.length);

  // 진행 중
  const run = by.running[0];
  const runEl = $("#q-running");
  if (run) {
    const p = run.est_images ? Math.min(100, (run.done_images / run.est_images) * 100) : 0;
    runEl.replaceChildren(h("div", { class: "job-now" },
      h("div", { class: "info" },
        h("div", { class: "title" }, `${run.name || run.cid} `, h("span", { class: "muted small" }, run.cid)),
        h("div", { class: "muted small" },
          `${MODE_LABEL[run.mode]}${run.expression ? " · " + run.expression : ""} · `,
          run.step ? `지금: ${run.step}` : "준비 중…",
          run.est_images ? ` · ${run.done_images}/${run.est_images}장` : ""),
        h("div", { class: "mini-progress" }, h("div", { style: { width: p + "%" } }))),
      h("button", { class: "btn small danger", onclick: () => cancelJobs([run.id]) }, "취소")));
  } else {
    runEl.replaceChildren(q.pause_reason
      ? h("div", { class: "pause-reason" }, h("b", {}, "자동으로 일시정지했습니다"), "\n", q.pause_reason)
      : h("div", { class: "empty small" }, q.paused ? "일시정지 중입니다. [재개]를 누르면 계속합니다." : "진행 중인 작업이 없습니다"));
  }

  // 대기
  const pend = by.pending.sort((a, b) => a.id - b.id);
  $("#q-h-pending").textContent = pend.length || "";
  fill($("#q-pending"), 
    ...pend.slice(0, MAX_LIST).map(j => jobLine(j, {
      button: h("button", { class: "icon-btn", title: "이 작업 취소", onclick: () => cancelJobs([j.id]) }, "✕"),
    })),
    pend.length > MAX_LIST ? h("li", { class: "more" }, `…외 ${pend.length - MAX_LIST}건`) : null,
    pend.length ? null : h("li", { class: "more" }, "없음"));

  // 완료 (최근 것부터)
  const done = by.done.sort((a, b) => (b.finished || 0) - (a.finished || 0));
  $("#q-h-done").textContent = done.length || "";
  fill($("#q-done"), 
    ...done.slice(0, MAX_LIST).map(j => {
      const li = jobLine(j, {
        tag: j.result === "skipped" ? "건너뜀(이미 있음)" : `${j.done_images}장 · ${fmtDur(j.finished - j.started)}`,
      });
      li.querySelector(".jl-main").append(" ", h("a", { onclick: () => isPartsJob(j) ? Parts.focus(j.cid) : Detail.open(j.cid, [j.cid]) }, "보기"));
      return li;
    }),
    done.length > MAX_LIST ? h("li", { class: "more" }, `…외 ${done.length - MAX_LIST}건`) : null,
    done.length ? null : h("li", { class: "more" }, "없음"));

  // 실패·취소
  const failed = by.failed.sort((a, b) => (b.finished || 0) - (a.finished || 0));
  $("#q-h-failed").textContent = failed.length || "";
  $("#btn-retry-all").hidden = failed.length === 0;
  fill($("#q-failed"), 
    ...failed.slice(0, MAX_LIST).map(j => jobLine(j, {
      err: j.status === "cancelled" ? null : j.error,
      tag: j.status === "cancelled" ? "취소됨" : "실패",
      button: h("button", { class: "btn small ghost", onclick: () => retryJobs([j.id]) }, "다시"),
    })),
    failed.length ? null : h("li", { class: "more" }, "없음"));
}

async function cancelJobs(ids) {
  try {
    const r = await api("POST", "/api/jobs/cancel", ids ? { job_ids: ids } : {});
    if (!ids) toast(`${r.cancelled}건을 취소했습니다`, "warn");
  } catch (e) { toast(e.message, "error"); }
}

async function retryJobs(ids) {
  try {
    const r = await api("POST", "/api/jobs/retry", ids ? { job_ids: ids } : {});
    toast(`${r.added.length}건을 다시 큐에 넣었습니다`, "ok");
  } catch (e) { toast(e.message, "error"); }
}

// 로그
const MAX_LOG = 1500;
function appendLog(ev) {
  const log = $("#log");
  const line = h("div", { class: ev.level === "info" ? "" : ev.level },
    h("span", { class: "t" }, fmtTime(ev.ts)),
    ev.id ? h("span", { class: "cid" }, ev.id) : null,
    ev.msg);
  const atBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 30;
  log.append(line);
  while (log.childElementCount > MAX_LOG) log.firstElementChild.remove();
  if ($("#log-autoscroll").checked && (atBottom || S.tab !== "queue")) log.scrollTop = log.scrollHeight;
}

// 방금 생성된 이미지
const MAX_RECENT = 12;
function addRecentImage(ev) {
  const box = $("#recent-images");
  $(".empty", box)?.remove();
  const src = ev.kind === "base" ? `/api/thumb/${encodeURIComponent(ev.id)}?w=200&v=${ev.seq}` : ev.url;
  const fig = h("figure", { class: ev.kind, dataset: { cid: ev.id }, title: `${ev.id} · ${ev.name} (${fmtTime(ev.ts)})`, onclick: () => Detail.open(ev.id, [ev.id], ev.kind === "expression" ? ev.name : null) },
    h("div", { class: "ri" }, h("img", { src, alt: ev.name, loading: "lazy" })),
    h("figcaption", {}, `${ev.id} · ${ev.name}`));
  box.prepend(fig);
  while (box.childElementCount > MAX_RECENT) box.lastElementChild.remove();
}

function setupQueue() {
  $("#btn-pause").addEventListener("click", () => api("POST", "/api/jobs/pause").catch(e => toast(e.message, "error")));
  $("#btn-resume").addEventListener("click", () => api("POST", "/api/jobs/resume").catch(e => toast(e.message, "error")));
  $("#btn-cancel").addEventListener("click", () => cancelJobs(null));
  $("#btn-clear").addEventListener("click", () => api("POST", "/api/jobs/clear").catch(e => toast(e.message, "error")));
  $("#btn-retry-all").addEventListener("click", () => retryJobs(null));
  $("#btn-log-clear").addEventListener("click", () => $("#log").replaceChildren());
}

// --------------------------------------------------------------------------- //
// SSE
// --------------------------------------------------------------------------- //
function connectEvents() {
  const es = new EventSource("/api/events");
  es.onopen = () => { $("#server-banner").hidden = true; };
  es.onerror = () => { $("#server-banner").hidden = false; };
  es.onmessage = m => {
    let ev;
    try { ev = JSON.parse(m.data); } catch { return; }
    switch (ev.type) {
      case "snapshot":
        $("#log").replaceChildren();
        ev.logs.forEach(appendLog);
        $("#recent-images").replaceChildren(h("div", { class: "empty small" }, "아직 없습니다"));
        ev.images.forEach(addRecentImage);
        S.queue = ev.queue;
        loadJobs().catch(() => {});
        refreshPortraits();
        break;
      case "log": appendLog(ev); break;
      case "job": S.jobs.set(ev.job.id, ev.job); scheduleQueueRender(); break;
      case "queue": S.queue = ev.queue; scheduleQueueRender(); break;
      case "jobs_reset": loadJobs().catch(() => {}); break;
      case "characters": Chars.onExternalChange(ev); break;
      case "portraits_deleted":
        applyDeletedPortraits(ev.ids);
        refreshPortraits();
        break;
      case "export":
        UnityExport.update(ev);
        if (!ev.running && ev.error) toast("Unity 내보내기 실패: " + ev.error, "error", 6000);
        else if (!ev.running && $("#export-dialog").hidden) toast(`Unity 내보내기 완료 (${ev.total}명)`, "ok");
        break;
      case "image": addRecentImage(ev); break;
      case "parts": Parts.refresh(); break;
      case "portrait":
        refreshPortraits();
        if (Detail.id === ev.id) Detail.reload();
        break;
    }
  };
}

// --------------------------------------------------------------------------- //
// 3. 검수 갤러리
// --------------------------------------------------------------------------- //
const Gallery = {
  status: store.get("gal.status", ""), personality: "", q: "", cards: new Map(),
  selected: new Set(), deleting: false, deleteIds: [],
  selectVisible(checked) {
    for (const p of galleryFiltered()) checked ? this.selected.add(p.id) : this.selected.delete(p.id);
    renderGallery();
  },
  updateSelection() {
    const visible = galleryFiltered();
    const count = visible.filter(p => this.selected.has(p.id)).length;
    const all = $("#gal-select-all");
    all.checked = visible.length > 0 && count === visible.length;
    all.indeterminate = count > 0 && count < visible.length;
    all.disabled = this.deleting || !visible.length;
    $("#gal-selection").textContent = this.selected.size ? `${this.selected.size}개 선택` : "";
    $("#btn-gallery-delete").disabled = this.deleting || !this.selected.size;
  },
  openDelete() {
    if (this.deleting || !this.selected.size) return;
    if (Chars.editing) Chars.commit($(".cell-input")?.value ?? "");
    if (Chars.dirty()) return toast("캐릭터 표의 변경 사항을 먼저 저장하거나 되돌린 뒤 삭제하세요.", "warn", 5000);
    this.deleteIds = [...this.selected];
    const names = this.deleteIds.map(id => S.portraits.get(id)?.name || id);
    $("#gallery-delete-summary").textContent = `${names.length}개 항목을 삭제합니다: ${names.slice(0, 8).join(", ")}${names.length > 8 ? " 외" : ""}`;
    $("#gallery-delete-error").hidden = true;
    $("#gallery-delete-dialog").hidden = false;
    $("#gallery-delete-cancel").focus();
  },
  closeDelete() {
    if (!this.deleting) { $("#gallery-delete-dialog").hidden = true; this.deleteIds = []; }
  },
  async deleteConfirmed() {
    if (this.deleting || !this.deleteIds.length) return;
    this.deleting = true;
    $("#gallery-delete-confirm").disabled = $("#gallery-delete-cancel").disabled = true;
    $("#gallery-delete-confirm").textContent = "삭제 중…";
    $("#gallery-delete-error").hidden = true;
    this.updateSelection();
    try {
      const headers = Chars.version ? { "X-Base-Version": Chars.version } : {};
      const result = await api("POST", "/api/portraits/delete", { ids: this.deleteIds }, headers);
      applyDeletedPortraits(result.deleted);
      await Promise.all([Chars.load(), loadPortraits(), loadJobs()]);
      if (result.failed.length) {
        this.deleteIds = result.failed.map(item => item.id);
        this.selected = new Set(this.deleteIds);
        $("#gallery-delete-summary").textContent = `${result.deleted.length}개를 삭제했습니다. 실패한 항목을 다시 시도할 수 있습니다.`;
        $("#gallery-delete-error").textContent = result.failed.map(item => `${item.id}: ${item.error}`).join("\n");
        $("#gallery-delete-error").hidden = false;
      } else {
        $("#gallery-delete-dialog").hidden = true;
        this.deleteIds = [];
        toast(`${result.deleted.length}개 항목과 생성 파일을 영구 삭제했습니다.`, "ok");
      }
    } catch (error) {
      $("#gallery-delete-error").textContent = error.message;
      $("#gallery-delete-error").hidden = false;
    } finally {
      this.deleting = false;
      $("#gallery-delete-confirm").disabled = $("#gallery-delete-cancel").disabled = false;
      $("#gallery-delete-confirm").textContent = "영구 삭제";
      renderGallery();
    }
  },
};

function applyDeletedPortraits(ids) {
  for (const id of ids) { S.portraits.delete(id); Gallery.cards.delete(id); Gallery.selected.delete(id); }
  S.portraitOrder = S.portraitOrder.filter(id => !ids.includes(id));
  if (ids.includes(Detail.id)) Detail.close();
  Detail.list = Detail.list.filter(id => !ids.includes(id));
  $$("#recent-images figure").filter(el => ids.includes(el.dataset.cid)).forEach(el => el.remove());
  if (S.tab === "gallery") renderGallery();
}

function galleryFiltered() {
  const q = Gallery.q.toLowerCase();
  const def = S.ex ? S.ex.default_personality : "";
  return S.portraitOrder.map(id => S.portraits.get(id)).filter(p => {
    if (typeOf(p) !== S.imageType) return false;
    if (Gallery.status && p.status !== Gallery.status) return false;
    if (Gallery.personality) {
      const ps = (p.personality || def).split("|").map(s => s.trim());
      if (!ps.includes(Gallery.personality)) return false;
    }
    if (q && !(p.id.toLowerCase().includes(q) || (p.name || "").toLowerCase().includes(q))) return false;
    return true;
  });
}

function galleryCard(p) {
  let c = Gallery.cards.get(p.id);
  const key = `${p.status}|${p.thumb}|${p.name}|${p.expr_count}|${Gallery.selected.has(p.id)}|${Gallery.deleting}`;
  if (c && c.dataset.key === key) return c;
  c = h("div", { class: "pcard" + (Gallery.selected.has(p.id) ? " selected" : ""), tabIndex: 0, dataset: { id: p.id, key } },
    h("label", { class: "gallery-check", title: "삭제할 항목 선택", onclick: event => event.stopPropagation(), onkeydown: event => event.stopPropagation() },
      h("input", { type: "checkbox", checked: Gallery.selected.has(p.id), disabled: Gallery.deleting, "aria-label": `${p.name || p.id} 삭제 선택`,
        onchange: event => { event.target.checked ? Gallery.selected.add(p.id) : Gallery.selected.delete(p.id); renderGallery(); } })),
    h("div", { class: "thumb" },
      p.thumb ? h("img", { src: p.thumb, alt: p.name, loading: "lazy", decoding: "async" }) : h("div", { class: "ph" }, "미생성"),
      badge(p.status),
      p.expr_count ? h("span", { class: "ec" }, `표정 ${p.expr_count + 1}`) : null),
    h("div", { class: "cap" },
      h("div", { class: "nm" }, p.name || p.id),
      h("div", { class: "sub" }, p.id + (p.personality ? ` · ${p.personality}` : ""))));
  Gallery.cards.set(p.id, c);
  return c;
}

function renderGallery() {
  // 상태 필터 버튼 (개수 포함)
  const counts = { "": 0, none: 0, base: 0, done: 0, approved: 0 };
  for (const p of S.portraits.values()) { if (typeOf(p) !== S.imageType) continue; counts[""]++; counts[p.status]++; }
  const seg = $("#gal-status");
  seg.replaceChildren(...[["", "전체"], ["none", "미생성"], ["base", "후처리 대기"], ["done", "완료"], ["approved", "승인됨"]].map(([v, label]) =>
    h("button", { class: Gallery.status === v ? "active" : "", onclick: () => { Gallery.status = v; store.set("gal.status", v); renderGallery(); } },
      label, h("span", { class: "n" }, counts[v]))));
  // 성격 필터
  const sel = $("#gal-personality");
  const opts = S.ex ? Object.keys(S.ex.personalities) : [];
  if (sel.options.length !== opts.length + 1) {
    sel.replaceChildren(h("option", { value: "" }, "모든 성격"), ...opts.map(o => h("option", { value: o }, o)));
    sel.value = Gallery.personality;
  }
  const list = galleryFiltered();
  for (const id of Gallery.selected) if (!S.portraits.has(id)) Gallery.selected.delete(id);
  $("#gallery").replaceChildren(...list.map(galleryCard));
  $("#gal-count").textContent = `${list.length}명`;
  Gallery.updateSelection();
  const empty = $("#gal-empty");
  empty.hidden = list.length > 0;
  empty.textContent = S.portraits.size ? "조건에 맞는 캐릭터가 없습니다" : "캐릭터가 없습니다";
}

function setupGallery() {
  $("#gal-select-all").addEventListener("change", event => {
    Gallery.selectVisible(event.target.checked);
  });
  $("#btn-gallery-delete").addEventListener("click", () => Gallery.openDelete());
  $("#gallery-delete-confirm").addEventListener("click", () => Gallery.deleteConfirmed());
  $$("#gallery-delete-dialog [data-close]").forEach(el => el.addEventListener("click", () => Gallery.closeDelete()));
  document.addEventListener("keydown", event => { if (event.key === "Escape") Gallery.closeDelete(); });
  $("#gal-personality").addEventListener("change", e => { Gallery.personality = e.target.value; renderGallery(); });
  $("#gal-search").addEventListener("input", debounce(e => { Gallery.q = e.target.value.trim(); renderGallery(); }, 120));
  const size = $("#gal-size");
  size.value = store.get("gal.size", 170);
  document.documentElement.style.setProperty("--card-w", size.value + "px");
  size.addEventListener("input", () => {
    document.documentElement.style.setProperty("--card-w", size.value + "px");
    store.set("gal.size", +size.value);
  });
  const open = el => { const c = el.closest(".pcard"); if (c) Detail.open(c.dataset.id, galleryFiltered().map(p => p.id)); };
  $("#gallery").addEventListener("click", e => open(e.target));
  $("#gallery").addEventListener("keydown", e => { if (e.key === "Enter") open(e.target); });
}

// --------------------------------------------------------------------------- //
// 승인된 것만 Unity 폴더로 내보내기
// --------------------------------------------------------------------------- //
const UnityExport = {
  async open() {
    let info;
    try { info = await api("GET", "/api/export/unity"); } catch (e) { return toast(e.message, "error"); }
    $("#exp-dest").value = info.dest || "";
    this.warn();
    $("#exp-info").textContent = info.approved
      ? `승인된 캐릭터 ${info.approved}명을 내보냅니다.`
      : "승인된 캐릭터가 없습니다. 검수 화면에서 먼저 [승인]하세요.";
    $("#exp-run").disabled = !info.approved || info.state.running;
    $("#export-dialog").hidden = false;
    this.update(info.state);
  },

  close() { $("#export-dialog").hidden = true; },

  warn() {
    const v = $("#exp-dest").value.trim();
    const w = $("#exp-warn");
    let msg = "";
    if (v && !/^([a-zA-Z]:[\\/]|\\\\|\/)/.test(v)) msg = "절대 경로를 입력하세요 (예: D:\\MyGame\\Assets\\Resources\\Portraits)";
    else if (v && !/[\\/]resources([\\/]|$)/i.test(v)) msg = "NpcPortrait.cs 는 Resources.Load 로 읽으므로 Assets/Resources 아래 폴더여야 합니다 (기본 Resources/Portraits).";
    w.textContent = msg;
    w.hidden = !msg;
  },

  async run() {
    const dest = $("#exp-dest").value.trim();
    if (!dest) return toast("내보낼 폴더를 입력하세요", "warn");
    $("#exp-run").disabled = true;
    try {
      await api("POST", "/api/export/unity", { dest, prune: $("#exp-prune").checked });
    } catch (e) {
      $("#exp-run").disabled = false;
      const w = $("#exp-warn");  // 대화상자 안에 바로 보여 줌
      w.textContent = "✗ " + e.message;
      w.hidden = false;
    }
  },

  /** SSE "export" 이벤트 */
  update(st) {
    if (!st || st.total == null) return;
    $("#exp-progress").hidden = false;
    const pct = st.total ? (st.done / st.total) * 100 : 100;
    $("#exp-bar").style.width = pct.toFixed(1) + "%";
    const counts = `복사 ${st.copied} · 변경 없음 ${st.skipped} · 삭제 ${st.removed}`;
    if (st.error) $("#exp-status").textContent = "실패: " + st.error;
    else if (st.running) $("#exp-status").textContent = `${st.done} / ${st.total}명 · ${counts}`;
    else $("#exp-status").textContent = `완료: ${st.total}명 → ${st.dest} (${counts})`;
    $("#exp-run").disabled = !!st.running;
    if (!st.running && !st.error && !$("#export-dialog").hidden) $("#exp-run").textContent = "다시 내보내기";
  },

  setup() {
    $("#btn-unity-export").addEventListener("click", () => this.open());
    $("#exp-run").addEventListener("click", () => this.run());
    $("#exp-dest").addEventListener("input", () => this.warn());
    $$("#export-dialog [data-close]").forEach(el => el.addEventListener("click", () => this.close()));
    document.addEventListener("keydown", e => { if (e.key === "Escape" && !$("#export-dialog").hidden) this.close(); });
  },
};

// --------------------------------------------------------------------------- //
// 상세 보기 (게임과 같은 방식: base 위에 meta.json 좌표로 오버레이)
// --------------------------------------------------------------------------- //
const Detail = {
  id: null, list: [], data: null, exprIdx: 0, bg: store.get("detail.bg", "check"), preload: [],
  autoNext: store.get("detail.autoNext", true),
  chainFull: store.get("detail.chainFull", false),
  rerollArmed: 0,   // [base 다시 뽑기] 두 번 누르기 확인용 시각
  view: store.get("detail.view", "portrait"),   // portrait | dialogue
  debug: store.get("detail.debug", false),
  hideBase: false,
  dlgPos: store.get("dlg.pos", "left"),
  dlgIdx: 0,
  dlgTimer: null,
  dlgFull: "",      // 지금 줄의 전체 텍스트 (타자 효과 중이면 일부만 보임)

  async open(id, list, exprName = null) {
    this.list = list && list.length ? list : [id];
    this.id = id;
    $("#detail").hidden = false;
    await this.load(exprName);
  },

  close() {
    clearInterval(this.dlgTimer);
    this.dlgTimer = null;
    $("#detail").hidden = true;
    this.id = null;
    this.data = null;
  },

  async reload() {
    const cur = this.currentExpr();
    await this.load(cur ? cur.name : null);
  },

  async load(exprName) {
    const id = this.id;
    let d;
    try {
      d = await api("GET", `/api/portraits/${encodeURIComponent(id)}`);
    } catch (e) {
      toast(e.message, "error");
      return;
    }
    if (this.id !== id) return;  // 그 사이 다른 캐릭터로 넘어감
    this.data = d;
    this.rerollArmed = 0;
    const exprs = this.exprs();
    const idx = exprName ? exprs.findIndex(e => e.name === exprName) : -1;
    this.exprIdx = idx >= 0 ? idx : 0;
    // 표정 즉시 전환을 위해 오버레이 미리 받기
    this.preload = exprs.filter(e => e.url).map(e => { const im = new Image(); im.src = e.url; return im; });
    this.render();
  },

  /** 만든 표정 + (meta 가 있으면) 계획에는 있지만 아직 없는 표정 */
  exprs() {
    const d = this.data;
    if (!d) return [];
    if (!d.expressions.length) return [{ name: "neutral", file: "", url: null }];
    const have = new Set(d.expressions.map(e => e.name));
    const missing = (d.planned || []).filter(n => !have.has(n)).map(n => ({ name: n, file: "", url: null, missing: true }));
    return [...d.expressions, ...missing];
  },

  currentExpr() { return this.exprs()[this.exprIdx]; },
  hasMeta() { return this.data && (this.data.status === "done" || this.data.status === "approved"); },
  jobs() {
    return [...S.jobs.values()].filter(j => j.cid === this.id && (j.status === "pending" || j.status === "running"));
  },

  render() {
    const d = this.data;
    if (!d) return;
    const kind = typeOf(d);
    for (const id of ["d-exprs-title", "d-exprs", "d-expr-info"]) $("#" + id).hidden = kind !== "character";
    $("#d-debug").closest("label").hidden = kind !== "character";
    const stages = d.stages || {};
    $("#d-stages").textContent = (d.stale ? "유형이 변경되었습니다. 이미지를 다시 생성하세요. · " : "") +
      Object.entries({ image: "이미지", expressions: "표정", layers: "레이어" })
        .filter(([key]) => stages[key] != null).map(([key, name]) => `${name}: ${stages[key] ? "완료" : "미완료"}`).join(" · ");
    const layerBox = $("#d-layers");
    layerBox.hidden = kind === "monster" || !d.layers?.available;
    fill(layerBox, h("h4", {}, d.layers?.current ? "레이어 분리 결과" : "이전 레이어 결과 (재분리 필요)"),
      d.layers?.preview ? h("img", { src: d.layers.preview, alt: "분리 레이어 합성 미리보기" }) : null,
      d.layers?.psd_url ? h("a", { class: "btn", href: d.layers.psd_url }, "PSD 다운로드") : null);
    $("#d-name").textContent = d.name || d.id;
    const st = $("#d-status");
    st.className = `badge ${d.status}`;
    st.textContent = STATUS_LABEL[d.status];
    const sub = [d.id];
    if (d.seed != null) sub.push(`seed ${d.seed}`);
    if (d.size) sub.push(`${d.size[0]}×${d.size[1]}`);
    $("#d-sub").textContent = sub.join(" · ");
    const pos = this.list.indexOf(this.id);
    $("#d-pos").textContent = this.list.length > 1 ? `${pos + 1} / ${this.list.length}` : "";
    $("#d-prev").disabled = pos <= 0;
    $("#d-next").disabled = pos < 0 || pos >= this.list.length - 1;

    // 무대
    const stage = $("#stage");
    stage.className = this.view === "dialogue" ? "stage" : `stage bg-${this.bg}`;
    if (d.size) stage.style.aspectRatio = `${d.size[0]} / ${d.size[1]}`;
    const base = $("#stage-base");
    base.hidden = !d.base_url;
    $("#stage-empty").hidden = !!d.base_url;
    if (d.base_url && base.getAttribute("src") !== d.base_url) base.src = d.base_url;
    $$("#d-bg button").forEach(b => b.classList.toggle("active", b.dataset.bg === this.bg));

    // 표정 버튼
    fill($("#d-exprs"), ...this.exprs().map((e, i) => h("button", {
      class: (i === this.exprIdx ? "active " : "") + (e.missing ? "missing" : ""),
      onclick: () => this.setExpr(i), title: e.missing ? "아직 만들지 않은 표정" : e.name,
    }, i < 9 ? h("span", { class: "k" }, i + 1) : null, e.name)));
    this.renderView();
    this.renderOverlay();
    this.renderActions();
    this.renderJobs();

    // 정보
    const row = d.row || {};
    const info = kind === "monster" ? [["유형", "몬스터"], ["설명", row.prompt]] : [["성격", kind === "character" ? row.personality || (S.ex ? `(기본) ${S.ex.default_personality}` : "") : ""],
      ["대상", [row.subject, row.age].filter(Boolean).join(", ")], ["머리", row.hair], ["눈", row.eyes],
      ["옷", row.outfit], ["기타", row.extra]];
    if (!d.in_csv) info.unshift(["주의", "characters.csv 에 없는 캐릭터 (결과물만 남아 있음)"]);
    fill($("#d-info"), ...info.filter(([, v]) => v).flatMap(([k, v]) => [h("dt", {}, k), h("dd", {}, v)]));
  },

  renderOverlay() {
    const d = this.data;
    const e = this.currentExpr();
    const ov = $("#stage-overlay");
    if (e && e.url && d.size) {
      const [W, H] = d.size;
      Object.assign(ov.style, {
        left: (e.x / W) * 100 + "%", top: (e.y / H) * 100 + "%",
        width: (e.w / W) * 100 + "%", height: (e.h / H) * 100 + "%",
      });
      ov.src = e.url;
      ov.hidden = false;
    } else {
      ov.hidden = true;
      ov.removeAttribute("src");
    }
    $$("#d-exprs button").forEach((b, i) => b.classList.toggle("active", i === this.exprIdx));
    let info = "";
    if (e && e.missing) info = "아직 만들지 않은 표정입니다 (지금은 base 를 보여 줍니다)";
    else if (e && e.file) {
      info = `${e.file} · ${e.w}×${e.h} @ (${e.x}, ${e.y})`;
      if (e.denoise != null) info += ` · denoise ${e.denoise}`;
      if (e.seed != null && d.seed != null && e.seed !== d.seed) info += ` · seed ${e.seed}`;
    } else if (e) {
      info = "neutral = base 그대로 (오버레이 없음)";
    }
    $("#d-expr-info").textContent = info;
    this.renderRegen();
    this.renderDebug();
  },

  // ---- 대화창 미리보기 ---- //
  renderView() {
    const dlg = this.view === "dialogue";
    $("#stage-wrap").classList.toggle("dialogue", dlg);
    const bg = this.bg === "check" ? "scene" : this.bg;  // 대화창에서 체크무늬는 어색하므로 장면 배경
    $("#scene").className = "scene" + (dlg ? ` pos-${this.dlgPos} bg-${bg}` : "");
    $("#dlg-box").hidden = !dlg;
    $("#d-dialogue").hidden = !dlg;
    $$("#d-view button").forEach(b => b.classList.toggle("active", b.dataset.view === this.view));
    $$("#dlg-pos button").forEach(b => b.classList.toggle("active", b.dataset.pos === this.dlgPos));
    if (dlg) {
      $("#dlg-name").textContent = this.data.name || this.data.id;
      this.showLine(this.dlgIdx, false);
    } else {
      clearInterval(this.dlgTimer);
      this.dlgTimer = null;
    }
  },

  /** 대사 한 줄: "smile: 텍스트" / "[smile] 텍스트" 면 표정도 바꿈 (그런 표정이 없으면 그냥 텍스트) */
  parseLine(line) {
    const m = /^\s*(?:\[([\w-]+)\]|([\w-]+)\s*[:：])\s*(.*)$/.exec(line);
    if (m) {
      const name = m[1] || m[2];
      if (this.exprs().some(e => e.name === name) || (S.ex && name in S.ex.expressions)) return { expr: name, text: m[3] };
    }
    return { expr: null, text: line };
  },

  dlgLines() {
    return $("#dlg-script").value.split("\n").map(s => s.trimEnd()).filter(s => s.trim());
  },

  showLine(i, animate = true) {
    const lines = this.dlgLines();
    clearInterval(this.dlgTimer);
    this.dlgTimer = null;
    const text = $("#dlg-text");
    if (!lines.length) { text.textContent = ""; this.dlgFull = ""; $("#dlg-next").hidden = true; return; }
    this.dlgIdx = ((i % lines.length) + lines.length) % lines.length;
    const { expr, text: full } = this.parseLine(lines[this.dlgIdx]);
    if (expr) {
      const idx = this.exprs().findIndex(e => e.name === expr);
      if (idx >= 0 && idx !== this.exprIdx) { this.exprIdx = idx; this.renderOverlay(); }
    }
    this.dlgFull = full;
    $("#dlg-next").hidden = true;
    const done = () => { $("#dlg-next").hidden = false; $("#dlg-next").textContent = this.dlgIdx < lines.length - 1 ? "▼" : "↺"; };
    if (!animate) { text.textContent = full; done(); return; }
    let n = 0;
    text.textContent = "";
    this.dlgTimer = setInterval(() => {
      n += 1;
      text.textContent = full.slice(0, n);
      if (n >= full.length) { clearInterval(this.dlgTimer); this.dlgTimer = null; done(); }
    }, 28);
  },

  /** 클릭/Space: 타자 중이면 줄 끝까지, 아니면 다음 줄 */
  advance() {
    if (this.dlgTimer) {
      clearInterval(this.dlgTimer);
      this.dlgTimer = null;
      $("#dlg-text").textContent = this.dlgFull;
      $("#dlg-next").hidden = false;
      return;
    }
    this.showLine(this.dlgIdx + 1);
  },

  setView(v) {
    this.view = v;
    store.set("detail.view", v);
    this.render();
  },

  // ---- 디버그 오버레이 (합성 마스크, 얼굴 박스, 크롭, 오버레이 위치) ---- //
  renderDebug() {
    const d = this.data;
    const w = d && d.work;
    const has = !!(w && w.ellipse && w.crop && d.size);
    const on = this.debug && has && typeOf(d) === "character";
    const mask = $("#stage-mask"), svg = $("#stage-debug"), info = $("#d-debug-info");
    mask.hidden = !on;
    svg.toggleAttribute("hidden", !on);  // SVG 요소에는 .hidden 속성이 없으므로 attribute 로
    $("#d-debug").checked = this.debug;
    $("#stage-base").style.visibility = on && this.hideBase ? "hidden" : "";
    info.hidden = !this.debug || typeOf(d) !== "character";
    if (!this.debug || typeOf(d) !== "character") return;
    if (!has) {
      fill(info, h("span", { class: "muted" }, "얼굴 정보(work.json)가 없습니다. 표정까지 생성하면 표시됩니다."));
      return;
    }
    const [W, H] = d.size;
    const src = `/api/portraits/${encodeURIComponent(d.id)}/mask.png?v=${w.base_src_mtime || ""}_${w.crop.join("_")}`;
    if (mask.getAttribute("src") !== src) mask.src = src;
    const NS = "http://www.w3.org/2000/svg";
    const el = (tag, attrs) => { const e = document.createElementNS(NS, tag); for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v); return e; };
    const fs = Math.round(W * 0.022);
    const label = (x, y, t, cls) => { const e = el("text", { x, y, class: cls, "font-size": fs }); e.textContent = t; return e; };
    const [fx0, fy0, fx1, fy1] = w.face, [cx0, cy0, cx1, cy1] = w.crop, [ex0, ey0, ex1, ey1] = w.ellipse;
    const ecfg = w.expression_cfg || {};
    const fpx = Math.max(2, Math.floor((ex1 - ex0) * (ecfg.feather || 0)));
    svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
    svg.setAttribute("preserveAspectRatio", "none");
    const kids = [
      el("rect", { x: cx0, y: cy0, width: cx1 - cx0, height: cy1 - cy0, class: "dbg-crop" }),
      label(cx0 + 6, cy0 + fs + 2, "크롭", "dbg-crop-t"),
      el("ellipse", { cx: (ex0 + ex1) / 2, cy: (ey0 + ey1) / 2, rx: (ex1 - ex0) / 2, ry: (ey1 - ey0) / 2, class: "dbg-mask" }),
      el("ellipse", { cx: (ex0 + ex1) / 2, cy: (ey0 + ey1) / 2, rx: Math.max(1, (ex1 - ex0) / 2 - fpx), ry: Math.max(1, (ey1 - ey0) / 2 - fpx), class: "dbg-feather" }),
      el("rect", { x: fx0, y: fy0, width: fx1 - fx0, height: fy1 - fy0, class: "dbg-face" }),
      label(fx0 + 4, fy0 - 6, "얼굴", "dbg-face-t"),
    ];
    const e = this.currentExpr();
    if (e && e.file) {
      kids.push(el("rect", { x: e.x, y: e.y, width: e.w, height: e.h, class: "dbg-ov" }));
      kids.push(label(e.x + 4, e.y + e.h - 6, `오버레이 ${e.w}×${e.h}`, "dbg-ov-t"));
    }
    svg.replaceChildren(...kids);

    const cur = d.geometry_cfg || {};
    const diff = Object.keys(ecfg).filter(k => cur[k] != null && cur[k] !== ecfg[k]);
    const side = cx1 - cx0;
    fill(info,
      h("div", { class: "legend" },
        h("span", { class: "lg face" }, "얼굴 박스"), h("span", { class: "lg mask" }, "마스크 (실선: 인페인트, 점선: 페더 시작)"),
        h("span", { class: "lg crop" }, "크롭"), h("span", { class: "lg ov" }, "오버레이"), h("span", { class: "lg fill" }, "합성 가중치")),
      h("div", { class: "muted small" },
        `얼굴 ${Math.round(fx1 - fx0)}×${Math.round(fy1 - fy0)} · 크롭 ${side}px → ${ecfg.inpaint_size}px (×${(ecfg.inpaint_size / side).toFixed(2)}) · `
        + `crop_scale ${ecfg.crop_scale} · mask_expand ${ecfg.mask_expand} · feather ${ecfg.feather} (${fpx}px)`),
      diff.length ? h("div", { class: "warnline small" },
        `지금 설정과 다름: ${diff.map(k => `${k} ${ecfg[k]} → ${cur[k]}`).join(", ")}. 이 캐릭터는 base 를 다시 처리해야 새 값이 적용됩니다.`) : null,
      h("label", { class: "check small" }, h("input", { type: "checkbox", checked: this.hideBase,
        onchange: ev => { this.hideBase = ev.target.checked; this.renderDebug(); } }), "base 숨기기 (오버레이 경계 확인)"));
  },

  toggleDebug() {
    this.debug = !this.debug;
    store.set("detail.debug", this.debug);
    this.renderDebug();
  },

  // ---- 표정 하나 다시 만들기 ---- //
  renderRegen() {
    const d = this.data;
    const e = this.currentExpr();
    const box = $("#d-regen");
    const show = !!(d && typeOf(d) === "character" && e && d.in_csv && d.meta && e.name !== "neutral");
    box.hidden = !show;
    if (!show) return;
    const def = e.denoise ?? (d.denoise_defaults || {})[e.name] ?? 0.55;
    $("#d-denoise").value = def;
    $("#d-denoise-range").value = def;
    $("#d-newseed").checked = !e.missing;  // 새로 만드는 표정은 base 와 같은 seed 가 기본
    $("#d-btn-regen").textContent = e.missing ? "이 표정 만들기" : "이 표정 다시 만들기";
  },

  async regen() {
    const e = this.currentExpr();
    const denoise = parseFloat($("#d-denoise").value);
    if (!(denoise > 0 && denoise <= 1)) return toast("denoise 는 0~1 사이로 입력하세요", "warn");
    await submitJobs("expression", [this.id], {
      expression: e.name, denoise, new_seed: $("#d-newseed").checked, force: false,
    });
  },

  // ---- 검수 동작 (승인 / base 다시 뽑기 / 생성) ---- //
  renderActions() {
    const d = this.data;
    const box = $("#d-actions");
    const items = [];
    const busy = this.jobs().length > 0;
    const kind = typeOf(d);
    if (d.in_csv && d.base_url && !d.stale && kind !== "monster") {
      items.push(h("div", { class: "btn-row" },
        kind === "character" ? h("button", { class: "btn", disabled: busy,
          onclick: () => submitJobs("expressions", [d.id], { force: false }) }, "표정 생성") : null,
        h("button", { class: "btn", disabled: busy,
          onclick: () => submitJobs("layers", [d.id], { force: false }) }, "레이어 분리")));
    }
    if (!d.in_csv) {
      items.push(h("p", { class: "muted small" }, "characters.csv 에 없는 캐릭터라 생성 작업은 할 수 없습니다."));
    }
    if (this.hasMeta()) {
      if (d.status === "approved") {
        items.push(h("div", { class: "approve-row" },
          h("span", { class: "approved-mark" }, "✓ 승인됨"),
          d.approved_at ? h("span", { class: "muted small" }, d.approved_at.replace("T", " ")) : null,
          h("span", { class: "spacer" }),
          h("button", { class: "btn small ghost", onclick: () => this.approve(false) }, "승인 취소")));
      } else {
        items.push(h("div", { class: "approve-row" },
          h("button", { class: "btn primary", disabled: busy, title: busy ? "생성 작업이 끝난 뒤 승인할 수 있습니다" : "meta.json 에 승인 기록 (단축키 A)",
            onclick: () => this.approve(true) }, "✓ 승인", h("kbd", {}, "A")),
          h("label", { class: "check small" }, h("input", { type: "checkbox", checked: this.autoNext,
            onchange: ev => { this.autoNext = ev.target.checked; store.set("detail.autoNext", this.autoNext); } }), "승인하면 다음 캐릭터로")));
      }
    }
    if (d.in_csv && d.status === "none") {
      items.push(h("div", { class: "btn-row" },
        h("button", { class: "btn", onclick: () => submitJobs("base", [d.id], { force: false }) }, "base 생성"),
        kind !== "monster" ? h("button", { class: "btn primary", onclick: () => submitJobs("full", [d.id], { force: false }) }, "전체 생성") : null));
    }
    if (d.in_csv && d.status === "base" && kind !== "monster") {
      items.push(h("div", { class: "btn-row" },
        h("button", { class: "btn primary", onclick: () => submitJobs("full", [d.id], { force: false }) }, "전체 작업 이어서")));
    }
    if (d.in_csv && d.status !== "none") {
      const n = d.expressions.filter(e => e.file).length;
      const armed = Date.now() - this.rerollArmed < 4000;
      const btn = h("button", { class: "btn " + (armed ? "danger" : ""), onclick: () => this.reroll() },
        armed ? `한 번 더 누르면 실행${n ? ` (표정 ${n}개${d.status === "approved" ? "·승인" : ""} 삭제)` : ""}` : "base 다시 뽑기 (새 seed)");
      items.push(h("div", { class: "reroll" },
        h("div", { class: "btn-row" }, btn,
          kind !== "monster" ? h("label", { class: "check small", title: "새 이미지 이후 전체 작업을 실행" },
            h("input", { type: "checkbox", checked: this.chainFull,
              onchange: ev => { this.chainFull = ev.target.checked; store.set("detail.chainFull", this.chainFull); } }), "전체 작업 이어서") : null),
        h("div", { class: "muted small hint" }, "새 seed 는 characters.csv 의 seed 칸에도 기록됩니다." + (n ? " 지금 표정은 새 base 와 맞지 않아 삭제됩니다." : ""))));
    }
    fill(box, ...items);
  },

  renderJobs() {
    const box = $("#d-jobs");
    if (!this.data) return;
    const jobs = this.jobs();
    box.hidden = jobs.length === 0;
    if (!jobs.length) return;
    fill(box, ...jobs.map(j => h("div", { class: "d-job " + j.status },
      h("span", { class: "dot" }),
      j.status === "running" ? "생성 중: " : "대기 중: ",
      MODE_LABEL[j.mode] + (j.expression ? ` · ${j.expression}` : "") + (j.new_seed ? " · 새 seed" : ""),
      j.status === "running" && j.step ? h("span", { class: "muted" }, ` (${j.step})`) : null)));
    this.renderActions();
  },

  async reroll() {
    if (Date.now() - this.rerollArmed > 4000) {  // 첫 번째 클릭: 확인 상태로
      this.rerollArmed = Date.now();
      this.renderActions();
      setTimeout(() => { if (this.rerollArmed && Date.now() - this.rerollArmed >= 4000) this.renderActions(); }, 4100);
      return;
    }
    this.rerollArmed = 0;
    const res = await submitJobs(this.chainFull && typeOf(this.data) !== "monster" ? "full" : "base", [this.id], { new_seed: true, force: false });
    this.renderActions();
    if (res && res.added.length) this.renderJobs();
  },

  async approve(on) {
    const id = this.id;
    try {
      const r = await api("POST", `/api/portraits/${encodeURIComponent(id)}/approve`, { approved: on });
      if (this.id !== id) return;
      this.data.status = r.status;
      if (on && this.autoNext && this.list.indexOf(id) < this.list.length - 1) {
        toast(`${this.data.name || id} 승인`, "ok", 1500);
        this.go(1);
      } else {
        await this.reload();
      }
    } catch (e) {
      toast(e.message, "error", 5000);
    }
  },

  setExpr(i) {
    const n = this.exprs().length;
    if (!n) return;
    this.exprIdx = (i + n) % n;
    this.renderOverlay();
  },

  go(delta) {
    const pos = this.list.indexOf(this.id);
    const next = this.list[pos + delta];
    if (next == null) return;
    const cur = this.currentExpr();
    this.id = next;
    this.load(cur ? cur.name : null);  // 같은 표정을 유지하며 다음 캐릭터로
  },

  setup() {
    $$("#detail [data-close]").forEach(el => el.addEventListener("click", () => this.close()));
    $("#d-prev").addEventListener("click", () => this.go(-1));
    $("#d-next").addEventListener("click", () => this.go(1));
    $$("#d-bg button").forEach(b => b.addEventListener("click", () => {
      this.bg = b.dataset.bg;
      store.set("detail.bg", this.bg);
      this.render();
    }));
    const rg = $("#d-denoise-range"), nb = $("#d-denoise");
    rg.addEventListener("input", () => { nb.value = rg.value; });
    rg.addEventListener("change", () => rg.blur());  // 놓은 뒤에는 ←/→ 가 다시 표정 전환이 되도록
    nb.addEventListener("input", () => { rg.value = nb.value; });
    nb.addEventListener("keydown", e => { if (e.key === "Enter") { e.preventDefault(); this.regen(); } });
    $("#d-btn-regen").addEventListener("click", () => this.regen());
    $$("#d-view button").forEach(b => b.addEventListener("click", () => this.setView(b.dataset.view)));
    $("#d-debug").addEventListener("change", () => this.toggleDebug());
    $$("#dlg-pos button").forEach(b => b.addEventListener("click", () => {
      this.dlgPos = b.dataset.pos;
      store.set("dlg.pos", this.dlgPos);
      this.renderView();
    }));
    const script = $("#dlg-script");
    script.value = store.get("dlg.script", "안녕하세요. 처음 뵙겠습니다.\nsmile: 만나서 반가워요!\nsurprised: 어? 벌써 가시려고요?\nsad: ...조심히 가세요.");
    script.addEventListener("input", debounce(() => {
      store.set("dlg.script", script.value);
      this.showLine(Math.min(this.dlgIdx, Math.max(this.dlgLines().length - 1, 0)), false);
    }, 200));
    $("#dlg-restart").addEventListener("click", () => this.showLine(0));
    $("#scene").addEventListener("click", () => { if (this.view === "dialogue") this.advance(); });
    document.addEventListener("keydown", e => {
      if ($("#detail").hidden) return;
      if (e.target.matches("input, textarea, select")) {
        if (e.key === "Escape") e.target.blur();
        return;
      }
      if (e.ctrlKey || e.metaKey || e.altKey) return;
      const k = e.key.toLowerCase();
      if (e.key === "Escape") this.close();
      else if (k === "t" || k === "ㅅ") this.setView(this.view === "dialogue" ? "portrait" : "dialogue");
      else if (k === "d" || k === "ㅇ") this.toggleDebug();
      else if ((e.key === " " || e.key === "Enter") && this.view === "dialogue") this.advance();
      else if (e.key === "ArrowRight") this.setExpr(this.exprIdx + 1);
      else if (e.key === "ArrowLeft") this.setExpr(this.exprIdx - 1);
      else if (e.key === "ArrowDown" || e.key === "PageDown") this.go(1);
      else if (e.key === "ArrowUp" || e.key === "PageUp") this.go(-1);
      else if (/^[1-9]$/.test(e.key)) { if (+e.key <= this.exprs().length) this.setExpr(+e.key - 1); }
      else if ((e.key.toLowerCase() === "a" || e.key === "ㅁ") && this.hasMeta() && this.data.status !== "approved" && !this.jobs().length) this.approve(true);
      else return;
      e.preventDefault();
    });
  },
};

// --------------------------------------------------------------------------- //
// 4. 파츠 조합 (parts.json 의 소스 → SeeThrough 레이어 → 섞어 보기). 생성은 같은 작업 큐
// --------------------------------------------------------------------------- //
const Parts = {
  data: null,          // GET /api/parts
  draft: [],           // 표에서 편집 중인 소스 [{id, seed, tags, from?, align?}] (from: 가져온 캐릭터 id)
  base: "[]",          // draft 를 만든 시점의 저장본 (JSON) — 변경 감지용
  selected: new Set(),
  current: null,       // 미리보기 중인 소스 id
  view: store.get("parts.view", "check"),
  picks: {},           // 그룹 → 소스 id ("" 는 없음)
  loaded: false,

  dirty() { return JSON.stringify(this.draft) !== this.base; },
  saved(id) { return this.data ? this.data.sources.find(s => s.id === id) : null; },
  layered() { return this.data ? this.data.sources.filter(s => s.layers != null) : []; },
  resetDraft() {
    this.draft = this.data.sources.map(s => ({ id: s.id, seed: s.seed, tags: s.tags, ...(s.from ? { from: s.from, align: s.align } : {}) }));
    this.base = JSON.stringify(this.draft);
  },

  async load() {
    try {
      this.data = await api("GET", "/api/parts");
    } catch (e) {
      this.showErrors([e.message], "parts.json 을 읽지 못했습니다");
      return;
    }
    const keepDraft = this.loaded && this.dirty();
    this.loaded = true;
    if (keepDraft) {
      // 편집 중이면 표를 다시 그리지 않음 (입력 중인 칸이 사라지지 않게)
      this.renderStatus(); this.renderPreview(); this.renderPicks();
    } else {
      this.resetDraft();
      if (!this.current && this.data.sources.length) this.current = this.data.sources[0].id;
      this.render();
    }
  },
  refresh: debounce(function () { if (Parts.loaded) Parts.load(); }, 300),
  open() { this.loaded ? this.render() : this.load(); },
  async focus(id) {
    showTab("parts");
    if (!this.loaded) await this.load();
    this.setCurrent(id);
  },

  // ---- 표 ---- //
  render() {
    if (!this.data) return;
    const jobs = this.jobMap();
    fill($("#parts-table tbody"), this.draft.map(r => this.rowEl(r, jobs)));
    $("#parts-empty").hidden = this.draft.length > 0;
    this.renderBar(); this.renderPreview(); this.renderPicks();
  },
  rowEl(r, jobs) {
    const s = this.saved(r.id);
    const tr = h("tr", { dataset: { id: r.id }, class: [this.selected.has(r.id) ? "selected" : "", this.current === r.id ? "current" : ""].join(" ") },
      h("td", { class: "col-check" }, h("input", {
        type: "checkbox", checked: this.selected.has(r.id),
        onchange: e => { e.target.checked ? this.selected.add(r.id) : this.selected.delete(r.id); tr.classList.toggle("selected", e.target.checked); this.renderBar(); },
      })),
      h("td", { class: "thumb" }, s && s.thumb ? h("img", { src: s.thumb, alt: "", loading: "lazy" }) : h("div", { class: "ph" })),
      h("td", {}, h("input", {
        class: "sid", value: r.id, readOnly: !!s, spellcheck: "false",
        title: s ? "저장된 소스의 id 는 바꿀 수 없습니다 (결과 폴더 이름)" : "영문·숫자·_·- (저장한 뒤에는 바꿀 수 없음)",
        oninput: e => { r.id = e.target.value.trim(); tr.dataset.id = r.id; this.renderBar(); },
      }), r.from ? h("div", { class: "from", title: `캐릭터 ${r.from} 의 생성 이미지에서 가져옴` + (r.align ? " (얼굴 위치 맞춤)" : " (원래 구도)") },
        `← ${r.from}` + (r.align ? "" : " · 원래 구도")) : null),
      h("td", {}, h("input", {
        class: "seed", type: "number", min: 0, step: 1, value: r.seed ?? "",
        title: r.from ? "레이어 분리에 쓰는 seed" : "",
        oninput: e => { r.seed = e.target.value === "" ? null : Number(e.target.value); this.renderBar(); },
      })),
      h("td", { class: "tags-cell" }, h("input", {
        class: "tags", value: r.tags, spellcheck: "false", placeholder: "1girl, long hair, black hair, red eyes, serafuku",
        readOnly: !!r.from,
        title: r.from ? "가져온 소스는 그림을 새로 그리지 않으므로 태그는 표시용입니다 (캐릭터 표에서 가져옴)" : "",
        oninput: e => { r.tags = e.target.value; this.renderBar(); },
      })),
      h("td", { class: "st" }, this.statusEl(r.id, jobs)));
    // 편집 칸·체크박스를 누를 때는 미리보기를 바꾸지 않음 (읽기 전용 id 칸은 바꿈)
    tr.addEventListener("click", e => { if (!e.target.closest("input:not([readonly])")) this.setCurrent(r.id); });
    return tr;
  },
  jobMap() {
    // 소스별 대기·진행 중인 파츠 작업 (진행 중 우선)
    const m = new Map();
    for (const j of S.jobs.values()) {
      if (!isPartsJob(j) || (j.status !== "running" && j.status !== "pending")) continue;
      const prev = m.get(j.cid);
      if (!prev || j.status === "running") m.set(j.cid, j);
    }
    return m;
  },
  statusEl(id, jobs) {
    const s = this.saved(id);
    const out = [];
    if (!s) out.push(h("span", { class: "muted small" }, "저장 안 됨"));
    else if (s.layers != null) out.push(h("span", { class: "badge done", title: s.groups.join(", ") }, `레이어 ${s.layers}`));
    else if (s.source) out.push(h("span", { class: "badge base" }, "소스만"));
    else out.push(h("span", { class: "badge none" }, "소스 없음"));
    if (s && s.issues && s.issues.length) out.push(" ", h("span", { class: "warnline", title: s.issues.join("\n") }, "⚠"));
    const j = jobs.get(id);
    if (j) out.push(" ", h("span", { class: "small " + (j.status === "running" ? "job-run" : "muted") },
      (j.status === "running" ? "▶ " : "대기 · ") + MODE_LABEL[j.mode].replace("파츠 ", "")));
    return out;
  },
  renderStatus() {
    if (!this.data) return;
    const jobs = this.jobMap();
    for (const tr of $$("#parts-table tbody tr")) fill($("td.st", tr), this.statusEl(tr.dataset.id, jobs));
  },
  renderBar() {
    const d = this.dirty();
    const lab = $("#parts-dirty");
    lab.textContent = d ? "저장하지 않은 변경이 있습니다" : "변경 사항 없음";
    lab.className = d ? "warnline" : "muted";
    $("#btn-parts-save").disabled = !d;
    $("#btn-parts-revert").disabled = !d;
    const n = this.draft.filter(r => this.selected.has(r.id)).length;
    $("#parts-selection").textContent = n ? `${n}개 선택` : "";
    $("#parts-all").checked = this.draft.length > 0 && n === this.draft.length;
  },
  showErrors(errs, title = "저장하지 못했습니다") {
    const b = $("#parts-errors");
    b.hidden = !errs || !errs.length;
    if (!b.hidden) fill(b, h("b", {}, title), h("ul", {}, errs.map(x => h("li", {}, x))));
  },

  // ---- 편집 ---- //
  add() {
    // 방금 삭제했지만 아직 저장 전인 id 도 피함 (그 id 를 다시 쓰면 지운 소스의 그림이 새 행에 붙어 보임)
    const used = new Set([...this.draft, ...(this.data ? this.data.sources : [])].map(r => r.id));
    let n = this.draft.length + 1, id;
    do { id = "s" + String(n++).padStart(2, "0"); } while (used.has(id));
    this.draft.push({ id, seed: Math.floor(Math.random() * 2 ** 31), tags: "" });
    this.current = id;
    this.render();
    const inp = $$("#parts-table input.tags").at(-1);
    if (inp) inp.focus();
  },
  remove() {
    const ids = new Set(this.draft.filter(r => this.selected.has(r.id)).map(r => r.id));
    if (!ids.size) return toast("삭제할 소스를 체크하세요", "warn");
    this.draft = this.draft.filter(r => !ids.has(r.id));
    ids.forEach(id => this.selected.delete(id));
    if (ids.has(this.current)) this.current = null;
    this.render();
    toast(`${ids.size}개를 목록에서 뺐습니다. [저장]하면 결과 폴더는 parts_out/.deleted 로 옮겨집니다`, "info", 5000);
  },
  async save() {
    try {
      this.data = await api("PUT", "/api/parts/sources", { sources: this.draft });
    } catch (e) {
      this.showErrors(e.errors || [e.message]);
      return;
    }
    this.showErrors(null);
    this.resetDraft();
    this.render();
    toast("parts.json 에 저장했습니다", "ok");
  },
  revert() { this.resetDraft(); this.showErrors(null); this.render(); },

  // ---- 생성 이미지에서 가져오기 ---- //
  imp: { sel: new Set(), q: "" },
  async openImport() {
    if (this.dirty()) return toast("저장하지 않은 변경이 있습니다. 먼저 [저장]하세요.", "warn", 5000);
    await loadPortraits();
    this.imp.sel.clear();
    $("#pi-q").value = this.imp.q = "";
    $("#parts-import-dialog").hidden = false;
    this.renderImport();
    $("#pi-q").focus();
  },
  closeImport() { $("#parts-import-dialog").hidden = true; },
  importable() {
    const q = this.imp.q.toLowerCase();
    return S.portraitOrder.map(id => S.portraits.get(id))
      .filter(p => p && typeOf(p) !== "monster" && p.status !== "none" && (!q || p.id.toLowerCase().includes(q) || (p.name || "").toLowerCase().includes(q)));
  },
  renderImport() {
    const list = this.importable();
    const from = new Set((this.data ? this.data.sources : []).filter(s => s.from).map(s => s.from));
    fill($("#pi-list"), list.length ? list.map(p => {
      const on = this.imp.sel.has(p.id);
      const fig = h("figure", { class: "pi-item" + (on ? " on" : ""), title: `${p.id} ${p.name || ""}`.trim() },
        h("input", { type: "checkbox", checked: on, tabIndex: -1 }),
        from.has(p.id) ? h("span", { class: "tag" }, "가져옴") : null,
        p.thumb ? h("img", { src: p.thumb, alt: "", loading: "lazy" }) : h("div", { class: "ph" }),
        h("figcaption", {}, p.name || p.id, p.name ? h("div", { class: "muted" }, p.id) : null));
      fig.addEventListener("click", () => {
        const now = !this.imp.sel.has(p.id);
        now ? this.imp.sel.add(p.id) : this.imp.sel.delete(p.id);
        fig.classList.toggle("on", now);
        $("input", fig).checked = now;
        this.renderImportBar(list);
      });
      return fig;
    }) : h("div", { class: "empty small" }, this.imp.q ? "검색 결과가 없습니다" : "base 를 생성한 캐릭터가 없습니다. 먼저 [생성 큐]에서 base 를 만드세요."));
    this.renderImportBar(list);
  },
  renderImportBar(list) {
    const n = this.imp.sel.size;
    $("#pi-count").textContent = `${list.length}명 중 ${list.filter(p => this.imp.sel.has(p.id)).length}명 선택` +
      (n > list.filter(p => this.imp.sel.has(p.id)).length ? ` (검색 밖 포함 ${n}명)` : "");
    $("#pi-all").checked = list.length > 0 && list.every(p => this.imp.sel.has(p.id));
    $("#pi-run").disabled = !n;
    $("#pi-run").textContent = n ? `${n}명 가져오기` : "가져오기";
  },
  async runImport() {
    const ids = S.portraitOrder.filter(id => this.imp.sel.has(id));
    if (!ids.length) return;
    const split = $("#pi-split").checked;
    $("#pi-run").disabled = true;
    try {
      const res = await api("POST", "/api/parts/import", { ids, align: $("#pi-align").checked, split });
      this.closeImport();
      await this.load();
      this.selected = new Set(res.sources);
      this.setCurrent(res.sources[0]);
      this.render();
      toast(`${ids.length}명을 소스로 등록하고 가져오기${split ? "·레이어 분리" : ""} 작업을 큐에 넣었습니다` +
        (split ? "" : ". 다 가져오면 [② 레이어 분리]를 누르세요"), "ok", 6000);
    } catch (e) {
      toast(e.message, "error", 6000);
      $("#pi-run").disabled = false;
    }
  },

  async submit(mode) {
    if (this.dirty()) return toast("저장하지 않은 변경이 있습니다. 먼저 [저장]하세요.", "warn", 5000);
    const ids = this.draft.map(r => r.id).filter(id => this.selected.has(id));
    if (!ids.length) return toast("소스를 먼저 체크하세요", "warn");
    try {
      const res = await api("POST", "/api/parts/jobs", { ids, mode, force: $("#parts-force").checked });
      let msg = `${mode === "gen" ? "소스 생성" : "레이어 분리"} ${res.added.length}건을 큐에 추가했습니다`;
      if (res.duplicates) msg += ` (이미 대기 중인 ${res.duplicates}건 제외)`;
      toast(msg, res.added.length ? "ok" : "warn");
    } catch (e) {
      toast(e.message, "error", 6000);
    }
  },

  // ---- 소스 미리보기 ---- //
  setCurrent(id) {
    this.current = id;
    $$("#parts-table tbody tr").forEach(tr => tr.classList.toggle("current", tr.dataset.id === id));
    this.renderPreview();
  },
  renderPreview() {
    // 표에서 뺀(저장 전) 소스는 보여 주지 않음
    const inDraft = this.current && this.draft.some(r => r.id === this.current);
    const s = inDraft ? this.saved(this.current) : null;
    $("#pv-id").textContent = s ? s.id : "";
    $("#pv-id").title = s ? s.tags : "";
    $$("#pv-view button").forEach(b => b.classList.toggle("active", b.dataset.v === this.view));
    const body = $("#pv-body");
    $("#pv-psd").hidden = !(s && s.layers);
    if (!s) return fill(body, h("div", { class: "empty small" },
      inDraft ? "저장한 뒤 [① 소스 생성]을 하면 여기에 보입니다" : "표에서 소스를 누르면 여기에 보입니다"));
    const url = s.files[this.view];
    const msg = this.view === "source" ? "아직 소스를 만들지 않았습니다" : "레이어를 분리하면 보입니다";
    fill(body, url
      ? h("a", { href: url, target: "_blank", title: "새 탭에서 원본 크기로 보기" }, h("img", { src: url, alt: "" }))
      : h("div", { class: "empty small" }, msg));
  },

  async downloadPsd(btn) {
    const id = this.current, kind = btn.dataset.kind;
    if (!id) return;
    btn.disabled = true;
    try {
      const res = await fetch(`/api/parts/psd/${encodeURIComponent(id)}?kind=${kind}`);
      if (!res.ok) {
        let msg = `오류 ${res.status}`;
        try { const d = await res.json(); msg = d.message || d.detail || msg; } catch { /* 본문 없음 */ }
        throw new Error(msg);
      }
      const url = URL.createObjectURL(await res.blob());
      const a = h("a", { href: url, download: kind === "depth" ? `${id}_depth.psd` : `${id}.psd` });
      document.body.append(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (e) {
      toast(`PSD 를 만들지 못했습니다: ${e.message}`, "error", 6000);
    } finally {
      btn.disabled = false;
    }
  },

  // ---- 조합 미리보기 ---- //
  renderPicks() {
    const groups = this.data ? this.data.groups : [];
    const layered = this.layered();
    for (const g of groups) {
      const cur = this.picks[g.key];
      const ok = cur === "" || (cur && layered.some(s => s.id === cur && s.groups.includes(g.key)));
      if (!ok) this.picks[g.key] = g.key === "glasses" ? "" : ((layered.find(s => s.groups.includes(g.key)) || {}).id || "");
    }
    fill($("#compose-picks"), groups.map(g => h("label", {}, g.label,
      h("select", { onchange: e => { this.picks[g.key] = e.target.value; this.renderCompose(); } },
        h("option", { value: "" }, "(없음)"),
        layered.filter(s => s.groups.includes(g.key)).map(s =>
          h("option", { value: s.id, selected: this.picks[g.key] === s.id, title: s.tags }, s.id))))));
    $("#btn-compose-random").disabled = $("#btn-compose-grid").disabled = layered.length === 0;
    this.renderCompose();
  },
  composeUrl(p, w) {
    const q = new URLSearchParams(Object.entries(p).filter(([, v]) => v));
    q.set("w", w);
    q.set("t", Date.now());
    return "/api/parts/compose?" + q;
  },
  renderCompose() {
    const out = $("#compose-out");
    if (!this.layered().length) return fill(out, h("div", { class: "empty small" }, "레이어를 분리한 소스가 있으면 여기서 섞어 볼 수 있습니다"));
    if (!Object.values(this.picks).some(Boolean)) return fill(out, h("div", { class: "empty small" }, "파츠를 고르세요"));
    fill(out, h("img", {
      src: this.composeUrl(this.picks, 520), alt: "조합 결과",
      onerror: () => fill(out, h("div", { class: "empty small" }, "조합 이미지를 만들지 못했습니다")),
    }));
  },
  randomPicks() {
    const layered = this.layered();
    const p = {};
    for (const g of this.data.groups) {
      const c = layered.filter(s => s.groups.includes(g.key));
      // 안경은 가끔만
      p[g.key] = !c.length || (g.key === "glasses" && Math.random() > 0.3) ? "" : c[Math.floor(Math.random() * c.length)].id;
    }
    return p;
  },
  random() { this.picks = this.randomPicks(); this.renderPicks(); },
  grid() {
    const box = $("#compose-grid");
    box.hidden = false;
    fill(box, Array.from({ length: 12 }, () => {
      const p = this.randomPicks();
      const used = this.data.groups.filter(g => p[g.key]);
      return h("figure", { title: used.map(g => `${g.label} ${p[g.key]}`).join("\n") + "\n\n누르면 위에서 크게 봅니다",
                           onclick: () => { this.picks = p; this.renderPicks(); $("#compose-out").scrollIntoView({ block: "nearest" }); } },
        h("div", { class: "cg bg-check" }, h("img", { src: this.composeUrl(p, 220), alt: "", loading: "lazy" })),
        h("figcaption", {}, used.map(g => p[g.key]).join(" ")));
    }));
  },

  setup() {
    $("#btn-parts-add").addEventListener("click", () => this.add());
    $("#btn-parts-import").addEventListener("click", () => this.openImport());
    $$("#parts-import-dialog [data-close]").forEach(el => el.addEventListener("click", () => this.closeImport()));
    $("#pi-q").addEventListener("input", debounce(e => { this.imp.q = e.target.value.trim(); this.renderImport(); }, 150));
    $("#pi-all").addEventListener("change", e => {
      for (const p of this.importable()) e.target.checked ? this.imp.sel.add(p.id) : this.imp.sel.delete(p.id);
      this.renderImport();
    });
    $("#pi-run").addEventListener("click", () => this.runImport());
    document.addEventListener("keydown", e => { if (e.key === "Escape" && !$("#parts-import-dialog").hidden) this.closeImport(); });
    $("#btn-parts-del").addEventListener("click", () => this.remove());
    $("#btn-parts-save").addEventListener("click", () => this.save());
    $("#btn-parts-revert").addEventListener("click", () => this.revert());
    $("#btn-parts-gen").addEventListener("click", () => this.submit("gen"));
    $("#btn-parts-split").addEventListener("click", () => this.submit("split"));
    $("#parts-all").addEventListener("change", e => {
      for (const r of this.draft) e.target.checked ? this.selected.add(r.id) : this.selected.delete(r.id);
      this.render();
    });
    $$("#pv-view button").forEach(b => b.addEventListener("click", () => {
      this.view = b.dataset.v;
      store.set("parts.view", this.view);
      this.renderPreview();
    }));
    $$("#pv-psd button").forEach(b => b.addEventListener("click", () => this.downloadPsd(b)));
    $("#btn-compose-random").addEventListener("click", () => this.random());
    $("#btn-compose-grid").addEventListener("click", () => this.grid());
  },
};

// --------------------------------------------------------------------------- //
// 5. 설정
// --------------------------------------------------------------------------- //
const getPath = (o, p) => p.split(".").reduce((a, k) => (a == null ? a : a[k]), o);
function setPath(o, p, v) {
  const ks = p.split(".");
  let cur = o;
  for (const k of ks.slice(0, -1)) cur = cur[k] = cur[k] ?? {};
  cur[ks.at(-1)] = v;
}

// 생성 설정 폼 정의
const CONFIG_SECTIONS = [
  { id: "s-comfy", title: "ComfyUI 연결", fields: [
    { key: "comfy_url", label: "ComfyUI 주소", type: "url",
      help: "ComfyUI Desktop 기본 포트는 8000, portable 버전은 8188 입니다." },
  ]},
  { id: "s-model", title: "모델", fields: [
    { key: "checkpoint", label: "체크포인트", type: "model", list: "checkpoints",
      help: "Illustrious 계열 SDXL (예: WAI-illustrious-SDXL)" },
    { key: "vae", label: "VAE", type: "model", list: "vaes", empty: "체크포인트 내장 VAE",
      help: "표정·여백 인페인트 부분이 뿌옇거나 회색 막처럼 보이면 체크포인트 VAE 가 망가진 것입니다. sdxl_vae_fp16_fix 같은 표준 SDXL VAE 를 고르세요." },
    { key: "loras", label: "LoRA", type: "loras", help: "그림체 통일용 스타일 LoRA. 강도 0.5~0.8 권장." },
    { key: "clip_skip", label: "CLIP skip", type: "int", min: 1, max: 12, help: "Illustrious 계열은 2" },
  ]},
  { id: "s-sample", title: "샘플링 (base)", fields: [
    { key: "width", label: "가로", type: "int", min: 256, max: 4096, step: 8 },
    { key: "height", label: "세로", type: "int", min: 256, max: 4096, step: 8, help: "SDXL 상반신은 832×1216 권장. 바꾸면 Unity 비율도 달라집니다." },
    { key: "steps", label: "스텝", type: "int", min: 1, max: 150 },
    { key: "cfg", label: "CFG", type: "float", min: 0, max: 30, step: 0.5 },
    { key: "sampler", label: "샘플러", type: "model", list: "samplers" },
    { key: "scheduler", label: "스케줄러", type: "model", list: "schedulers" },
  ]},
  { id: "s-prompt", title: "프롬프트", fields: [
    { key: "quality_prefix", label: "품질 태그 (앞)", type: "textarea" },
    { key: "framing", label: "구도 (base)", type: "textarea" },
    { key: "closeup_framing", label: "구도 (표정 인페인트)", type: "textarea" },
    { key: "background", label: "배경", type: "textarea" },
    { key: "style_suffix", label: "스타일 태그 (뒤)", type: "textarea", help: "웹툰 느낌 등 전체에 붙일 태그" },
    { key: "negative", label: "네거티브", type: "textarea", rows: 3 },
  ]},
  ...Object.entries(IMAGE_TYPES).map(([kind, label]) => ({
    id: `s-type-prompt-${kind}`, title: `${label} 기본 프롬프트`, fields: [
      { key: `type_prompts.${kind}.positive`, label: "기본 프롬프트", type: "textarea", rows: 3,
        help: "공통 품질 태그와 개별 이미지 설명에 함께 적용합니다. AI 자동 추가의 설명 캐시와는 별도 설정입니다." },
      { key: `type_prompts.${kind}.negative`, label: "유형별 네거티브", type: "textarea", rows: 3,
        help: kind === "monster" ? "미소녀·사람형으로 나오는 결과를 억제하는 기본값입니다. 사람형 몬스터가 필요하면 humanoid 등 해당 태그를 삭제하세요. 모델에 따라 효과가 다릅니다."
          : "공통 네거티브에 추가할 태그입니다. 비워 두면 공통 설정만 적용합니다." },
    ],
  })),
  { id: "s-expr", title: "표정 인페인트", fields: [
    { key: "expression.denoise", label: "denoise (기본)", type: "range", min: 0.2, max: 0.9, step: 0.01,
      help: "높이면 표정 변화가 커지고, 너무 높으면 얼굴형·눈 색이 바뀝니다. 0.45~0.65 권장. 표정별로 따로 줄 수도 있습니다." },
    { key: "expression.steps", label: "스텝", type: "int", min: 1, max: 150 },
    { key: "expression.inpaint_size", label: "인페인트 해상도", type: "int", min: 256, max: 2048, step: 8, help: "얼굴 크롭을 이 크기로 확대해 그립니다" },
    { key: "expression.crop_scale", label: "크롭 배율", type: "float", min: 1, max: 6, step: 0.1, help: "낮추면 얼굴이 더 크게 확대되어 디테일이 좋아집니다 (2.2 → 1.8)" },
    { key: "expression.mask_expand", label: "마스크 확장", type: "float", min: 0.5, max: 3, step: 0.05, help: "앞머리·귀까지 바뀌면 1.1 정도로 낮추세요" },
    { key: "expression.feather", label: "경계 페더", type: "float", min: 0, max: 0.5, step: 0.01, help: "오버레이 경계가 보이면 0.1 정도로 올리세요" },
  ]},
  { id: "s-headroom", title: "머리 위 여백", fields: [
    { key: "headroom.enabled", label: "자동 아웃페인트", type: "bool",
      help: "base 에서 머리가 위쪽 가장자리에 닿으면 그림을 아래로 내리고 위쪽을 이어 그려 정수리 위 여백을 만듭니다" },
    { key: "headroom.min_margin", label: "최소 여백", type: "float", min: 0.005, max: 0.2, step: 0.005,
      help: "세로 길이 대비. 위쪽 이만큼에 인물이 걸치면 아웃페인트합니다 (0.03 = 1216px 기준 36px)" },
    { key: "headroom.pad", label: "한 번에 늘릴 양", type: "float", min: 0.02, max: 0.4, step: 0.01,
      help: "세로 길이 대비. 그만큼 아래쪽(가슴 아래)이 잘립니다" },
    { key: "headroom.max_passes", label: "최대 반복", type: "int", min: 1, max: 5,
      help: "그래도 닿아 있으면 경고만 남깁니다 → base 다시 뽑기" },
    { key: "headroom.denoise", label: "denoise", type: "range", min: 0.3, max: 1, step: 0.05, help: "보통 1.0" },
  ]},
  { id: "s-controlnet", title: "자세 고정 (ControlNet)", fields: [
    { key: "type_poses.character", label: "주인공/조연 기본 포즈", type: "model", list: "poses", help: "개별 항목의 포즈를 지정하면 우선 적용합니다." },
    { key: "type_poses.npc", label: "NPC 기본 포즈", type: "model", list: "poses", help: "몬스터에는 포즈 고정을 적용하지 않습니다." },
    { key: "controlnet.model", label: "ControlNet 모델", type: "model", list: "controlnets",
      help: "SDXL 용이어야 합니다 (예: xinsir controlnet-union-sdxl-1.0 promax). SD1.5 모델(control_v11…)은 쓸 수 없습니다." },
    { key: "controlnet.union_type", label: "union 종류", type: "select", options: [
      ["openpose", "openpose (권장)"], ["", "union 모델 아님"], ["auto", "auto"], ["depth", "depth"],
      ["canny/lineart/anime_lineart/mlsd", "lineart / canny"]],
      help: "union(promax) 모델에서 쓸 종류. openpose 는 몸 윤곽을 고정하지 않아 머리 모양·옷이 다양하게 나옵니다." },
    { key: "controlnet.pose", label: "기본 포즈", type: "model", list: "poses",
      help: "포즈 폴더의 PNG. 마음에 드는 base 로 만듭니다: python pipeline.py --make-pose <캐릭터 id>. 캐릭터 표 [포즈] 칸으로 캐릭터마다 바꿀 수 있습니다." },
    { key: "controlnet.pose_dir", label: "포즈 폴더", type: "text", help: "상대 경로는 프로젝트 폴더 기준입니다" },
    { key: "controlnet.strength", label: "강도", type: "range", min: 0, max: 1.5, step: 0.05,
      help: "0.8 권장. 0.6 이면 전신은 상반신으로 잡히지만 클로즈업은 그대로인 경우가 많습니다. 1.0 으로 올려도 거의 차이 없음" },
    { key: "controlnet.start", label: "시작", type: "range", min: 0, max: 1, step: 0.05, help: "보통 0" },
    { key: "controlnet.end", label: "끝", type: "range", min: 0, max: 1, step: 0.05,
      help: "전체 스텝 중 이 비율까지만 적용 (0.6 권장). 구도만 잡고 세부는 자유롭게" },
  ]},
  { id: "s-detect", title: "얼굴 검출 · 배경 제거", fields: [
    { key: "face_detector.repo", label: "검출 모델 저장소", type: "text" },
    { key: "face_detector.file", label: "검출 모델 파일", type: "text" },
    { key: "face_detector.conf", label: "검출 신뢰도", type: "float", min: 0.01, max: 1, step: 0.05 },
    { key: "remove_background", label: "배경 제거", type: "bool", help: "rembg 로 배경을 투명하게 만듭니다 (base.png)" },
    { key: "rembg_model", label: "rembg 모델", type: "text" },
  ]},
  { id: "s-output", title: "출력", fields: [
    { key: "output_dir", label: "출력 폴더", type: "text", help: "상대 경로는 프로젝트 폴더 기준입니다" },
    { key: "unity_export_dir", label: "Unity 내보내기 폴더", type: "text",
      help: "검수 탭의 [Unity로 내보내기] 대상. 예: D:\\MyGame\\Assets\\Resources\\Portraits (NpcPortrait.cs 는 Resources/Portraits 에서 읽습니다)" },
  ]},
];

const Settings = {
  testResult: null,
  cfg: null, cfgOrig: "", ex: null, exOrig: "", models: null, loaded: false,

  dirtyCfg() { return this.cfg && JSON.stringify(this.cfg) !== this.cfgOrig; },
  dirtyEx() { return this.ex && JSON.stringify(this.ex) !== this.exOrig; },

  async open() {
    if (!this.loaded) await this.load();
    else this.updateBar();
  },

  async load() {
    try {
      const [cfg, ex] = await Promise.all([api("GET", "/api/config"), api("GET", "/api/expressions")]);
      this.cfg = cfg; this.cfgOrig = JSON.stringify(cfg);
      this.ex = ex; this.exOrig = JSON.stringify(ex);
      this.loaded = true;
    } catch (e) {
      toast("설정을 읽지 못했습니다: " + e.message, "error");
      return;
    }
    this.renderNav();
    this.renderConfig();
    this.renderExpr();
    this.updateBar();
    this.loadModels();
  },

  async loadModels(url) {
    const q = url ? `?url=${encodeURIComponent(url)}` : "";
    try {
      this.models = await api("GET", "/api/comfy/models" + q);
    } catch (e) {
      this.models = { ok: false, error: e.message };
    }
    this.renderConfig();
  },

  updateBar() {
    const c = this.dirtyCfg(), x = this.dirtyEx();
    const parts = [c && "생성 설정", x && "표정 설정"].filter(Boolean);
    const el = $("#settings-dirty");
    el.textContent = parts.length ? `저장하지 않은 변경: ${parts.join(", ")}` : "변경 사항 없음";
    el.classList.toggle("dirty", parts.length > 0);
    $("#btn-save-config").disabled = !c;
    $("#btn-save-expr").disabled = !x;
    $("#btn-settings-revert").disabled = !(c || x);
  },

  changed() { this.updateBar(); },

  renderNav() {
    $("#settings-nav").replaceChildren(
      h("div", { class: "grp" }, "생성 설정 (config.json)"),
      ...CONFIG_SECTIONS.map(s => h("a", { href: "#" + s.id, onclick: ev => { ev.preventDefault(); $("#" + s.id).scrollIntoView({ behavior: "smooth" }); } }, s.title)),
      h("div", { class: "grp" }, "표정 (expressions.json)"),
      ...[["x-tags", "표정 태그"], ["x-pers", "성격 프리셋"]].map(([id, t]) =>
        h("a", { href: "#" + id, onclick: ev => { ev.preventDefault(); $("#" + id).scrollIntoView({ behavior: "smooth" }); } }, t)));
  },

  // ---- config.json ---- //
  renderConfig() {
    if (!this.cfg) return;
    const scroll = $("main").scrollTop;
    const form = $("#config-form");
    form.replaceChildren(h("h2", {}, "생성 설정 ", h("span", { class: "muted small" }, "config.json")),
      ...CONFIG_SECTIONS.map(sec => h("section", { class: "panel", id: sec.id },
        h("h3", {}, sec.title), ...sec.fields.map(f => this.field(f)))));
    $("main").scrollTop = scroll;
  },

  field(f) {
    const val = getPath(this.cfg, f.key);
    const set = v => { setPath(this.cfg, f.key, v); this.changed(); };
    const ctl = h("div", { class: "ctl" });
    const extra = [];
    const num = (v, int) => { const n = int ? parseInt(v, 10) : parseFloat(v); return isNaN(n) ? v : n; };

    switch (f.type) {
      case "textarea":
        ctl.append(h("textarea", { rows: f.rows || 2, value: val ?? "", oninput: e => set(e.target.value) }));
        break;
      case "int": case "float":
        ctl.append(h("input", { type: "number", min: f.min, max: f.max, step: f.step || (f.type === "int" ? 1 : "any"), value: val ?? "",
          oninput: e => set(num(e.target.value, f.type === "int")) }));
        break;
      case "range": {
        const nb = h("input", { type: "number", min: f.min, max: f.max, step: f.step, value: val ?? "" });
        const rg = h("input", { type: "range", min: f.min, max: f.max, step: f.step, value: val ?? f.min });
        rg.addEventListener("input", () => { nb.value = rg.value; set(num(rg.value)); });
        nb.addEventListener("input", () => { rg.value = nb.value; set(num(nb.value)); });
        ctl.append(rg, nb);
        break;
      }
      case "select": {
        const opts = f.options.map(([v, label]) => h("option", { value: v }, label));
        if (val != null && !f.options.some(([v]) => v === val)) opts.unshift(h("option", { value: val }, val));
        const sel = h("select", { onchange: e => set(e.target.value) }, ...opts);
        sel.value = val ?? f.options[0][0];
        ctl.append(sel);
        break;
      }
      case "bool":
        ctl.append(h("label", { class: "check" }, h("input", { type: "checkbox", checked: !!val, onchange: e => set(e.target.checked) }), "사용"));
        break;
      case "url": {
        const inp = h("input", { type: "text", value: val ?? "", oninput: e => set(e.target.value.trim()) });
        // 결과는 this.testResult 에 보관 (모델 목록을 다시 받으면 폼이 새로 그려지므로)
        const res = h("span", { class: "test-result " + (this.testResult ? this.testResult.cls : "") }, this.testResult ? this.testResult.text : "");
        const show = (cls, text) => { this.testResult = { cls, text }; res.className = "test-result " + cls; res.textContent = text; };
        const btn = h("button", { class: "btn", type: "button" }, "ComfyUI 연결 테스트");
        btn.addEventListener("click", async () => {
          btn.disabled = true;
          show("", "확인 중…");
          try {
            const r = await api("POST", "/api/comfy/test", { url: inp.value.trim() });
            show(r.ok ? "ok" : "bad", r.ok
              ? `✓ 연결됨 (${r.latency_ms}ms)` + (r.version ? ` · ComfyUI ${r.version}` : "") + (r.device ? ` · ${r.device}` : "") + (r.vram_gb ? ` ${r.vram_gb}GB` : "")
              : `✗ 연결 실패: ${r.error}`);
            if (r.ok) this.loadModels(inp.value.trim());
          } catch (e) {
            show("bad", "✗ " + e.message);
          }
          btn.disabled = false;
        });
        ctl.append(inp, btn);
        extra.push(h("div", { class: "help" }, res));
        break;
      }
      case "model":
        ctl.append(this.modelSelect(f.list, val, set, f.empty));
        if (f.list === "checkpoints") {
          ctl.append(h("button", { class: "btn small ghost", type: "button", title: "ComfyUI 에서 목록 다시 받기",
            onclick: () => this.loadModels(this.cfg.comfy_url) }, "목록 새로고침"));
        }
        if (this.models && this.models.ok && val && !(this.models[f.list] || []).includes(val)) {
          extra.push(h("div", { class: "warnline" }, f.list === "poses"
            ? `'${val}' 이(가) 포즈 폴더에 없습니다. python pipeline.py --make-pose <캐릭터 id> 로 만드세요.`
            : `'${val}' 은(는) ComfyUI 목록에 없습니다. 파일 이름을 확인하세요.`));
        }
        if (this.models && !this.models.ok && f.list === "checkpoints") {
          extra.push(h("div", { class: "warnline" }, `ComfyUI 에서 목록을 받지 못해 직접 입력합니다 (${this.models.error || "연결 안 됨"})`));
        }
        break;
      case "loras":
        ctl.append(this.loraEditor());
        break;
      default:
        ctl.append(h("input", { type: "text", value: val ?? "", oninput: e => set(e.target.value) }));
    }
    return h("div", { class: "field" },
      h("label", {}, f.label, h("span", { class: "key" }, f.key)),
      ctl,
      f.help ? h("div", { class: "help" }, f.help) : null,
      ...extra);
  },

  /** ComfyUI 목록이 있으면 드롭다운, 없으면 텍스트 입력 */
  modelSelect(listName, val, set, emptyLabel) {
    const list = this.models && this.models.ok ? (this.models[listName] || []) : null;
    if (!list || !list.length) {
      return h("input", { type: "text", value: val ?? "", placeholder: emptyLabel ? `비우면 ${emptyLabel}` : "파일 이름",
        oninput: e => set(e.target.value.trim()) });
    }
    const opts = list.map(n => h("option", { value: n }, n));
    if (val && !list.includes(val)) opts.unshift(h("option", { value: val }, `${val} (목록에 없음)`));
    if (emptyLabel) opts.unshift(h("option", { value: "" }, emptyLabel));
    else if (!val) opts.unshift(h("option", { value: "" }, "선택하세요"));
    const sel = h("select", { onchange: e => { set(e.target.value); this.renderConfig(); } }, ...opts);
    sel.value = val ?? "";
    return sel;
  },

  loraEditor() {
    const loras = this.cfg.loras = this.cfg.loras || [];
    const box = h("div", { style: { width: "100%" } });
    const rerender = () => { this.changed(); this.renderConfig(); };
    loras.forEach((l, i) => {
      const name = this.modelSelect("loras", l.name, v => { l.name = v; this.changed(); });
      const strength = h("input", { type: "number", step: 0.05, min: -5, max: 5, value: l.strength ?? 0.7, title: "모델 강도",
        oninput: e => { l.strength = parseFloat(e.target.value); if (isNaN(l.strength)) l.strength = 0; this.changed(); } });
      const clip = h("input", { type: "number", step: 0.05, min: -5, max: 5, value: l.strength_clip ?? "", placeholder: "같게", title: "CLIP 강도 (비우면 모델 강도와 같게)",
        oninput: e => { const v = parseFloat(e.target.value); if (isNaN(v)) delete l.strength_clip; else l.strength_clip = v; this.changed(); } });
      box.append(h("div", { class: "lora-row" }, name,
        h("span", { class: "lbl" }, "강도"), strength, h("span", { class: "lbl" }, "CLIP"), clip,
        h("button", { class: "icon-btn", type: "button", title: "삭제", onclick: () => { loras.splice(i, 1); rerender(); } }, "✕")));
    });
    box.append(h("button", { class: "btn small", type: "button", onclick: () => {
      const first = this.models && this.models.ok && this.models.loras.length ? this.models.loras[0] : "";
      loras.push({ name: first, strength: 0.7 });
      rerender();
    } }, "+ LoRA 추가"));
    return box;
  },

  // ---- expressions.json ---- //
  usage(name) {
    return Object.values(this.ex.personalities).filter(l => l.includes(name)).length;
  },

  renderExpr() {
    if (!this.ex) return;
    const ex = this.ex;
    const scroll = $("main").scrollTop;
    const rer = () => { this.changed(); this.renderExpr(); };

    // 표정 태그 표
    const tbody = h("tbody");
    for (const [name, v] of Object.entries(ex.expressions)) {
      const tags = typeof v === "string" ? v : v.tags;
      const den = typeof v === "object" && v.denoise != null ? v.denoise : "";
      const setVal = (t, d) => { ex.expressions[name] = d === "" || d == null ? t : { tags: t, denoise: d }; this.changed(); };
      const nameIn = h("input", { type: "text", class: "mono", value: name });
      nameIn.addEventListener("change", () => this.renameExpr(name, nameIn.value.trim(), nameIn));
      const tagIn = h("input", { type: "text", value: tags });
      const denIn = h("input", { type: "number", min: 0.01, max: 1, step: 0.01, value: den, placeholder: `기본 ${this.cfg ? this.cfg.expression.denoise : ""}` });
      tagIn.addEventListener("input", () => setVal(tagIn.value, denIn.value === "" ? "" : parseFloat(denIn.value)));
      denIn.addEventListener("input", () => setVal(tagIn.value, denIn.value === "" ? "" : parseFloat(denIn.value)));
      const used = this.usage(name);
      tbody.append(h("tr", {},
        h("td", { class: "c-name" }, nameIn),
        h("td", {}, tagIn),
        h("td", { class: "c-den" }, denIn),
        h("td", { class: "c-used", title: "이 표정을 쓰는 성격 수" }, used || "–"),
        h("td", { class: "c-del" }, h("button", { class: "icon-btn", title: "표정 삭제", disabled: name === "neutral",
          onclick: () => {
            delete ex.expressions[name];
            let n = 0;
            for (const k of Object.keys(ex.personalities)) {
              const before = ex.personalities[k].length;
              ex.personalities[k] = ex.personalities[k].filter(x => x !== name);
              n += before !== ex.personalities[k].length;
            }
            if (n) toast(`'${name}' 을(를) 성격 ${n}개에서도 뺐습니다. 실수라면 [되돌리기]를 누르세요.`, "warn", 5000);
            rer();
          } }, "✕"))));
    }

    const tagsPanel = h("section", { class: "panel", id: "x-tags" },
      h("h3", {}, "표정 태그"),
      h("div", { class: "field" },
        h("label", {}, "base 표정", h("span", { class: "key" }, "base_expression")),
        h("div", { class: "ctl" }, h("input", { type: "text", value: ex.base_expression, oninput: e => { ex.base_expression = e.target.value; this.changed(); } })),
        h("div", { class: "help" }, "base 이미지를 만들 때 쓰는 표정 태그 (보통 closed mouth)")),
      h("table", { class: "expr-table" },
        h("thead", {}, h("tr", {}, h("th", {}, "이름 (파일명)"), h("th", {}, "태그"), h("th", {}, "denoise"), h("th", {}, "사용"), h("th"))),
        tbody),
      h("div", { style: { marginTop: "8px" } }, h("button", { class: "btn small", type: "button", onclick: () => {
        let i = 1, n = "new_expr";
        while (n in ex.expressions) n = `new_expr${++i}`;
        ex.expressions[n] = "";
        rer();
        setTimeout(() => { const ins = $$("#x-tags tbody input.mono"); ins.at(-1)?.select(); }, 0);
      } }, "+ 표정 추가")),
    );

    // 성격 프리셋
    const exprNames = Object.keys(ex.expressions);
    const persBox = h("div", { class: "pers-grid" });
    for (const [pname, list] of Object.entries(ex.personalities)) {
      const isDefault = ex.default_personality === pname;
      const nameIn = h("input", { type: "text", value: pname });
      nameIn.addEventListener("change", () => this.renamePers(pname, nameIn.value.trim(), nameIn));
      const add = h("select", { title: "표정 추가" },
        h("option", { value: "" }, "+ 표정 추가"),
        ...exprNames.filter(n => !list.includes(n)).map(n => h("option", { value: n }, n)));
      add.addEventListener("change", () => { if (add.value) { list.push(add.value); rer(); } });
      persBox.append(h("div", { class: "pers" },
        h("div", { class: "pers-head" }, nameIn,
          h("label", { class: "check small" }, h("input", { type: "radio", name: "default-pers", checked: isDefault,
            onchange: () => { ex.default_personality = pname; rer(); } }), "기본 성격"),
          h("span", { class: "muted small" }, `${list.length}개`),
          h("span", { class: "spacer" }),
          h("button", { class: "icon-btn", title: isDefault ? "기본 성격은 삭제할 수 없습니다" : "성격 삭제", disabled: isDefault,
            onclick: () => { delete ex.personalities[pname]; rer(); } }, "✕")),
        h("div", { class: "chips" },
          ...list.map((n, i) => h("span", { class: "chip" + (n in ex.expressions ? "" : " missing"), title: n in ex.expressions ? "" : "정의되지 않은 표정" },
            n,
            i > 0 ? h("button", { title: "앞으로", onclick: () => { [list[i - 1], list[i]] = [list[i], list[i - 1]]; rer(); } }, "‹") : null,
            h("button", { title: "빼기", onclick: () => { list.splice(i, 1); rer(); } }, "✕"))),
          add)));
    }
    const persPanel = h("section", { class: "panel", id: "x-pers" },
      h("h3", {}, "성격 프리셋"),
      h("p", { class: "note" }, "성격마다 만들 표정 목록입니다. neutral 이 없으면 자동으로 맨 앞에 들어갑니다. CSV 의 personality 칸에 '명랑|소심' 처럼 섞어 쓸 수 있습니다."),
      persBox,
      h("div", { style: { marginTop: "10px" } }, h("button", { class: "btn small", type: "button", onclick: () => {
        let i = 1, n = "새 성격";
        while (n in ex.personalities) n = `새 성격 ${++i}`;
        ex.personalities[n] = ["neutral"];
        rer();
      } }, "+ 성격 추가")));

    $("#expr-editor").replaceChildren(h("h2", {}, "표정 설정 ", h("span", { class: "muted small" }, "expressions.json")), tagsPanel, persPanel);
    $("main").scrollTop = scroll;
  },

  /** 순서를 유지하며 객체 키 이름 바꾸기 */
  renameKey(obj, from, to) {
    const entries = Object.entries(obj).map(([k, v]) => [k === from ? to : k, v]);
    for (const k of Object.keys(obj)) delete obj[k];
    for (const [k, v] of entries) obj[k] = v;
  },

  renameExpr(from, to, input) {
    const ex = this.ex;
    if (to === from) return;
    if (!/^[\w-]+$/.test(to)) { toast("표정 이름은 영문/숫자/_/- 만 쓸 수 있습니다 (파일 이름이 됩니다)", "warn"); input.value = from; return; }
    if (to in ex.expressions) { toast(`'${to}' 은(는) 이미 있습니다`, "warn"); input.value = from; return; }
    this.renameKey(ex.expressions, from, to);
    for (const l of Object.values(ex.personalities)) l.forEach((n, i) => { if (n === from) l[i] = to; });
    this.changed();
    this.renderExpr();
  },

  renamePers(from, to, input) {
    const ex = this.ex;
    if (to === from) return;
    if (!to || to.includes("|")) { toast("성격 이름은 비울 수 없고 | 를 쓸 수 없습니다", "warn"); input.value = from; return; }
    if (to in ex.personalities) { toast(`'${to}' 은(는) 이미 있습니다`, "warn"); input.value = from; return; }
    this.renameKey(ex.personalities, from, to);
    if (ex.default_personality === from) ex.default_personality = to;
    toast("성격 이름을 바꿨습니다. characters.csv 의 personality 칸도 함께 고쳐야 합니다.", "warn", 5000);
    this.changed();
    this.renderExpr();
  },

  showErrors(title, errs) {
    const box = $("#settings-errors");
    if (!errs || !errs.length) { box.hidden = true; return; }
    box.replaceChildren(h("b", {}, title), h("ul", {}, ...errs.map(e => h("li", {}, e))));
    box.hidden = false;
    box.scrollIntoView({ behavior: "smooth", block: "nearest" });
  },

  async saveConfig() {
    try {
      await api("PUT", "/api/config", this.cfg);
      this.cfgOrig = JSON.stringify(this.cfg);
      this.showErrors();
      toast("config.json 을 저장했습니다. 다음 작업부터 적용됩니다.", "ok");
      pollStatus();
      refreshPortraits();
    } catch (e) {
      this.showErrors(e.message, e.errors || [e.message]);
      toast("저장하지 못했습니다", "error");
    }
    this.updateBar();
    this.renderConfig();
  },

  async saveExpr() {
    try {
      await api("PUT", "/api/expressions", this.ex);
      this.exOrig = JSON.stringify(this.ex);
      S.ex = deepCopy(this.ex);
      this.showErrors();
      toast("expressions.json 을 저장했습니다.", "ok");
    } catch (e) {
      this.showErrors(e.message, e.errors || [e.message]);
      toast("저장하지 못했습니다", "error");
    }
    this.updateBar();
  },

  revert() {
    this.cfg = JSON.parse(this.cfgOrig);
    this.ex = JSON.parse(this.exOrig);
    this.showErrors();
    this.renderConfig();
    this.renderExpr();
    this.updateBar();
  },

  setup() {
    $("#btn-save-config").addEventListener("click", () => this.saveConfig());
    $("#btn-save-expr").addEventListener("click", () => this.saveExpr());
    $("#btn-settings-revert").addEventListener("click", () => this.revert());
    window.addEventListener("beforeunload", e => {
      if (this.dirtyCfg() || this.dirtyEx() || Chars.dirty() || Parts.dirty()) { e.preventDefault(); e.returnValue = ""; }
    });
  },
};

// --------------------------------------------------------------------------- //
// 시작
// --------------------------------------------------------------------------- //
async function init() {
  $$("[data-image-type]").forEach(b => b.addEventListener("click", () => selectImageType(b.dataset.imageType)));
  $$(".tab-btn").forEach(b => b.addEventListener("click", () => showTab(b.dataset.tab)));
  window.addEventListener("hashchange", () => showTab(location.hash.slice(1)));
  Chars.setup();
  setupQueue();
  setupGallery();
  Detail.setup();
  UnityExport.setup();
  Parts.setup();
  Settings.setup();

  await Promise.all([loadRows(), loadPortraits(), loadExpressions(), pollStatus()]);
  selectImageType(S.imageType);
  showTab(location.hash.slice(1) || "characters");
  connectEvents();
  setInterval(pollStatus, 10000);
}

init();
