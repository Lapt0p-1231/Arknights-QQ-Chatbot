// node --test tests/test_target_editor.cjs
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const html = fs.readFileSync(path.join(__dirname, '../webui/index.html'), 'utf8');
const script = [...html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/gi)]
  .map(match => match[1]).find(text => text.includes('createApp({'));
let options;
vm.runInNewContext(script, {
  Vue: {createApp(definition) { options = definition; return {mount() {}}; }},
  confirm: () => true,
});

function editor() {
  const state = options.data();
  const requests = [];
  const pending = [];
  for (const [key, method] of Object.entries(options.methods)) state[key] = method.bind(state);
  for (const [key, getter] of Object.entries(options.computed)) {
    Object.defineProperty(state, key, {get: getter.bind(state)});
  }
  state.toast = () => {};
  state.api = (url, args) => {
    requests.push({url, args});
    return new Promise((resolve, reject) => pending.push({resolve, reject}));
  };
  function select(id) {
    const old = state.targetKey;
    state.targetId = id;
    if (state.targetKey !== old) options.watch.targetKey.handler.call(state);
  }
  async function load(id, text) {
    select(id);
    const loading = state.loadTarget();
    pending.shift().resolve({user_id: id, user_persona: text});
    await loading;
  }
  return {state, select, load, requests, pending};
}

test('A -> B without LOAD clears drafts and blocks all target mutations', async () => {
  const {state, select, load, requests} = editor();
  await load('123456', 'only belongs to A');
  assert.equal(state.personaText, 'only belongs to A');
  assert.equal(state.targetReady, true);
  select('234567');
  assert.equal(state.targetReady, false);
  assert.equal(state.personaText, '');
  assert.equal(state.charText, '');
  assert.equal(state.favorValue, 0);
  await state.savePersona();
  await state.clearPersona();
  await state.saveChar();
  await state.clearChar();
  await state.setFavor();
  await state.setTargetMode('聊天');
  await state.setTargetAtOnly('at');
  await state.clearMemory(false);
  assert.equal(requests.length, 1, 'only the LOAD for A should have gone to the server');
  select('123456');
  assert.equal(state.targetReady, false, 'going back to A still requires LOAD');
});

test('out-of-order LOAD responses cannot populate the current editor', async () => {
  const {state, select, pending} = editor();
  select('123456');
  const a = state.loadTarget();
  const loadingA = pending.shift();
  select('234567');
  const b = state.loadTarget();
  const loadingB = pending.shift();
  loadingB.resolve({user_id: '234567', user_persona: 'B'});
  await b;
  loadingA.resolve({user_id: '123456', user_persona: 'A (late)'});
  await a;
  assert.equal(state.targetReady, true);
  assert.equal(state.targetProfile.user_id, '234567');
  assert.equal(state.personaText, 'B');
});

test('writes stay pinned to their loaded target and late responses leave the new draft alone', async () => {
  const {state, select, load, requests, pending} = editor();
  await load('123456', 'A');
  const clearing = state.clearPersona();
  assert.equal(requests.at(-1).args.body.user_id, '123456');
  const pendingClear = pending.shift();
  await load('234567', 'B');
  pendingClear.resolve({ok: true});
  await clearing;
  assert.equal(state.personaText, 'B');
  select('234567 ');
  state.personaText = 'updated B';
  const saving = state.savePersona();
  assert.equal(requests.at(-1).args.body.user_id, '234567');
  assert.equal(requests.at(-1).args.body.text, 'updated B');
  pending.shift().resolve({ok: true});
  await saving;
});

test('failed LOAD and memory-panel reads do not authorize target writes', async () => {
  const {state, select, load, requests, pending} = editor();
  await load('123456', 'A');
  select('234567');
  const loading = state.loadTarget();
  pending.shift().reject(new Error('LOAD failed'));
  await loading;
  state.profile = {user_id: '234567', user_persona: 'from memory panel'};
  assert.equal(state.targetReady, false);
  await state.savePersona();
  assert.equal(requests.length, 2);
});
