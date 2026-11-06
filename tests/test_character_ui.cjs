// DOM 모의 환경에서 초안 보존/응답 지연/재시도 동작 검증. node tests/test_character_ui.cjs
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

const elements = new Map();
const storage = new Map();
function element(selector) {
  if (!elements.has(selector)) elements.set(selector, {
    value: '', hidden: true, disabled: false, textContent: '',
    listeners: {}, addEventListener(name, fn) { this.listeners[name] = fn; }, focus() {}, scrollIntoView() {},
  });
  return elements.get(selector);
}
const sandbox = {
  document: { querySelector: element, querySelectorAll: () => [], addEventListener() {} },
  window: {}, setTimeout, clearTimeout, console,
  localStorage: { getItem: key => storage.get(key) ?? null, setItem: (key, value) => storage.set(key, value) },
};
vm.createContext(sandbox);
const source = fs.readFileSync(path.join(__dirname, '../static/app.js'), 'utf8').replace(/init\(\);\s*$/, '');
vm.runInContext(source + '\nglobalThis.ui = { Chars, CharacterAI, S, Gallery }; toast = () => {};', sandbox);
const { Chars, CharacterAI, S, Gallery } = sandbox.ui;
Chars.render = () => {};
Chars.showErrors = () => {};
Chars.columns = ['id', 'name', 'subject', 'personality', 'seed', 'expressions', 'custom'];
S.rows = [{ id: 'npc_0001', name: '기존', custom: '보존' }];
Chars.savedJSON = JSON.stringify(S.rows);
Chars.draft = [{ ...S.rows[0], name: '미저장 편집', _k: 1, _orig: S.rows[0] }];
Chars.nextK = 2;
S.portraits.set('NPC_0002', { status: 'done' });
S.ex = { personalities: { '명랑': [] } };
const generated = { name: '새 캐릭터', subject: '1girl', personality: '명랑' };
Chars.addGenerated([generated]);
assert.equal(Chars.draft[0].name, '미저장 편집');
assert.equal(Chars.draft[0].custom, '보존');
assert.equal(Chars.draft[1].id, 'npc_0003');
assert.equal(Chars.draft[1].custom, '');
assert.equal(Chars.draft[1].seed, '');
assert.equal(Chars.draft[1]._orig, null);
assert.equal(Chars.sel.has(Chars.draft[1]._k), true);
assert.equal(Chars.dirty(), true);
assert.equal(S.rows.length, 1);
Chars.revert();
assert.equal(Chars.draft.length, 1);
assert.equal(Chars.draft[0].name, '기존');

