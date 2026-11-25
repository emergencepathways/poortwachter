/* Poortwachter study app. Plain JS, no build step, works offline. */
'use strict';

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const S = { state: null, activities: [], settings: null, books: null, view: 'today' };

const CAT_LABEL = {
  flashcards: 'Flashcards', reading: 'Reading', speaking: 'Speaking', textbook: 'Textbook',
  writing: 'Writing', listening: 'Listening', other: 'Other', mystery: 'Mystery',
};
const CAT_ICON = {
  flashcards: '🃏', reading: '📖', speaking: '🗣️', textbook: '📘', writing: '✍️', listening: '🎧', other: '•', mystery: '❓',
};
const WEEKDAYS = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday'];
const DUTCH_DAYS = ['maandag', 'dinsdag', 'woensdag', 'donderdag', 'vrijdag', 'zaterdag', 'zondag'];

// ------------------------------------------------------------------ api

async function api(path, { method = 'GET', body, raw, headers = {} } = {}) {
  const opts = { method, headers: { ...headers } };
  if (method !== 'GET') opts.headers['X-Poortwachter'] = '1';
  if (raw !== undefined) opts.body = raw;
  else if (body !== undefined) { opts.body = JSON.stringify(body); opts.headers['Content-Type'] = 'application/json'; }
  const r = await fetch(path, opts);
  let data = null;
  try { data = await r.json(); } catch { /* empty */ }
  if (!r.ok) throw new Error((data && data.error) || `Request failed (${r.status})`);
  return data;
}
const post = (path, body) => api(path, { method: 'POST', body: body || {} });

function toast(msg, bad = false) {
  const t = $('#toast');
  t.textContent = msg;
  t.className = 'toast' + (bad ? ' bad' : '');
  t.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => { t.hidden = true; }, bad ? 6000 : 3500);
}

async function guard(fn, btn) {
  if (btn) btn.disabled = true;
  try { return await fn(); }
  catch (e) { toast(e.message, true); }
  finally { if (btn) btn.disabled = false; }
}

// ------------------------------------------------------------------ formatting