async function run() {
  let calls = 0, resolve;
  sandbox.request = () => { calls++; return new Promise(r => { resolve = r; }); };
  vm.runInContext('api = (...args) => globalThis.request(...args);', sandbox);
  element('#ai-count').value = '1';
  CharacterAI.open();
  element('#ai-description').value = '마을 주민';
  const pending = CharacterAI.run();
  assert.equal(element('#ai-run').disabled, true);
  await CharacterAI.run();
  assert.equal(calls, 1);
  CharacterAI.close();
  CharacterAI.open(); // 다시 열어도 닫기 전 요청 결과를 추가하지 않는다.
  resolve({ rows: [generated] });
  await pending;
  assert.equal(Chars.draft.length, 1);
  assert.equal(element('#ai-run').disabled, false);

  sandbox.request = async () => { throw new Error('모의 오류'); };
  await CharacterAI.run();
  assert.equal(element('#ai-error').hidden, false);
  assert.equal(element('#ai-error').textContent, '모의 오류');
  assert.equal(Chars.draft.length, 1);
  sandbox.request = async () => ({ rows: [generated] });
  await CharacterAI.run();
  assert.equal(Chars.draft.length, 2);
  assert.equal(element('#ai-dialog').hidden, true);
  assert.equal(element('#ai-run').disabled, false);
  S.imageType = 'monster';
  CharacterAI.open();
  assert.equal(element('#ai-description').value, '');
  element('#ai-description').value = '슬라임 몬스터';
  sandbox.request = async (_method, _url, body) => {
    assert.equal(body.image_type, 'monster');
    return { rows: [{ name: '슬라임', prompt: 'green slime', image_type: 'monster' }] };
  };
  await CharacterAI.run();
  assert.equal(Chars.draft.at(-1).image_type, 'monster');
  assert.equal(Chars.draft.at(-1).prompt, 'green slime');
  assert.equal(Chars.filtered().length, 1);
  assert.equal(Chars.allCols().some(c => c.key === 'prompt'), true);
  assert.equal(Chars.allCols().some(c => ['personality', 'expressions', 'pose', 'subject'].includes(c.key)), false);
  S.imageType = 'npc';
  assert.equal(Chars.allCols().some(c => c.key === 'pose'), true);
  assert.equal(Chars.allCols().some(c => ['expressions', 'personality', 'prompt'].includes(c.key)), false);
  S.imageType = 'character';
  assert.equal(Chars.allCols().some(c => c.key === 'expressions'), true);
  CharacterAI.open();
  assert.equal(element('#ai-description').value, '마을 주민');
  CharacterAI.close();
  S.imageType = 'npc';
  CharacterAI.open();
  assert.equal(element('#ai-description').value, '');
  element('#ai-description').value = '상점 NPC';
  element('#ai-description').listeners.input();
  assert.equal(JSON.parse(storage.get('npcui.ai.prompt.npc')), '상점 NPC');
  CharacterAI.close();
  S.imageType = 'monster';
  CharacterAI.open();
  assert.equal(element('#ai-description').value, '슬라임 몬스터');
  element('#ai-description').value = '';
  element('#ai-description').listeners.input();
  CharacterAI.close();
  CharacterAI.open();
  assert.equal(element('#ai-description').value, '');
  CharacterAI.close();
  const refreshed = { ...sandbox, window: {} };
  vm.createContext(refreshed);
  vm.runInContext(source + '\nglobalThis.ui = { CharacterAI, S };', refreshed);
  refreshed.ui.S.imageType = 'npc';
  refreshed.ui.CharacterAI.open();
  assert.equal(element('#ai-description').value, '상점 NPC');
  vm.runInContext('renderGallery = () => Gallery.updateSelection(); loadPortraits = async () => {}; loadJobs = async () => {}; Chars.load = async () => {};', sandbox);
  Chars.dirty = () => false;
  S.tab = 'gallery';
  S.imageType = 'character';
  S.portraits = new Map([
    ['a', { id: 'a', name: 'A', image_type: 'character' }],
    ['b', { id: 'b', name: 'B', image_type: 'character' }],
    ['c', { id: 'c', name: 'C', image_type: 'monster' }],
  ]);
  S.portraitOrder = ['a', 'b', 'c'];
  Gallery.selectVisible(true);
  assert.deepEqual([...Gallery.selected], ['a', 'b']);
  Gallery.q = 'a';
  Gallery.selectVisible(false);
  assert.deepEqual([...Gallery.selected], ['b']);
  Gallery.q = '';
  Gallery.selectVisible(true);
  Gallery.openDelete();
  assert.equal(element('#gallery-delete-dialog').hidden, false);
  let deleteResolve, deleteCalls = 0;
  sandbox.request = async (method, url, body) => {
    deleteCalls++;
    assert.equal(method, 'POST');
    assert.equal(url, '/api/portraits/delete');
    assert.deepEqual([...body.ids].sort(), ['a', 'b']);
    return new Promise(resolve => { deleteResolve = resolve; });
  };
  const deleting = Gallery.deleteConfirmed();
  await Gallery.deleteConfirmed();
  assert.equal(deleteCalls, 1);
  assert.equal(element('#gallery-delete-confirm').disabled, true);
  deleteResolve({ deleted: ['a'], failed: [{ id: 'b', error: 'locked' }] });
  await deleting;
  assert.equal(S.portraits.has('a'), false);
  assert.equal(S.portraits.has('b'), true);
  assert.equal(S.portraits.has('c'), true);
  assert.deepEqual([...Gallery.deleteIds], ['b']);
  assert.equal(element('#gallery-delete-dialog').hidden, false);
  assert.equal(element('#gallery-delete-error').hidden, false);
  sandbox.request = async () => { throw new Error('생성 중'); };
  await Gallery.deleteConfirmed();
  assert.equal(element('#gallery-delete-error').textContent, '생성 중');
  assert.equal(S.portraits.has('b'), true);
  sandbox.request = async () => ({ deleted: ['b'], failed: [] });
  await Gallery.deleteConfirmed();
  assert.equal(S.portraits.has('b'), false);
  assert.equal(element('#gallery-delete-dialog').hidden, true);
  assert.equal(Gallery.selected.size, 0);
  console.log('Character UI: draft preservation, IDs, revert, duplicate requests, stale response, error and retry passed');
}
run().catch(err => { console.error(err); process.exitCode = 1; });