const fmtMin = (m) => (m >= 60 ? `${Math.floor(m / 60)} h ${m % 60 ? (m % 60) + ' min' : ''}`.trim() : `${m} min`);
const fmtClock = (s) => { s = Math.max(0, Math.floor(s)); return `${String(Math.floor(s / 60)).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}`; };
const dayName = (iso) => { const d = new Date(iso + 'T12:00'); return WEEKDAYS[(d.getDay() + 6) % 7]; };
const dayNl = (iso) => { const d = new Date(iso + 'T12:00'); return DUTCH_DAYS[(d.getDay() + 6) % 7]; };
const fmtDate = (iso) => new Date(iso + 'T12:00').toLocaleDateString(undefined, { weekday: 'long', day: 'numeric', month: 'long' });
const wordCount = (s) => (s.match(/[A-Za-zÀ-ÿ'’-]+/g) || []).length;
const catVar = (c) => `--cat: var(--c-${CAT_LABEL[c] ? c : 'other'})`;

function kindSummary(t) {
  const p = t.params || {};
  switch (t.kind) {
    case 'anki': return `${p.reviews} reviews${p.deck ? ' in ' + p.deck : ''}${p.clear_due ? ' + clear due' : ''}`;
    case 'bw_reading': return `${p.target} ${p.metric} in Boekenwurm`;
    case 'bw_cards': return `${p.cards} Boekenwurm cards`;
    case 'timer': return `${p.minutes} min timer${p.note_words ? ` + ${p.note_words}-word note` : ''}`;
    case 'note': return `${p.min_words}+ words in Dutch`;
    case 'recording': { const s = p.min_seconds || 60; return `Record ${s < 60 ? s + ' s' : Math.round(s / 60 * 10) / 10 + ' min'}`; }
    case 'textbook': return 'Graded by Claude';
    case 'honor': return t.source === 'event' ? `Session${p.place ? ' · ' + p.place : ''}. Tick it off afterwards` : 'Honour check-off';
    default: return '';
  }
}

// ------------------------------------------------------------------ data

async function loadState() {
  S.state = await api('/api/state');
  renderLock();
  return S.state;
}
async function loadActivities() { S.activities = await api('/api/activities'); return S.activities; }
async function loadSettings() { S.settings = await api('/api/settings'); return S.settings; }
async function loadBooks() { S.books = await api('/api/textbooks'); return S.books; }

// ------------------------------------------------------------------ lock bar

function renderLock() {
  const st = S.state; if (!st) return;
  const L = st.lock;
  const pct = L.total ? Math.round((L.done / L.total) * 100) : 100;
  const reset = L.next_reset.split('T')[1];
  let icon, title, sub;
  const free = { install_day: ['🎁', 'Install day: Steam is open', `Poortwachter starts locking at ${reset} tonight. Have a look around, and plan the coming days in Plan.`],
    free_day: ['🏖️', 'Free day: Steam is open', `Paid with 24 saved hours. Geniet ervan! Back to it after ${reset}.`],
    emergency_free_day: ['🔓', 'Emergency key: paid with a free day', `Your chain is safe. Back to it after ${reset}.`] }[L.reason];
  if (free) {
    [icon, title, sub] = free;
  } else if (L.unlocked && L.reason === 'emergency') {
    icon = '🔓'; title = 'Steam unlocked with the emergency key';
    sub = `${L.done}/${L.total} done. Unfinished tasks carry over (max 2). Resets at ${reset}.`;
  } else if (L.unlocked) {
    icon = '🎮'; title = 'Steam is unlocked. Goed gedaan!';
    sub = `All ${L.total} tasks done. Veel plezier! Resets at ${reset}.`;
  } else {
    icon = '🔒'; title = `Steam is locked: ${L.total - L.done} task${L.total - L.done === 1 ? '' : 's'} to go`;
    sub = `${L.done} of ${L.total} done · about ${fmtMin(L.minutes_left)} left · resets at ${reset}`;
  }
  $('#lockbar').innerHTML = `
    <div class="lockcard ${L.unlocked ? 'unlocked' : 'locked'}">
      <div class="lockicon" aria-hidden="true">${icon}</div>
      <div>
        <div class="title">${esc(title)}</div>
        <div class="sub">${esc(sub)}</div>
        <div class="bar" role="progressbar" aria-valuenow="${pct}" aria-valuemin="0" aria-valuemax="100"><span style="width:${pct}%"></span></div>
      </div>
      <div class="streak"><b>${st.streak}</b>day streak<br><a href="#plan" title="Free days saved" style="text-decoration:none">🏖️ ${st.pto.days} d ${st.pto.partial_hours} h</a></div>
    </div>`;
}

// ------------------------------------------------------------------ routing

const VIEWS = { today: viewToday, plan: () => viewPlan(), tomorrow: () => viewPlan(), textbook: viewTextbook, library: viewLibrary, history: viewHistory, settings: viewSettings };

async function route() {
  const [name, query] = (location.hash.slice(1) || 'today').split('?');
  S.view = VIEWS[name] ? name : 'today';
  $$('#nav a').forEach((a) => a.classList.toggle('active', a.dataset.view === (S.view === 'tomorrow' ? 'plan' : S.view)));
  const params = new URLSearchParams(query || '');
  if (params.get('bw')) {
    const v = params.get('bw');
    toast(v === 'ok' ? 'Boekenwurm connected.' : v, v !== 'ok');
    history.replaceState(null, '', '#' + name);
  }
  $('#view').innerHTML = '<div class="empty">Laden…</div>';
  try { await VIEWS[S.view](); }
  catch (e) { $('#view').innerHTML = `<div class="card err">${esc(e.message)}</div>`; }
}
window.addEventListener('hashchange', route);

// ------------------------------------------------------------------ task cards

function taskCard(t, { planner = false } = {}) {
  const mystery = t.kind === 'mystery';
  const prog = t.progress || {};
  let progLine = '';
  if (!mystery && t.status !== 'done') {
    if (t.kind === 'timer' && prog.seconds) progLine = `${Math.floor(prog.seconds / 60)} of ${t.params.minutes} min`;
    else if (prog.message) progLine = prog.message;
  }
  const chips = [
    `<span class="chip cat" style="${catVar(t.category)}">${CAT_ICON[t.category] || ''} ${esc(CAT_LABEL[t.category] || t.category)}</span>`,
    `<span class="chip">~${fmtMin(t.est_min)}</span>`,
  ];
  if (t.source === 'carry') chips.push('<span class="chip carry">carried over</span>');
  if (t.source === 'event') chips.push(`<span class="chip">📅 session${t.time ? ' ' + esc(t.time) : ''}</span>`);
  if (t.source === 'core') chips.push('<span class="chip">core</span>');
  if (!t.counts) chips.push('<span class="chip bonus">bonus</span>');
  return `
    <button class="task ${t.status === 'done' ? 'done' : ''} ${mystery ? 'mystery' : ''}" style="${catVar(t.category)}" data-task="${t.id}" ${planner ? 'disabled' : ''}>
      <div class="name">${mystery ? '❓ Mystery task' : esc(t.name)}</div>
      <div class="prog">${mystery ? 'Revealed when you start it.' : esc(kindSummary(t))}</div>
      ${progLine ? `<div class="prog">${esc(progLine)}</div>` : ''}
      <div class="meta">${chips.join('')}</div>
    </button>`;
}

// ------------------------------------------------------------------ TODAY

async function viewToday() {
  await loadState();
  const D = S.state.today;
  const gating = D.tasks.filter((t) => t.counts);
  const bonus = D.tasks.filter((t) => !t.counts);
  const todo = gating.filter((t) => t.status !== 'done');
  const allDone = gating.length && !todo.length;
  $('#view').innerHTML = `
    <div class="section-title">
      <h1>Vandaag, ${esc(dayNl(D.day))} <small class="muted" style="font-size:.9rem;font-weight:500">${esc(fmtDate(D.day))}</small></h1>
    </div>
    ${['install_day', 'free_day', 'emergency_free_day'].includes(S.state.lock.reason) ? `<div class="card" style="text-align:center"><div class="celebrate">${S.state.lock.reason === 'install_day' ? '🎁' : '🏖️'}</div><h2>${S.state.lock.reason === 'install_day' ? 'Welkom! Today is on the house.' : 'Vrije dag!'}</h2><p class="muted">${S.state.lock.reason === 'install_day' ? 'The lock starts at 02:00. Set things up now: Settings → Textbook → Library → Plan.' : 'Nothing is required today. Anything you do is a bonus.'}</p></div>` : ''}
    ${allDone ? `<div class="card" style="text-align:center"><div class="celebrate">🎉</div><h2>Alles af! Steam is open.</h2><p class="muted">Add a bonus task below if you're on a roll. The coming days are in the Plan tab.</p></div>` : ''}
    <div class="section-title"><h2>Before Steam opens</h2><span class="muted">${gating.length - todo.length}/${gating.length} done</span></div>
    ${gating.length ? `<div class="tasks">${gating.map((t) => taskCard(t)).join('')}</div>` : '<div class="empty">Nothing planned today, so Steam is open.</div>'}
    <div class="section-title"><h2>Bonus practice</h2><span class="muted">doesn't affect Steam</span></div>
    ${bonus.length ? `<div class="tasks">${bonus.map((t) => taskCard(t)).join('')}</div>` : ''}
    <div class="card" style="margin-top:14px">
      <div class="row">
        <select id="bonus-act" style="max-width:360px" aria-label="Activity"></select>
        <button class="ghost" id="add-bonus">+ Add as bonus</button>
        <button class="ghost" id="add-today" title="Adds to today's required tasks. You can only make today harder.">+ Add as required</button>
      </div>
      <p class="hint">Today's plan is locked in. You can add tasks to it but not remove them. Changes to the coming days go in the Plan tab.</p>
    </div>`;
  await loadActivities();
  $('#bonus-act').innerHTML = S.activities.filter((a) => a.enabled).map((a) => `<option value="${a.id}">${esc(a.name)}</option>`).join('');
  $$('[data-task]').forEach((b) => b.addEventListener('click', () => openTask(+b.dataset.task)));
  $('#add-bonus').onclick = (e) => guard(async () => { await post(`/api/day/${D.day}/add`, { activity_id: +$('#bonus-act').value, bonus: true }); viewToday(); }, e.target);
  $('#add-today').onclick = (e) => guard(async () => {
    if (!confirm('Add this to today\'s required tasks? It will have to be done before Steam opens.')) return;
    await post(`/api/day/${D.day}/add`, { activity_id: +$('#bonus-act').value });
    viewToday();
  }, e.target);
}

// ------------------------------------------------------------------ task panel

let panelCleanup = null;

function openPanel(html, { wide = false } = {}) {
  closePanel(true);
  $('#panel').innerHTML = html;
  $('.panel').classList.toggle('wide', wide);
  $('#panel-wrap').hidden = false;
  $('.panel').scrollTop = 0;
}
function closePanel(silent) {
  if (panelCleanup) { const c = panelCleanup; panelCleanup = null; c(); }
  $('#panel-wrap').hidden = true;
  $('#panel').innerHTML = '';
  if (!silent && (S.view === 'today')) viewToday();
}
$('#panel-close').onclick = () => closePanel();
$('#panel-wrap').addEventListener('click', (e) => { if (e.target.id === 'panel-wrap') closePanel(); });
document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && !$('#panel-wrap').hidden) closePanel(); });

function panelHead(t) {
  return `<p class="muted" style="margin:0">${CAT_ICON[t.category] || ''} ${esc(CAT_LABEL[t.category])} · ~${fmtMin(t.est_min)}</p>
    <h1 id="panel-title">${esc(t.name)}</h1>
    ${t.description ? `<p>${esc(t.description)}</p>` : ''}`;
}

async function openTask(id) {
  let t = await api(`/api/tasks/${id}`);
  const act = S.activities.find((a) => a.id === t.activity_id);
  if (act) t.description = act.description;
  if (t.kind === 'mystery') {
    openPanel(`<div style="text-align:center;padding-top:40px">
      <div class="celebrate">❓</div><h1 id="panel-title">Mystery task</h1>
      <p class="muted">About ${fmtMin(t.est_min)}. Once you reveal it, it's yours. No swapping.</p>
      <button class="big warm" id="reveal">Reveal &amp; start</button></div>`);
    $('#reveal').onclick = (e) => guard(async () => { await post(`/api/tasks/${id}/reveal`); await loadState(); openTask(id); }, e.target);
    return;
  }
  if (t.status === 'done') return openDone(t);
  const fn = { anki: panelCheck, bw_reading: panelCheck, bw_cards: panelCheck, timer: panelTimer, note: panelNote, recording: panelRecording, textbook: panelTextbook, honor: panelHonor }[t.kind];
  if (fn) fn(t);
}

function openDone(t) {
  const ev = t.evidence || {};
  let body = '';
  if (ev.note) body += `<h3>Your note</h3><div class="card" style="white-space:pre-wrap">${esc(ev.note)}</div>`;
  if (ev.checked) body += `<p class="ok">✓ ${esc(ev.checked)}</p>`;
  if (ev.recording) body += `<h3>Your recording</h3><p class="muted">${esc(ev.prompt || '')}</p><audio controls src="/files/recording/${esc(ev.recording)}" style="width:100%"></audio>`;
  if (ev.score) body += `<p class="ok">✓ ${esc(ev.label)}: ${ev.score.correct}/${ev.score.total} correct</p><p><a href="#history">See the full feedback in History</a></p>`;
  if (ev.link) body += `<p>Lesson: <a href="${esc(ev.link)}" target="_blank" rel="noopener">${esc(ev.link)}</a></p>`;
  openPanel(`${panelHead(t)}<p class="ok" style="font-size:1.1rem">✓ Done${t.completed_at ? ' at ' + new Date(t.completed_at * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : ''}</p>${body}`);
}

// -- automatic checks (Anki, Boekenwurm)
function panelCheck(t) {
  const p = t.params;
  const how = {
    anki: `Open Anki and do your reviews${p.deck ? ` in <b>${esc(p.deck)}</b>` : ''}. Target: <b>${p.reviews}</b> reviews since 02:00${p.clear_due ? ', and no due cards left' : ''}. You don't need to close Anki first.`,
    bw_reading: `Read in Boekenwurm. Target: <b>${p.target} ${esc(p.metric)}</b> today. Finish or leave the reading session so it syncs, then press Check.`,
    bw_cards: `Review your Boekenwurm flashcards. Target: <b>${p.cards}</b> cards today.`,
  }[t.kind];
  openPanel(`${panelHead(t)}
    <div class="card"><p>${how}</p>
      <div id="check-msg" class="muted">${esc((t.progress || {}).message || 'Not checked yet.')}</div>
      <div class="row" style="margin-top:12px"><button id="check" class="big">Check now</button>
      ${t.kind.startsWith('bw') ? '<a class="btn ghost" href="#settings" onclick="closePanel(true)">Boekenwurm settings</a>' : ''}</div>
      <p class="hint">Poortwachter also checks every few minutes in the background, so Steam opens on its own once you hit the target.</p>
    </div>`);
  const run = async (btn) => guard(async () => {
    const r = await post(`/api/tasks/${t.id}/check`);
    $('#check-msg').innerHTML = `<span class="${r.ok ? 'ok' : ''}">${r.ok ? '✓ ' : ''}${esc(r.message)}</span>`;
    if (r.ok) { toast('Klaar! Task done.'); await loadState(); setTimeout(() => closePanel(), 900); }
  }, btn);
  $('#check').onclick = (e) => run(e.target);
  run();
  const iv = setInterval(() => run(), 60000);
  panelCleanup = () => clearInterval(iv);
}

// -- timer
function panelTimer(t) {
  const p = t.params;
  const need = p.minutes * 60;
  let secs = (t.progress || {}).seconds || 0;
  let running = false; let lastTick = 0; let beatIv = null; let tickIv = null;
  openPanel(`${panelHead(t)}
    <div class="card">
      <div class="clock" id="clock">${fmtClock(Math.max(need - secs, 0))}</div>
      <div class="bar"><span id="tbar" style="width:${Math.min(100, secs / need * 100)}%"></span></div>
      <p class="hint" style="text-align:center">Counts down while this panel is open. Other windows and tabs are fine, but closing the panel pauses it.</p>
      <div class="row" style="justify-content:center"><button class="big" id="go">▶ Start</button></div>
    </div>
    <div class="card" id="notebox" ${secs >= need ? '' : 'hidden'}>
      ${p.note_words ? `<label for="note">${esc(p.note_prompt || `Write ${p.note_words} words in Dutch`)}</label>
        <textarea id="note" lang="nl" spellcheck="true" placeholder="Schrijf in het Nederlands…"></textarea>
        <div class="wordcount" id="wc">0 / ${p.note_words} woorden</div>` : '<p>Time\'s up. Nice work!</p>'}
      <button class="big warm" id="finish" style="margin-top:10px">Finish task</button>
    </div>`);
  const draw = () => {
    $('#clock').textContent = fmtClock(Math.max(need - secs, 0));
    $('#tbar').style.width = Math.min(100, (secs / need) * 100) + '%';
    if (secs >= need) $('#notebox').hidden = false;
  };
  const beat = async (run) => {
    try { const r = await post(`/api/tasks/${t.id}/beat`, { running: run }); secs = r.seconds; draw(); }
    catch (e) { toast(e.message, true); }
  };
  const start = async () => {
    running = true; $('#go').textContent = '⏸ Pause';
    await beat(true); lastTick = Date.now();
    beatIv = setInterval(() => beat(true), 10000);
    tickIv = setInterval(() => { const n = Date.now(); secs += (n - lastTick) / 1000; lastTick = n; draw(); }, 250);
  };
  const stop = async () => {
    running = false; $('#go') && ($('#go').textContent = '▶ Resume');
    clearInterval(beatIv); clearInterval(tickIv);
    await beat(false);
  };
  $('#go').onclick = () => (running ? stop() : start());
  if ($('#note')) $('#note').oninput = (e) => { $('#wc').textContent = `${wordCount(e.target.value)} / ${p.note_words} woorden`; };
  $('#finish').onclick = (e) => guard(async () => {
    if (running) await stop();
    await post(`/api/tasks/${t.id}/finish_timer`, { note: $('#note') ? $('#note').value : '' });
    toast('Goed gedaan!'); await loadState(); closePanel();
  }, e.target);
  panelCleanup = () => { if (running) { clearInterval(beatIv); clearInterval(tickIv); post(`/api/tasks/${t.id}/beat`, { running: false }).catch(() => {}); } };
  window.onbeforeunload = () => (running ? true : undefined);
}

// -- writing
function panelNote(t) {
  const p = t.params;
  const key = `pw-draft-${t.id}`;
  openPanel(`${panelHead(t)}
    <div class="prompt" lang="nl">${esc(p.prompt || 'Schrijf in het Nederlands.')}</div>
    <textarea id="note" lang="nl" spellcheck="true" style="min-height:260px" placeholder="Schrijf hier…"></textarea>
    <div class="wordcount" id="wc">0 / ${p.min_words} woorden</div>
    <p class="hint">Your draft is saved in this browser as you type. Poortwachter checks the word count and that it's written in Dutch, not the grammar.</p>
    <button class="big warm" id="submit">Submit</button>`);
  const ta = $('#note');
  try { ta.value = localStorage.getItem(key) || ''; } catch { /* storage off */ }
  const upd = () => { $('#wc').textContent = `${wordCount(ta.value)} / ${p.min_words} woorden`; try { localStorage.setItem(key, ta.value); } catch { /* */ } };
  ta.oninput = upd; upd(); ta.focus();
  $('#submit').onclick = (e) => guard(async () => {
    await post(`/api/tasks/${t.id}/note`, { text: ta.value });
    try { localStorage.removeItem(key); } catch { /* */ }
    toast('Goed geschreven!'); await loadState(); closePanel();
  }, e.target);
}

// -- honour
function panelHonor(t) {
  const p = t.params;
  openPanel(`${panelHead(t)}
    ${t.source === 'event' ? `<p class="muted">📅 ${t.time ? esc(t.time) + ' · ' : ''}${fmtMin(t.est_min)}${p.place ? ' · ' + esc(p.place) : ''}. Tick it off after the session.</p>` : ''}
    <div class="card">
      ${p.link ? `<label for="link">Lesson link (optional)</label><input type="url" id="link" placeholder="https://www.italki.com/…">` : ''}
      <label for="note">${esc(p.label || 'What did you do?')}</label>
      <textarea id="note" placeholder="Dutch or English is fine here."></textarea>
      <div class="wordcount" id="wc">0 / ${p.note_words || 0} words</div>
      <button class="big warm" id="submit" style="margin-top:10px">Mark as done</button>
      <p class="hint">This one runs on trust. Be honest with yourself.</p>
    </div>`);
  $('#note').oninput = (e) => { $('#wc').textContent = `${wordCount(e.target.value)} / ${p.note_words || 0} words`; };
  $('#submit').onclick = (e) => guard(async () => {
    await post(`/api/tasks/${t.id}/honor`, { note: $('#note').value, link: $('#link') ? $('#link').value : '' });
    toast('Lekker bezig!'); await loadState(); closePanel();
  }, e.target);
}

// -- speaking / recording
async function panelRecording(t) {
  const p = t.params;
  const need = p.min_seconds || 60;
  const { prompt } = await api(`/api/tasks/${t.id}/prompt`);
  openPanel(`${panelHead(t)}
    <div class="prompt" lang="nl" id="prompt">${esc(prompt)}</div>
    ${p.prompt ? '' : '<button class="small ghost" id="newprompt">↻ Different prompt</button>'}
    <div class="card" style="margin-top:14px">
      <div class="clock" id="rclock" style="font-size:3rem">00:00</div>
      <p class="muted" style="text-align:center">Speak for at least <b>${fmtClock(need)}</b> in one recording. Pauses are fine. Keep going!</p>
      <div class="meter"><span id="lvl"></span></div>
      <div class="row" style="justify-content:center">
        <button class="big warm" id="rec">● Record</button>
        <button class="big" id="save" hidden>Save recording</button>
      </div>
      <audio id="play" controls hidden style="width:100%;margin-top:14px"></audio>
      <div id="rmsg" class="hint" style="text-align:center"></div>
    </div>`);
  if ($('#newprompt')) $('#newprompt').onclick = (e) => guard(async () => { const r = await post(`/api/tasks/${t.id}/prompt`); $('#prompt').textContent = r.prompt; }, e.target);

  let stream, rec, chunks = [], startAt = 0, clockIv, raf, ac, blob, dur = 0, mime = '';
  const pickMime = () => ['audio/webm;codecs=opus', 'audio/ogg;codecs=opus', 'audio/webm', 'audio/mp4'].find((m) => window.MediaRecorder && MediaRecorder.isTypeSupported(m)) || '';
  const stopAll = () => {
    clearInterval(clockIv); cancelAnimationFrame(raf);
    if (stream) stream.getTracks().forEach((tr) => tr.stop());
    if (ac) ac.close().catch(() => {});
    stream = null; ac = null;
  };
  panelCleanup = () => { if (rec && rec.state === 'recording') rec.stop(); stopAll(); };

  $('#rec').onclick = async () => {
    if (rec && rec.state === 'recording') { rec.stop(); return; }
    try { stream = await navigator.mediaDevices.getUserMedia({ audio: true }); }
    catch { toast('Microphone access was blocked. Allow it in the browser to record.', true); return; }
    mime = pickMime(); chunks = []; blob = null; $('#save').hidden = true; $('#play').hidden = true; $('#rmsg').textContent = '';
    rec = new MediaRecorder(stream, mime ? { mimeType: mime } : undefined);
    rec.ondataavailable = (e) => e.data.size && chunks.push(e.data);
    rec.onstop = () => {
      dur = (Date.now() - startAt) / 1000; stopAll();
      blob = new Blob(chunks, { type: rec.mimeType || mime || 'audio/webm' });
      $('#play').src = URL.createObjectURL(blob); $('#play').hidden = false;
      $('#rec').textContent = '● Record again'; $('#save').hidden = false;
      $('#rmsg').textContent = dur < need ? `That's ${Math.round(dur)} s. You need ${need} s. Record again, or save it to keep it anyway.` : 'Listen back if you like, then save.';
    };
    rec.start(1000); startAt = Date.now();
    $('#rec').innerHTML = '<span class="rec-dot"></span> Stop';
    clockIv = setInterval(() => {
      const s = (Date.now() - startAt) / 1000;
      $('#rclock').textContent = fmtClock(s);
      $('#rclock').style.color = s >= need ? 'var(--good)' : '';
    }, 250);
    ac = new AudioContext(); const an = ac.createAnalyser(); an.fftSize = 512;
    ac.createMediaStreamSource(stream).connect(an);
    const buf = new Uint8Array(an.fftSize);
    const loop = () => { an.getByteTimeDomainData(buf); let m = 0; for (const v of buf) m = Math.max(m, Math.abs(v - 128)); $('#lvl').style.width = Math.min(100, m / 1.1) + '%'; raf = requestAnimationFrame(loop); };
    loop();
  };
  $('#save').onclick = (e) => guard(async () => {
    const ext = (blob.type.includes('ogg') ? 'ogg' : blob.type.includes('mp4') ? 'm4a' : 'webm');
    const r = await api(`/api/tasks/${t.id}/recording`, { method: 'POST', raw: blob, headers: { 'Content-Type': blob.type, 'X-Seconds': String(dur), 'X-Ext': ext } });
    if (r.ok) { toast(r.message); await loadState(); closePanel(); }
    else { $('#rmsg').innerHTML = `<span class="err">${esc(r.message)}</span>`; }
  }, e.target);
}

// -- textbook (study view)
async function panelTextbook(t) {
  const target = await api(`/api/tasks/${t.id}/textbook`);
  if (target.error) {
    openPanel(`${panelHead(t)}<div class="card"><p class="err">${esc(target.error)}</p><a class="btn" href="#textbook" onclick="closePanel(true)">Go to Textbook</a></div>`);
    return;
  }
  await loadBooks();
  const book = S.books.books.find((b) => b.id === target.textbook_id);
  studyView({ book, task: t, target });
}

function answerTemplate(items) {
  return items.map((it) => `## ${it.label}\n1. \n2. \n3. \n`).join('\n');
}

function studyView({ book, task = null, target = {}, practice = false, set = null }) {
  const items = set ? set.items : (target.items || []);
  const title = set ? set.title : target.title;
  const free = !items.length;
  const off = book.page_offset || 0;
  const startPage = items.length ? items[0].page_from : 1;
  const draftKey = task ? `pw-tb-${task.id}` : `pw-tb-practice-${book.id}-${set ? set.id : 'free'}`;
  const pdfSrc = (p) => `/files/textbook/${book.id}.pdf#page=${p + off}&zoom=page-width`;
  openPanel(`
    <div class="study">
      <iframe id="pdf" title="Textbook" src="${pdfSrc(startPage)}"></iframe>
      <div class="side">
        <p class="muted" style="margin:0">📘 ${esc(book.name)}${practice ? ' · practice (doesn\'t count)' : ''}</p>
        <h1 id="panel-title">${free ? 'Choose an exercise' : esc(title)}</h1>
        ${free ? `
          ${target.message ? `<p class="muted">${esc(target.message)}</p>` : ''}
          <div class="row">
            <div style="flex:2"><label for="tb-label">Exercise</label><input type="text" id="tb-label" placeholder="e.g. H7 Oefening 4"></div>
            <div style="flex:1"><label for="tb-from">Page</label><input type="number" id="tb-from" min="1" value="${startPage}"></div>
            <div style="flex:1"><label for="tb-to">to</label><input type="number" id="tb-to" min="1" placeholder="(same)"></div>
          </div>
          <button class="small ghost" id="tb-go" style="margin-top:8px">Show page</button>`
        : `<div class="tb-items">${items.map((it, i) => `
            <button class="tb-item ${i === 0 ? 'active' : ''}" data-page="${it.page_from}">
              <span>${it.review ? '🔁 ' : ''}${esc(it.label)}</span>
              <small>p. ${it.page_from}${it.page_to !== it.page_from ? '–' + it.page_to : ''} · ~${it.minutes} min</small>
              ${it.note ? `<small class="tb-note">${esc(it.note)}</small>` : ''}
            </button>`).join('')}</div>
          <p class="hint">Click an exercise to jump to its page. 🔁 = review from an earlier chapter.</p>`}
        <label for="answers">Your answers</label>
        <textarea id="answers" class="answers" lang="nl" spellcheck="false" placeholder="1. …&#10;2. …&#10;&#10;Number your answers the way the book does."></textarea>
        <div class="wordcount" id="wc"></div>
        <button class="big warm" id="grade">Submit for grading</button>
        <p class="hint">Only these pages and your answers go to Claude, using your own API key. Grading takes up to a minute.</p>
        <div id="results"></div>
      </div>
    </div>`, { wide: true });
  const ta = $('#answers');
  let draft = null;
  try { draft = localStorage.getItem(draftKey); } catch { /* */ }
  ta.value = draft || (items.length ? answerTemplate(items) : '');
  const upd = () => { $('#wc').textContent = `${wordCount(ta.value)} words`; try { localStorage.setItem(draftKey, ta.value); } catch { /* */ } };
  ta.oninput = upd; upd();
  $$('.tb-item').forEach((b) => (b.onclick = () => {
    $$('.tb-item').forEach((x) => x.classList.remove('active')); b.classList.add('active');
    $('#pdf').src = pdfSrc(+b.dataset.page);
  }));
  if ($('#tb-go')) $('#tb-go').onclick = () => { $('#pdf').src = pdfSrc(+$('#tb-from').value || 1); };
  $('#grade').onclick = (e) => guard(async () => {
    const body = { answers: ta.value };
    if (free) {
      body.label = $('#tb-label').value; body.page_from = +$('#tb-from').value; body.page_to = +$('#tb-to').value || body.page_from;
      if (!body.page_from) throw new Error('Enter the page number of the exercise.');
    }
    if (set) body.set_id = set.id;
    $('#results').innerHTML = '<div class="card muted">Claude is grading… (this takes 15–60 seconds)</div>';
    let r;
    try {
      r = practice ? await post(`/api/textbooks/${book.id}/practice`, body) : await post(`/api/tasks/${task.id}/textbook`, body);
    } catch (err) { $('#results').innerHTML = ''; throw err; }
    try { localStorage.removeItem(draftKey); } catch { /* */ }
    $('#results').innerHTML = renderResult(r);
    $('#grade').disabled = true;
    if (!practice) { toast('Graded! Task done.'); await loadState(); }
    $('#results').scrollIntoView({ behavior: 'smooth' });
  }, e.target);
}

function renderResult(r) {
  const items = r.items || [];
  const sc = r.score || {};
  return `
    <div class="card" style="margin-top:16px">
      <div class="spread"><div><div class="score">${sc.correct ?? '?'} / ${sc.total ?? items.length}</div><div class="muted">correct</div></div>
      <div style="flex:1;min-width:200px"><p>${esc(r.summary || '')}</p>${r.summary_nl ? `<p lang="nl"><i>${esc(r.summary_nl)}</i></p>` : ''}</div></div>
      ${r.exercise_found === false ? '<p class="err">Claude couldn\'t find the exercise on those pages. Check the page offset in the Textbook tab.</p>' : ''}
      ${(r.missing || []).length ? `<p class="err">No answers found for: ${r.missing.map(esc).join(', ')}</p>` : ''}
    </div>
    ${items.map((i) => `
      <div class="result-item ${i.correct ? '' : 'wrong'}">
        <div class="q">${i.exercise ? esc(i.exercise) + ' · ' : ''}${esc(i.number)}. ${esc(i.question || '')}</div>
        <div>${i.correct ? '✓' : '✗'} <span ${i.correct ? '' : 'style="text-decoration:line-through;opacity:.75"'}>${esc(i.answer || '')}</span></div>
        ${!i.correct || (i.corrected && i.corrected !== i.answer) ? `<div class="fix" lang="nl">→ ${esc(i.corrected || '')}</div>` : ''}
        <dl>
          ${i.rule ? `<dt>Rule</dt><dd>${esc(i.rule)}</dd>` : ''}
          ${i.example ? `<dt>Example</dt><dd lang="nl">${esc(i.example)}</dd>` : ''}
          ${i.note ? `<dt>Watch out</dt><dd>${esc(i.note)}</dd>` : ''}
        </dl>
      </div>`).join('')}`;
}

// ------------------------------------------------------------------ PLAN (a week ahead)

const fmtHours = (h) => `${Math.floor(h / 24)} d ${h % 24} h`;
const shortDay = (iso) => { const d = new Date(iso + 'T12:00'); return d.toLocaleDateString(undefined, { weekday: 'short', day: 'numeric', month: 'short' }); };

async function viewPlan(selected) {
  await Promise.all([loadState(), loadActivities()]);
  const week = S.state.week;
  const day = selected && week.some((w) => w.day === selected) ? selected : (S.planDay && week.some((w) => w.day === S.planDay) ? S.planDay : week[0].day);
  S.planDay = day;
  const [D, sess] = await Promise.all([api(`/api/day/${day}`), api('/api/sessions')]);
  const P = S.state.pto;
  const over = D.planned_minutes - D.budget;
  const caps = D.caps || {};
  const capLine = Object.entries(caps).map(([c, cap]) => {
    const m = D.category_minutes[c] || 0;
    return `<span class="chip ${m >= cap ? 'carry' : ''}">${CAT_ICON[c] || ''} ${esc(CAT_LABEL[c] || c)} ${m}/${cap} min</span>`;
  }).join(' ');
  const rows = D.tasks.filter((t) => t.counts || t.source === 'event').map((t) => {
    const mystery = t.kind === 'mystery';
    const isSession = t.source === 'event';
    const p = t.params || {};
    return `<div class="plan-row" style="${catVar(t.category)}">
      <div><div class="name">${isSession ? '📅 ' : ''}${mystery ? '❓ Mystery task' : esc(t.name)}${isSession && t.time ? ` <span class="muted" style="font-weight:500">${esc(t.time)}</span>` : ''}</div>
        <div class="muted" style="font-size:.85rem">${mystery ? 'Hidden until you start it' : isSession ? `Session${p.place ? ' · ' + esc(p.place) : ''}` : esc(CAT_LABEL[t.category]) + ' · ' + esc(kindSummary(t))} · ~${fmtMin(t.est_min)}${t.source === 'core' ? ' · core' : ''}${!t.counts ? ' · not required (free day)' : ''}</div></div>
      <div class="tools">
        ${isSession ? `<button class="small ghost" data-skip="${t.event_id}" title="Not happening this time">Skip this one</button>` : `
        ${!mystery && t.source !== 'core' ? `<button class="small ghost" data-swap="${t.id}" title="Swap for another random activity">↻ Swap</button>` : ''}
        ${!mystery ? `<button class="small ghost" data-myst="${t.id}" title="Hide it until the day">❓ Hide</button>` : ''}
        <button class="small danger" data-rm="${t.id}" title="Remove">✕</button>`}
      </div></div>`;
  }).join('');
  const weekly = sess.sessions.filter((e) => e.weekday !== null);
  const oneoff = sess.sessions.filter((e) => e.date);
  $('#view').innerHTML = `
    <div class="section-title"><h1>Plan</h1><span class="muted">Today is fixed. Plan up to ${week.length} days ahead.</span></div>
    <div class="weekstrip">${week.map((w) => `
      <button class="wday ${w.day === day ? 'active' : ''} ${w.free ? 'free' : ''}" data-day="${w.day}">
        <b>${esc(dayNl(w.day))}</b><small>${esc(w.day.slice(8))}-${esc(w.day.slice(5, 7))}</small>
        <span>${w.free ? '🏖️ free' : `${w.planned_minutes}/${w.budget} min`}</span>
        ${w.sessions ? `<span class="dot">📅 ${w.sessions}</span>` : ''}
      </button>`).join('')}</div>
    <div class="grid2">
      <div>
        <div class="section-title" style="margin-top:6px"><h2>${esc(fmtDate(day))}</h2></div>
        ${D.free ? `<div class="card" style="border-left:6px solid var(--good)"><h3>🏖️ Free day</h3><p class="muted">Nothing is required, so Steam is open all day. Sessions still show up as reminders.</p>
          ${D.free === 'planned' ? '<button class="ghost small" id="cancel-free">Cancel free day (refunds 24 h)</button>' : ''}</div>` : `
        <div class="card">
          <div class="spread"><b>Planned: ${fmtMin(D.planned_minutes)}</b><span class="muted">budget ${fmtMin(D.budget)}</span></div>
          <div class="bar"><span style="width:${Math.min(100, D.planned_minutes / Math.max(D.budget, 1) * 100)}%"></span></div>
          <p class="hint">${D.sessions.reduce((a, t) => a + t.est_min, 0) >= D.budget ? `Sessions take up this day, so only the minimum task is required besides them.` : over > 0 ? `${fmtMin(over)} over budget. That's allowed.` : over < -10 ? `${fmtMin(-over)} under budget.` : 'Right on budget.'}${day === week[0].day ? ' Up to 2 unfinished tasks from today will be added at 02:00.' : ''}</p>
          <div class="row">${capLine}</div>
        </div>`}
        ${rows || '<div class="empty">Nothing planned. Steam would be open all day.</div>'}
        ${D.free ? '' : `
        <div class="card" style="margin-top:12px">
          <h3>Add to this day</h3>
          <div class="row"><select id="add-act" style="flex:1">${S.activities.filter((a) => a.enabled).map((a) => `<option value="${a.id}">${esc(a.name)} (~${a.est_min} min)</option>`).join('')}</select>
          <button id="add">Add</button></div>
          <div class="row" style="margin-top:10px"><select id="myst-cat" style="flex:1"><option value="">Any category</option>${S.state.categories.map(([k, v]) => `<option value="${k}">${esc(v)}</option>`).join('')}</select>
          <button class="warm" id="add-myst">+ ❓ Surprise me</button></div>
          <hr>
          <div class="row"><button class="ghost" id="regen">↻ Redraw this day</button>
          <button class="ghost" id="take-free" ${P.hours < 24 ? 'disabled title="You need 24 saved hours"' : ''}>🏖️ Take this day off (−24 h)</button></div>
        </div>`}
      </div>
      <div>
        <div class="card">
          <h2>📅 Sessions</h2>
          <p class="muted">italki lessons, library group study, taalcafés. Their minutes count toward the day's budget. If a session fills the whole budget, only "${esc((S.activities.find((a) => a.id === S.state.event_minimum) || {}).name || 'the minimum task')}" is still required.</p>
          <div class="row">
            <button class="small" data-quick="italki">+ italki lesson on ${esc(dayNl(day))}</button>
            <button class="small ghost" data-newsess="1">+ Other session…</button>
          </div>
          ${weekly.length ? `<h3 style="margin-top:14px">Every week</h3>${weekly.map((e) => `<div class="plan-row" style="--cat: var(--c-${e.category})"><div><div class="name">${esc(e.name)}</div>
            <div class="muted" style="font-size:.85rem">${WEEKDAYS[e.weekday]}s${e.time ? ' ' + esc(e.time) : ''} · ${e.minutes} min${e.place ? ' · ' + esc(e.place) : ''}${e.end_date ? ' · until ' + esc(e.end_date) : ''}</div></div>
            <div class="tools"><button class="small ghost" data-editsess="${e.id}">Edit</button><button class="small danger" data-endsess="${e.id}">End</button></div></div>`).join('')}` : ''}
          ${oneoff.length ? `<h3 style="margin-top:14px">Dates</h3>${oneoff.map((e) => `<div class="plan-row" style="--cat: var(--c-${e.category})"><div><div class="name">${esc(e.name)}</div>
            <div class="muted" style="font-size:.85rem">${esc(shortDay(e.date))}${e.time ? ' ' + esc(e.time) : ''} · ${e.minutes} min${e.place ? ' · ' + esc(e.place) : ''}</div></div>
            <div class="tools"><button class="small ghost" data-editsess="${e.id}">Edit</button><button class="small danger" data-endsess="${e.id}">✕</button></div></div>`).join('')}` : ''}
          ${!weekly.length && !oneoff.length ? '<p class="muted" style="margin-top:10px">No sessions yet.</p>' : ''}
        </div>
        <div class="card">
          <h2>🏖️ Free days</h2>
          <div class="spread"><div><div class="score">${P.days} d <span style="font-size:1.1rem">${P.partial_hours} h</span></div><div class="muted">saved · cap ${P.cap_days} days${P.at_cap ? ' (reached: nothing more accrues)' : ''}</div></div>
          <div style="flex:1;min-width:180px"><div class="bar"><span style="width:${P.partial_hours / 24 * 100}%"></span></div><p class="hint">${24 - P.partial_hours} more completed day(s) until the next free day.</p></div></div>
          <p class="muted" style="font-size:.88rem">Each completed day earns 1 hour, and 24 hours = 1 free day. A missed day wipes the hours toward the next one (banked days stay). Free days don't break the chain. The emergency key uses a banked day first.</p>
          ${P.upcoming.length ? `<p><b>Planned:</b> ${P.upcoming.map((f) => esc(shortDay(f.day))).join(', ')}</p>` : ''}
          <details><summary>History</summary><table><tbody>${P.ledger.map((l) => `<tr><td><small>${esc(l.day)}</small></td><td style="white-space:nowrap">${l.hours > 0 ? '+' : ''}${l.hours} h</td><td><small>${esc(l.reason)}</small></td></tr>`).join('') || '<tr><td class="muted">Nothing yet.</td></tr>'}</tbody></table></details>
        </div>
      </div>
    </div>`;
  const again = () => viewPlan(day);
  $$('[data-day]').forEach((b) => (b.onclick = () => viewPlan(b.dataset.day)));
  $$('[data-swap]').forEach((b) => (b.onclick = () => guard(async () => { await post(`/api/tasks/${b.dataset.swap}/swap`); again(); }, b)));
  $$('[data-myst]').forEach((b) => (b.onclick = () => guard(async () => { await post(`/api/tasks/${b.dataset.myst}/mystify`); again(); }, b)));
  $$('[data-rm]').forEach((b) => (b.onclick = () => guard(async () => { await post(`/api/tasks/${b.dataset.rm}/remove`); again(); }, b)));
  $$('[data-skip]').forEach((b) => (b.onclick = () => guard(async () => { await post(`/api/sessions/${b.dataset.skip}/skip`, { day }); again(); }, b)));
  $$('[data-endsess]').forEach((b) => (b.onclick = () => guard(async () => { if (!confirm('End this session? Weekly sessions stop from tomorrow; dates are removed.')) return; await post(`/api/sessions/${b.dataset.endsess}/end`); again(); }, b)));
  $$('[data-editsess]').forEach((b) => (b.onclick = () => editSession(sess.sessions.find((e) => e.id === +b.dataset.editsess), sess.presets, day)));
  $$('[data-newsess]').forEach((b) => (b.onclick = () => editSession(null, sess.presets, day)));
  $$('[data-quick]').forEach((b) => (b.onclick = () => editSession({ kind: 'italki', date: day, ...sess.presets.italki, weekday: null }, sess.presets, day, true)));
  if ($('#add')) $('#add').onclick = (e) => guard(async () => { await post(`/api/day/${day}/add`, { activity_id: +$('#add-act').value }); again(); }, e.target);
  if ($('#add-myst')) $('#add-myst').onclick = (e) => guard(async () => { await post(`/api/day/${day}/mystery`, { category: $('#myst-cat').value }); again(); }, e.target);
  if ($('#regen')) $('#regen').onclick = (e) => guard(async () => { if (!confirm('Redraw this day? Your edits to it will be lost (sessions stay).')) return; await post(`/api/day/${day}/regenerate`); again(); }, e.target);
  if ($('#take-free')) $('#take-free').onclick = (e) => guard(async () => { if (!confirm(`Take ${fmtDate(day)} off? This uses 24 saved hours.`)) return; await post('/api/pto/free', { day }); toast('Enjoy your free day!'); again(); }, e.target);
  if ($('#cancel-free')) $('#cancel-free').onclick = (e) => guard(async () => { await post('/api/pto/cancel', { day }); again(); }, e.target);
}

function editSession(e, presets, day, isNew = false) {
  isNew = isNew || !e || !e.id;
  e = e || { kind: 'library', ...presets.library, weekday: null, date: day, time: '', place: '' };
  const repeat = e.weekday !== null && e.weekday !== undefined ? 'weekly' : 'date';
  openPanel(`
    <h1 id="panel-title">${isNew ? 'New session' : 'Edit session'}</h1>
    <label for="s-kind">Type</label>
    <select id="s-kind">${Object.entries(presets).map(([k, v]) => `<option value="${k}" ${k === e.kind ? 'selected' : ''}>${esc(v.name)}</option>`).join('')}</select>
    <label for="s-name">Name</label><input type="text" id="s-name" value="${esc(e.name)}">
    <div class="row">
      <div style="flex:1"><label for="s-min">Minutes</label><input type="number" id="s-min" min="5" max="600" step="5" value="${e.minutes}"></div>
      <div style="flex:1"><label for="s-time">Start time</label><input type="text" id="s-time" placeholder="19:00" value="${esc(e.time || '')}"></div>
    </div>
    <label for="s-place">Place / link (optional)</label><input type="text" id="s-place" value="${esc(e.place || '')}" placeholder="Centrale Bibliotheek, Spui 68">
    <label>Repeats</label>
    <div class="row"><label class="inline"><input type="radio" name="rep" value="date" ${repeat === 'date' ? 'checked' : ''}> On a date</label>
    <label class="inline"><input type="radio" name="rep" value="weekly" ${repeat === 'weekly' ? 'checked' : ''}> Every week</label></div>
    <div id="rep-date"><label for="s-date">Date</label><input type="date" id="s-date" value="${esc(e.date || day)}"></div>
    <div id="rep-week">
      <label for="s-wd">Weekday</label><select id="s-wd">${WEEKDAYS.map((w, i) => `<option value="${i}" ${i === e.weekday ? 'selected' : ''}>${w}</option>`).join('')}</select>
      <div class="row"><div style="flex:1"><label for="s-from">From</label><input type="date" id="s-from" value="${esc(e.start_date || '')}"></div>
      <div style="flex:1"><label for="s-to">Until (optional)</label><input type="date" id="s-to" value="${esc(e.end_date || '')}"></div></div>
    </div>
    <label class="inline" style="margin-top:14px"><input type="checkbox" id="s-link" ${e.link ? 'checked' : ''}> Ask for a link when ticking it off (italki lesson page)</label>
    <div class="row" style="margin-top:18px"><button class="big" id="s-save">Save</button></div>
    <p class="hint">Afterwards you tick it off on the day with a short note: what you practised, who you talked to. It's honour-based.</p>`);
  const showRep = () => { const w = $('input[name=rep]:checked').value === 'weekly'; $('#rep-week').hidden = !w; $('#rep-date').hidden = w; };
  $$('input[name=rep]').forEach((r) => (r.onchange = showRep)); showRep();
  $('#s-kind').onchange = () => { const p = presets[$('#s-kind').value]; $('#s-name').value = p.name; $('#s-min').value = p.minutes; $('#s-link').checked = !!p.link; };
  $('#s-save').onclick = (ev) => guard(async () => {
    const weekly = $('input[name=rep]:checked').value === 'weekly';
    const body = {
      kind: $('#s-kind').value, name: $('#s-name').value, minutes: +$('#s-min').value, time: $('#s-time').value,
      place: $('#s-place').value, link: $('#s-link').checked,
      date: weekly ? null : $('#s-date').value, weekday: weekly ? +$('#s-wd').value : null,
      start_date: weekly ? $('#s-from').value || null : null, end_date: weekly ? $('#s-to').value || null : null,
    };
    await post(isNew ? '/api/sessions' : `/api/sessions/${e.id}`, body);
    toast('Session saved.'); closePanel(true); viewPlan(day);
  }, ev.target);
}

// ------------------------------------------------------------------ LIBRARY (activities + weekly plan)

const KIND_FIELDS = {
  anki: [['deck', 'text', 'Deck (empty = all decks; subdecks included)'], ['reviews', 'number', 'Reviews needed today'], ['clear_due', 'checkbox', 'Also require no due cards left']],
  bw_reading: [['metric', 'select:pages,paragraphs,minutes', 'Measure'], ['target', 'number', 'Target']],
  bw_cards: [['cards', 'number', 'Cards reviewed today']],
  timer: [['minutes', 'number', 'Minutes'], ['note_words', 'number', 'Dutch note afterwards: minimum words (0 = none)'], ['note_prompt', 'text', 'Note prompt']],
  note: [['min_words', 'number', 'Minimum words'], ['prompt', 'text', 'Writing prompt']],
  recording: [['min_seconds', 'number', 'Minimum seconds'], ['prompt', 'text', 'Fixed prompt (empty = random from your prompt list)']],
  textbook: [['textbook_id', 'book', 'Textbook (empty = the active one)']],
  honor: [['note_words', 'number', 'Note: minimum words'], ['label', 'text', 'Question to answer'], ['link', 'checkbox', 'Ask for a link (e.g. italki lesson)']],
};
const KIND_DEFAULTS = {
  anki: { deck: '', reviews: 50, clear_due: false }, bw_reading: { metric: 'pages', target: 5 }, bw_cards: { cards: 20 },
  timer: { minutes: 15, note_words: 20, note_prompt: '' }, note: { min_words: 50, prompt: '' }, recording: { min_seconds: 120, prompt: '' },
  textbook: { mode: 'queue' }, honor: { note_words: 10, label: '', link: false },
};

async function viewLibrary() {
  await Promise.all([loadActivities(), loadSettings(), loadState()]);
  const sched = S.settings.schedule;
  const acts = S.activities;
  const byCat = {};
  acts.forEach((a) => { (byCat[a.category] = byCat[a.category] || []).push(a); });
  $('#view').innerHTML = `
    <div class="section-title"><h1>Library</h1><button id="new-act">+ New activity</button></div>
    <div class="card">
      <div class="spread"><h2 style="margin:0">Weekly plan</h2><button id="save-week">Save weekly plan</button></div>
      <p class="muted">Each weekday: a minute budget, the <b>core</b> tasks that are always on, and how much of the random fill becomes ❓ mystery tasks. The rest of the budget is drawn from the pool. Changes apply from tomorrow.</p>
      <div class="week">${WEEKDAYS.map((d, i) => {
        const s = sched[String(i)];
        return `<div class="day" data-wd="${i}">
          <h3>${d}</h3>
          <label>Budget (min)</label><input type="number" min="0" max="600" step="5" class="wk-budget" value="${s.budget}">
          <label>Mystery: <span class="wk-mv">${Math.round(s.mystery * 100)}</span>%</label>
          <input type="range" min="0" max="100" step="5" class="wk-myst" value="${Math.round(s.mystery * 100)}" style="width:100%">
          <label>Core tasks</label>
          <div class="core-list">${acts.filter((a) => a.enabled).map((a) => `<label><input type="checkbox" class="wk-core" value="${a.id}" ${s.core.includes(a.id) ? 'checked' : ''}> ${esc(a.name)}</label>`).join('')}</div>
        </div>`;
      }).join('')}</div>
    </div>
    ${S.state.categories.filter(([k]) => byCat[k]).map(([k, v]) => `
      <div class="section-title"><h2>${CAT_ICON[k]} ${esc(v)}</h2></div>
      <div class="card" style="padding:6px 10px"><table>
        <thead><tr><th>Activity</th><th>Check</th><th>Time</th><th>Pool weight</th><th></th></tr></thead>
        <tbody>${byCat[k].map((a) => `<tr style="${a.enabled ? '' : 'opacity:.5'}">
          <td><b>${esc(a.name)}</b><br><small>${esc(a.description)}</small></td>
          <td><small>${esc(kindSummary(a))}</small></td>
          <td>${a.est_min} min</td>
          <td>${a.in_pool ? a.weight : '<small>not in pool</small>'}${a.enabled ? '' : '<br><small>disabled</small>'}</td>
          <td><button class="small ghost" data-edit="${a.id}">Edit</button></td></tr>`).join('')}</tbody></table></div>`).join('')}`;
  $$('.wk-myst').forEach((r) => (r.oninput = () => { r.closest('.day').querySelector('.wk-mv').textContent = r.value; }));
  $('#save-week').onclick = (e) => guard(async () => {
    const schedule = {};
    $$('.week .day').forEach((d) => {
      schedule[d.dataset.wd] = {
        budget: +d.querySelector('.wk-budget').value,
        mystery: +d.querySelector('.wk-myst').value / 100,
        core: $$('.wk-core', d).filter((c) => c.checked).map((c) => +c.value),
      };
    });
    await post('/api/settings', { schedule });
    toast('Weekly plan saved. The coming days have been redrawn (except days you edited by hand).');
  }, e.target);
  $('#new-act').onclick = () => editActivity(null);
  $$('[data-edit]').forEach((b) => (b.onclick = () => editActivity(acts.find((a) => a.id === +b.dataset.edit))));
}

async function editActivity(a) {
  const isNew = !a;
  a = a || { name: '', category: 'reading', kind: 'timer', params: { ...KIND_DEFAULTS.timer }, est_min: 15, weight: 1, in_pool: 1, enabled: 1, description: '' };
  if (!S.books) await loadBooks().catch(() => {});
  openPanel(`
    <h1 id="panel-title">${isNew ? 'New activity' : 'Edit activity'}</h1>
    <label for="f-name">Name</label><input type="text" id="f-name" value="${esc(a.name)}">
    <div class="row">
      <div style="flex:1"><label for="f-cat">Category</label><select id="f-cat">${S.state.categories.map(([k, v]) => `<option value="${k}" ${k === a.category ? 'selected' : ''}>${esc(v)}</option>`).join('')}</select></div>
      <div style="flex:1"><label for="f-kind">How it's checked</label><select id="f-kind">${Object.entries(S.state.kinds).map(([k, v]) => `<option value="${k}" ${k === a.kind ? 'selected' : ''}>${esc(v)}</option>`).join('')}</select></div>
    </div>
    <div id="f-params"></div>
    <div class="row">
      <div style="flex:1"><label for="f-est">Estimated minutes</label><input type="number" id="f-est" min="1" value="${a.est_min}"></div>
      <div style="flex:1"><label for="f-w">Pool weight</label><input type="number" id="f-w" min="0" max="10" step="0.1" value="${a.weight}"></div>
    </div>
    <p class="hint">Estimated minutes fill the daily budget. Higher weight = drawn more often (1 is normal, 0 = never random).</p>
    <div class="row" style="margin-top:10px">
      <label class="inline"><input type="checkbox" id="f-pool" ${a.in_pool ? 'checked' : ''}> In the random pool</label>
      <label class="inline"><input type="checkbox" id="f-on" ${a.enabled ? 'checked' : ''}> Enabled</label>
    </div>
    <label for="f-desc">Description / instructions</label><textarea id="f-desc" style="min-height:80px">${esc(a.description)}</textarea>
    <div class="row" style="margin-top:16px"><button id="f-save" class="big">Save</button>${isNew ? '' : '<button id="f-del" class="danger">Delete</button>'}</div>
    <p class="hint">Changes apply from tomorrow. Today's tasks keep the settings they started with.</p>`);
  let params = { ...a.params };
  const drawParams = () => {
    const kind = $('#f-kind').value;
    if (!KIND_FIELDS[kind]) { $('#f-params').innerHTML = ''; return; }
    if (kind !== a.kind && !drawParams.touched) params = { ...KIND_DEFAULTS[kind] };
    $('#f-params').innerHTML = KIND_FIELDS[kind].map(([key, type, label]) => {
      const v = params[key];
      if (type === 'checkbox') return `<label class="inline" style="margin-top:12px"><input type="checkbox" data-p="${key}" ${v ? 'checked' : ''}> ${esc(label)}</label>`;
      if (type.startsWith('select:')) return `<label>${esc(label)}</label><select data-p="${key}">${type.slice(7).split(',').map((o) => `<option ${o === v ? 'selected' : ''}>${o}</option>`).join('')}</select>`;
      if (type === 'book') return `<label>${esc(label)}</label><select data-p="${key}" data-num="1"><option value="">Active textbook</option>${(S.books?.books || []).map((b) => `<option value="${b.id}" ${b.id === v ? 'selected' : ''}>${esc(b.name)}</option>`).join('')}</select>`;
      return `<label>${esc(label)}</label><input type="${type}" data-p="${key}" value="${esc(v ?? '')}" ${key === 'deck' ? 'list="decklist"' : ''}>`;
    }).join('') + (kind === 'anki' ? '<datalist id="decklist"></datalist>' : '');
    if (kind === 'anki') api('/api/anki/decks').then((d) => { $('#decklist').innerHTML = d.map((n) => `<option value="${esc(n)}">`).join(''); }).catch(() => {});
  };
  $('#f-kind').onchange = () => { drawParams.touched = false; a.kind = null; drawParams(); };
  drawParams();
  const collect = () => {
    const p = { ...(KIND_DEFAULTS[$('#f-kind').value] || {}) };
    $$('[data-p]').forEach((el) => {
      const k = el.dataset.p;
      if (el.type === 'checkbox') p[k] = el.checked;
      else if (el.type === 'number' || el.dataset.num) p[k] = el.value === '' ? null : +el.value;
      else p[k] = el.value;
    });
    return p;
  };
  $('#f-save').onclick = (e) => guard(async () => {
    const body = {
      name: $('#f-name').value, category: $('#f-cat').value, kind: $('#f-kind').value, params: collect(),
      est_min: +$('#f-est').value, weight: +$('#f-w').value, in_pool: $('#f-pool').checked, enabled: $('#f-on').checked, description: $('#f-desc').value,
    };
    await post(isNew ? '/api/activities' : `/api/activities/${a.id}`, body);
    toast('Saved.'); closePanel(true); viewLibrary();
  }, e.target);
  if ($('#f-del')) $('#f-del').onclick = (e) => guard(async () => {
    if (!confirm('Delete this activity? Past tasks keep their history. To keep it but stop drawing it, untick Enabled instead.')) return;
    await post(`/api/activities/${a.id}/delete`); closePanel(true); viewLibrary();
  }, e.target);
}

// ------------------------------------------------------------------ TEXTBOOK tab

async function viewTextbook() {
  await loadBooks();
  const { books, active } = S.books;
  const cap = ((await loadSettings()).category_caps || {}).textbook;
  $('#view').innerHTML = `
    <div class="section-title"><h1>Textbook</h1></div>
    <div class="grid2">
      <div>
        ${books.map((b) => {
          const open = b.sets.filter((x) => !x.done);
          const done = b.sets.length - open.length;
          return `
          <div class="card">
            <div class="spread"><h2 style="margin:0">📘 ${esc(b.name)}</h2>${b.id === active ? '<span class="chip bonus">active</span>' : `<button class="small ghost" data-act="${b.id}">Make active</button>`}</div>
            <p class="muted">${b.pages || '?'} PDF pages · ${b.sets.length} sets, ${done} done</p>
            <label>Page offset</label>
            <div class="row"><input type="number" style="max-width:110px" value="${b.page_offset}" data-off="${b.id}"><button class="small ghost" data-saveoff="${b.id}">Save</button></div>
            <p class="hint">If printed page 1 is page 15 of the PDF, the offset is 14. Sets use printed page numbers.</p>
            <div class="row" style="margin-top:8px"><button class="small" data-practice="${b.id}">Practise any pages</button><button class="small danger" data-delbook="${b.id}">Remove PDF</button></div>
            <hr>
            <div class="spread"><h3 style="margin:0">Set queue</h3>${open.length ? `<button class="small danger" data-clear="${b.id}">Clear open sets</button>` : ''}</div>
            <p class="muted">"Textbook — next exercise" tasks work through these in order, one set per day${cap ? `, max ${cap} min` : ''}.</p>
            ${b.sets.length ? `<div class="setlist">${b.sets.map((x, i) => `
              <details class="setrow ${x.done ? 'done' : ''}" ${!x.done && x === open[0] ? 'open' : ''}>
                <summary><span>${x.done ? '✓' : `#${i + 1}`}</span> <b>${esc(x.title)}</b> <small>~${x.est_min} min${cap && x.est_min > cap ? ' ⚠️ over the daily limit' : ''}</small></summary>
                <ul>${x.items.map((it) => `<li>${it.review ? '🔁 ' : ''}${esc(it.label)} <small>p. ${it.page_from}${it.page_to !== it.page_from ? '–' + it.page_to : ''} · ${it.minutes} min</small></li>`).join('')}</ul>
                <div class="row">${x.done ? `<button class="small ghost" data-setreset="${x.id}">Redo</button>` : `<button class="small ghost" data-setpractice="${x.id}" data-book="${b.id}">Practise now</button>`}<button class="small danger" data-setdel="${x.id}">Delete</button></div>
              </details>`).join('')}</div>` : '<p class="muted">Empty. Import a sets file or add lines below.</p>'}
            <hr>
            <h3>Import a sets file</h3>
            <p class="muted">A <code>.json</code> sets file (like the one Claude builds from your PDF).</p>
            <input type="file" accept=".json,application/json" data-import="${b.id}">
            <label class="inline" style="margin-top:8px"><input type="checkbox" data-replace="${b.id}" checked> Replace the open (not yet done) sets</label>
            <div><button class="small" data-doimport="${b.id}" style="margin-top:8px">Import</button></div>
            <details style="margin-top:14px"><summary>…or type sets by hand</summary>
              <p class="hint">One set per line: <code>label; pages; minutes</code>, e.g. <code>H5 Oefening 3; 84-85; 20</code></p>
              <textarea data-lines="${b.id}" style="min-height:100px" placeholder="H5 Oefening 1; 84; 15&#10;H5 Oefening 2; 84-85; 20"></textarea>
              <button class="small" data-addlines="${b.id}" style="margin-top:6px">Add to queue</button>
            </details>
          </div>`;
        }).join('') || '<div class="card empty">No textbook yet. Upload your PDF.</div>'}
      </div>
      <div>
        <div class="card">
          <h2>Upload a textbook PDF</h2>
          <p class="muted">It's stored in Poortwachter's private folder on this computer. Only the pages of a set you submit are sent to Claude for grading.</p>
          <label for="up-name">Name</label><input type="text" id="up-name" placeholder="e.g. Home in on Holland">
          <label for="up-file">PDF file</label><input type="file" id="up-file" accept="application/pdf">
          <button id="up" style="margin-top:12px">Upload</button>
          <div id="up-msg" class="hint"></div>
        </div>
        <div class="card"><h2>Recent grading</h2><div id="subs" class="muted">Laden…</div></div>
      </div>
    </div>`;
  const findBook = (id) => books.find((x) => x.id === +id);
  $$('[data-act]').forEach((b) => (b.onclick = () => guard(async () => { await post(`/api/textbooks/${b.dataset.act}/activate`); viewTextbook(); }, b)));
  $$('[data-saveoff]').forEach((b) => (b.onclick = () => guard(async () => { await post(`/api/textbooks/${b.dataset.saveoff}/offset`, { offset: +$(`[data-off="${b.dataset.saveoff}"]`).value }); toast('Offset saved.'); }, b)));
  $$('[data-delbook]').forEach((b) => (b.onclick = () => guard(async () => { if (!confirm('Remove this PDF and its set queue from Poortwachter?')) return; await post(`/api/textbooks/${b.dataset.delbook}/delete`); viewTextbook(); }, b)));
  $$('[data-clear]').forEach((b) => (b.onclick = () => guard(async () => { if (!confirm('Delete all sets that aren\'t done yet?')) return; await post(`/api/textbooks/${b.dataset.clear}/sets/clear`); viewTextbook(); }, b)));
  $$('[data-setreset]').forEach((b) => (b.onclick = () => guard(async () => { await post(`/api/sets/${b.dataset.setreset}/reset`); viewTextbook(); }, b)));
  $$('[data-setdel]').forEach((b) => (b.onclick = () => guard(async () => { await post(`/api/sets/${b.dataset.setdel}/delete`); viewTextbook(); }, b)));
  $$('[data-setpractice]').forEach((b) => (b.onclick = () => {
    const bk = findBook(b.dataset.book);
    studyView({ book: bk, practice: true, set: bk.sets.find((x) => x.id === +b.dataset.setpractice) });
  }));
  $$('[data-practice]').forEach((b) => (b.onclick = () => studyView({ book: findBook(b.dataset.practice), practice: true })));
  $$('[data-addlines]').forEach((b) => (b.onclick = () => guard(async () => {
    const r = await post(`/api/textbooks/${b.dataset.addlines}/sets`, { text: $(`[data-lines="${b.dataset.addlines}"]`).value });
    toast(`${r.added} added${r.bad.length ? ` · couldn't read: ${r.bad.join(' | ')}` : ''}`, r.bad.length > 0);
    viewTextbook();
  }, b)));
  $$('[data-doimport]').forEach((b) => (b.onclick = () => guard(async () => {
    const f = $(`[data-import="${b.dataset.doimport}"]`).files[0];
    if (!f) throw new Error('Choose a sets file first.');
    const r = await post(`/api/textbooks/${b.dataset.doimport}/sets/import`, { data: await f.text(), replace: $(`[data-replace="${b.dataset.doimport}"]`).checked });
    toast(`${r.added} sets imported${r.over_cap ? ` · ${r.over_cap} over the daily limit` : ''}.`, r.over_cap > 0);
    viewTextbook();
  }, b)));
  $('#up').onclick = (e) => guard(async () => {
    const f = $('#up-file').files[0];
    if (!f) throw new Error('Choose a PDF first.');
    $('#up-msg').textContent = `Uploading ${(f.size / 1048576).toFixed(1)} MB…`;
    await api('/api/textbooks', { method: 'POST', raw: f, headers: { 'Content-Type': 'application/pdf', 'X-Name': encodeURIComponent($('#up-name').value || f.name.replace(/\.pdf$/i, '')) } });
    toast('Textbook added.'); viewTextbook();
  }, e.target);
  const subs = await api('/api/submissions');
  $('#subs').innerHTML = subs.length ? subs.slice(0, 12).map((x) => `
    <details style="margin-bottom:8px"><summary>${esc(x.label)} · ${x.result.score ? `${x.result.score.correct}/${x.result.score.total}` : ''} <small>${new Date(x.ts * 1000).toLocaleDateString()}</small></summary>
    ${renderResult(x.result)}</details>`).join('') : 'Nothing graded yet.';
}

// ------------------------------------------------------------------ HISTORY

async function viewHistory() {
  await loadState();
  const [recs, subs, events] = await Promise.all([api('/api/recordings'), api('/api/submissions'), api('/api/events')]);
  const H = S.state.history;
  $('#view').innerHTML = `
    <div class="section-title"><h1>History</h1><span class="muted">Emergency keys used in the last 30 days: <b>${S.state.emergency_30d}</b></span></div>
    <div class="card"><h2>Last 3 weeks</h2>
      <div class="heat">${H.map((h) => `<div class="${h.free ? 'free' : h.complete ? 'full' : h.done ? 'part' : ''} ${h.emergency ? 'em' : ''}" title="${h.emergency ? 'emergency key used' : h.free ? 'free day' : ''}">
        <b>${h.free ? '🏖️' : `${h.done}/${h.total}`}</b>${esc(dayNl(h.day).slice(0, 2))} ${esc(h.day.slice(5))}</div>`).join('')}</div>
      <p class="hint">Green = all done · orange = partly · blue 🏖️ = free day · dashed red = emergency key.</p></div>
    <div class="grid2">
      <div class="card"><h2>🗣️ Recordings</h2>
        ${recs.length ? recs.map((r) => `<div style="margin-bottom:14px"><div class="spread"><b lang="nl" style="font-weight:600">${esc(r.prompt)}</b><small>${new Date(r.ts * 1000).toLocaleDateString()} · ${Math.round(r.seconds)} s</small></div>
          <audio controls preload="none" src="/files/recording/${esc(r.file)}" style="width:100%"></audio></div>`).join('') : '<p class="muted">No recordings yet.</p>'}
        <p class="hint">Listening back a month later is the best way to hear your progress.</p></div>
      <div class="card"><h2>📘 Graded exercises</h2>
        ${subs.length ? subs.map((s) => `<details style="margin-bottom:8px"><summary>${esc(s.label)} · ${s.result.score ? `${s.result.score.correct}/${s.result.score.total}` : ''} <small>${new Date(s.ts * 1000).toLocaleDateString()}</small></summary>
          <h3 style="margin-top:10px">Your answers</h3><pre style="white-space:pre-wrap;font-family:inherit" lang="nl">${esc(s.answers)}</pre>${renderResult(s.result)}</details>`).join('') : '<p class="muted">Nothing graded yet.</p>'}</div>
    </div>
    <div class="card"><h2>Log</h2><table><tbody>${events.filter((e) => ['emergency', 'blocked', 'unlocked', 'carryover', 'settings', 'service', 'watchdog', 'install', 'free-day', 'session'].includes(e.type)).slice(0, 60).map((e) => `
      <tr><td style="white-space:nowrap"><small>${new Date(e.ts * 1000).toLocaleString()}</small></td><td><b>${esc(e.type)}</b></td><td>${esc(e.detail)}</td></tr>`).join('')}</tbody></table></div>`;
}

// ------------------------------------------------------------------ SETTINGS

async function viewSettings() {
  await Promise.all([loadSettings(), loadActivities(), loadState()]);
  const s = S.settings;
  const caps = s.category_caps || {};
  $('#view').innerHTML = `
    <div class="section-title"><h1>Settings</h1></div>
    <div class="grid2">
      <div>
        <div class="card">
          <h2>Claude (textbook grading)</h2>
          <p class="muted">Uses your own Anthropic API key. It's stored in Poortwachter's root-only folder and never sent to the browser.</p>
          <label for="c-key">API key ${s.claude_key_set ? '<span class="chip bonus">saved</span>' : ''}</label>
          <input type="password" id="c-key" placeholder="${s.claude_key_set ? '•••••••• (leave empty to keep)' : 'sk-ant-…'}" autocomplete="off">
          <label for="c-model">Model</label><input type="text" id="c-model" value="${esc(s.claude_model)}">
          <p class="hint">Default <code>claude-sonnet-5-5</code>: accurate Dutch at a sensible price. <code>claude-opus-5-5</code> is stronger, <code>claude-haiku-5-5</code> cheaper.</p>
          <div class="row"><button id="c-save">Save</button>${s.claude_key_set ? '<button class="danger" id="c-clear">Remove key</button>' : ''}</div>
        </div>
        <div class="card">
          <h2>Anki</h2>
          <p class="muted">Poortwachter copies your collection and reads the copy, so it never touches Anki's own files.</p>
          <label for="a-path">Collection file</label>
          <select id="a-pick"><option value="">Auto-detect (most recently used profile)</option>${s.anki_detected.map((p) => `<option ${p === s.anki_collection ? 'selected' : ''}>${esc(p)}</option>`).join('')}<option value="__custom">Custom path…</option></select>
          <input type="text" id="a-path" value="${esc(s.anki_collection)}" placeholder="/home/you/.local/share/Anki2/User 1/collection.anki2" style="margin-top:6px" ${s.anki_detected.includes(s.anki_collection) || !s.anki_collection ? 'hidden' : ''}>
          <p class="hint">In use now: ${s.anki_in_use ? `<code>${esc(s.anki_in_use)}</code>` : '<span class="err">none found</span>'}</p>
          <div class="row"><button id="a-save">Save</button><button class="ghost" id="a-test">Show decks</button></div>
          <div id="a-out" class="hint"></div>
        </div>
        <div class="card">
          <h2>Speaking prompts</h2>
          <p class="muted">One per line. "Speaking — answer a prompt" picks one at random.</p>
          <textarea id="sp" lang="nl" style="min-height:220px">${esc(s.speaking_prompts.join('\n'))}</textarea>
          <button id="sp-save" style="margin-top:8px">Save prompts</button>
        </div>
      </div>
      <div>
        <div class="card">
          <h2>Boekenwurm</h2>
          ${s.bw.signed_in ? `<p class="ok">✓ Signed in as ${esc(s.bw.email)}</p>` : '<p class="muted">Not connected.</p>'}
          <label for="b-url">Supabase project URL</label><input type="url" id="b-url" value="${esc(s.bw_url)}" placeholder="https://xxxx.supabase.co">
          <label for="b-key">Publishable (anon) key ${s.bw_key_set ? '<span class="chip bonus">saved</span>' : ''}</label>
          <input type="password" id="b-key" placeholder="${s.bw_key_set ? '•••••••• (leave empty to keep)' : 'sb_publishable_… or eyJ…'}">
          <p class="hint">The same two values as <code>VITE_SUPABASE_URL</code> / <code>VITE_SUPABASE_PUBLISHABLE_KEY</code> in Boekenwurm (public by design; row-level security protects the data).</p>
          <button id="b-save" class="ghost">Save connection</button>
          <hr>
          <label for="b-email">Your Boekenwurm email</label>
          <div class="row"><input type="email" id="b-email" style="flex:1" value="${esc(s.bw.email)}"><button id="b-login">Email me a sign-in link</button></div>
          <p class="hint"><b>One-time setup:</b> in Supabase → Authentication → URL Configuration, add <code>${esc(s.bw.callback)}</code> to Redirect URLs. Then open the link from the email <i>on this computer</i>.</p>
          <div class="row">${s.bw.signed_in ? '<button class="ghost" id="b-test">Test: today\'s reading</button><button class="danger" id="b-out">Sign out</button>' : ''}</div>
          <div id="b-msg" class="hint"></div>
          <label for="b-ppp">EPUB paragraphs per "page"</label><input type="number" id="b-ppp" min="1" max="20" value="${s.bw_paragraphs_per_page}" style="max-width:120px">
          <p class="hint">Photographed and PDF books count real printed pages. EPUBs have no page numbers, so this many paragraphs = 1 page.</p>
          <button class="ghost small" id="b-ppp-save">Save</button>
        </div>
        <div class="card">
          <h2>Daily limits &amp; planning</h2>
          <p class="muted">The most required minutes per category on one day. The random draw and the planner never go over; bonus tasks are never limited. Leave empty for no limit.</p>
          <div class="caps">${S.state.categories.filter(([k]) => k !== 'other').map(([k, v]) => `
            <label>${CAT_ICON[k]} ${esc(v)}<input type="number" min="5" max="600" step="5" data-cap="${k}" value="${caps[k] ?? ''}" placeholder="no limit"></label>`).join('')}</div>
          <label for="evmin">On a day where a session fills the whole budget, still require:</label>
          <select id="evmin"><option value="">Nothing else</option>${S.activities.filter((a) => a.enabled).map((a) => `<option value="${a.id}" ${a.id === s.event_minimum_activity ? 'selected' : ''}>${esc(a.name)} (~${a.est_min} min)</option>`).join('')}</select>
          <label for="ahead">Plan this many days ahead</label>
          <input type="number" id="ahead" min="1" max="14" value="${s.plan_days_ahead}" style="max-width:120px">
          <div style="margin-top:12px"><button id="limits-save">Save</button></div>
          <p class="hint">Changes apply from tomorrow; days you've edited by hand keep your edits.</p>
        </div>
        <div class="card">
          <h2>The lock</h2>
          <p>Watching desktop user: ${s.watched_user ? `<code>${esc(s.watched_user)}</code>` : '<span class="err">none. Run the installer.</span>'}</p>
          <p class="muted">Installed: ${esc(s.installed_day || '?')} (that day was free).</p>
          <p class="muted">The lock itself can't be switched off from this page on purpose. In a real emergency, open a terminal and run:</p>
          <p><code>sudo poortwachter noodsleutel</code></p>
          <p class="muted">You type a commitment sentence and wait 90 seconds, then Steam opens until 02:00. If you have a banked free day it uses that, so your chain stays intact; otherwise the day counts as missed. It's logged in History.</p>
          <label for="minfill">Smallest gap the random draw fills (minutes)</label>
          <div class="row"><input type="number" id="minfill" min="1" max="60" value="${s.min_fill_minutes}" style="max-width:120px"><button class="ghost small" id="minfill-save">Save</button></div>
        </div>
      </div>
    </div>`;

  const save = (body, msg = 'Saved.') => post('/api/settings', body).then(() => toast(msg));
  $('#c-save').onclick = (e) => guard(async () => { await save({ claude_api_key: $('#c-key').value.trim(), claude_model: $('#c-model').value.trim() }); viewSettings(); }, e.target);
  if ($('#c-clear')) $('#c-clear').onclick = (e) => guard(async () => { await post('/api/settings/clear_claude_key'); viewSettings(); }, e.target);
  $('#a-pick').onchange = () => { const v = $('#a-pick').value; $('#a-path').hidden = v !== '__custom'; if (v !== '__custom') $('#a-path').value = v; };
  $('#a-save').onclick = (e) => guard(async () => { await save({ anki_collection: $('#a-path').value.trim() }); viewSettings(); }, e.target);
  $('#a-test').onclick = (e) => guard(async () => { const d = await api('/api/anki/decks'); $('#a-out').innerHTML = d.length ? 'Decks: ' + d.map(esc).join(' · ') : 'No decks found.'; }, e.target);
  $('#sp-save').onclick = (e) => guard(() => save({ speaking_prompts: $('#sp').value.split('\n') }), e.target);
  $('#b-save').onclick = (e) => guard(async () => { await save({ bw_url: $('#b-url').value.trim(), bw_key: $('#b-key').value.trim() }); viewSettings(); }, e.target);
  $('#b-login').onclick = (e) => guard(async () => { await post('/api/bw/login', { email: $('#b-email').value.trim() }); $('#b-msg').innerHTML = '<span class="ok">Sent! Open the link in the email on this computer.</span>'; }, e.target);
  if ($('#b-out')) $('#b-out').onclick = (e) => guard(async () => { await post('/api/bw/logout'); viewSettings(); }, e.target);
  if ($('#b-test')) $('#b-test').onclick = (e) => guard(async () => { const r = await api('/api/bw/test'); $('#b-msg').textContent = `Today: ${r.reading.pages} pages · ${r.reading.paragraphs} paragraphs · ${r.reading.minutes} min · ${r.reading.sessions} sessions · ${r.cards} cards reviewed`; }, e.target);
  $('#b-ppp-save').onclick = (e) => guard(() => save({ bw_paragraphs_per_page: +$('#b-ppp').value }), e.target);
  $('#limits-save').onclick = (e) => guard(async () => {
    const category_caps = {};
    $$('[data-cap]').forEach((i) => { if (i.value) category_caps[i.dataset.cap] = +i.value; });
    await save({ category_caps, event_minimum_activity: $('#evmin').value ? +$('#evmin').value : null, plan_days_ahead: +$('#ahead').value });
  }, e.target);
  $('#minfill-save').onclick = (e) => guard(() => save({ min_fill_minutes: +$('#minfill').value }), e.target);
}

// ------------------------------------------------------------------ boot

window.closePanel = closePanel;
route();
setInterval(() => { if (S.view === 'today' && $('#panel-wrap').hidden) viewToday().catch(() => {}); else loadState().catch(() => {}); }, 60000);
