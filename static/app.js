/* PinClicks Mining Tool - UI controller.
 * No framework, no build step. Screens are sections toggled by the nav. */

const $  = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

const state = {
  runId: null,
  settings: null,
  logCursor: 0,
  depth: 40,
};

/* ------------------------------------------------------------------ api */

async function api(path, options = {}) {
  const res = await fetch(`/api${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail ?? detail; } catch { /* non-JSON body */ }
    // 501 is how the backend reports "this milestone isn't built yet".
    throw new Error(detail);
  }
  return res.status === 204 ? null : res.json();
}

function banner(message, kind = 'info') {
  const el = $('#global-banner');
  if (!message) { el.className = 'banner'; return; }
  el.textContent = message;
  el.className = `banner show ${kind}`;
}

/* --------------------------------------------------------------- nav */

/* Each step gets a title and a one-line description of what it is for, so the
   top of the screen always answers "where am I and why". */
const SCREEN_INFO = {
  s1: ['Session', 'Sign in to PinClicks once; the app remembers it.'],
  s2: ['Research', 'Niche to sub-niche to keyword, without leaving the page.'],
  s4: ['Scraping', 'Live progress for the keywords you selected.'],
  s5: ['Review & export', 'Check what the filters kept, then download the CSV.'],
  s3: ['Manual search', 'Look up a keyword directly, skipping the niche tree.'],
  s6: ['Settings', 'API keys for the off-niche filter.'],
};

/* Where you were is part of your work. Refreshing should return you to the
   screen you were using, not throw you back to the start. */
const LAST_SCREEN = 'pinclicks.screen';

function showScreen(id) {
  $$('nav button').forEach((b) => b.classList.toggle('active', b.dataset.screen === id));
  $$('.screen').forEach((s) => s.classList.toggle('active', s.id === id));
  const [title, sub] = SCREEN_INFO[id] || ['', ''];
  $('#page-title').textContent = title;
  $('#page-sub').textContent = sub;
  try { localStorage.setItem(LAST_SCREEN, id); } catch { /* private mode */ }
}

$$('nav button').forEach((btn) => {
  btn.addEventListener('click', () => {
    showScreen(btn.dataset.screen);
    // Key states change over time (resting keys wake up), so the list is
    // rebuilt on entry rather than cached.
    if (btn.dataset.screen === 's6' && typeof renderKeys === 'function') renderKeys();
  });
});

/* ---------------------------------------------------------- badges */

function setSessionBadge(status) {
  const label = { active: 'Session active', expired: 'Session expired', unknown: 'Unknown' }[status]
    ?? 'Unknown';
  for (const sel of ['#session-badge', '#session-badge-lg']) {
    const el = $(sel);
    el.className = `badge ${status}`;
    el.textContent = label;
  }
}



/* ------------------------------------------------------------ startup */

async function boot() {
  try {
    const [health, settings] = await Promise.all([api('/health'), api('/settings')]);
    state.settings = settings;
    state.runId = health.current_run_id;

    $('#sites-input').value = (settings.sites || []).join('\n');

    state.depth = settings.pins_per_keyword_default ?? 40;
    const slider = $('#depth-slider');
    slider.min = settings.pins_per_keyword_min ?? 10;
    slider.max = settings.pins_per_keyword_max ?? 100;
    slider.value = state.depth;
    $('#depth-value').textContent = state.depth;

    $('#run-label').textContent = state.runId ? `run #${state.runId}` : 'no active run';

    await refreshSession();
    await loadPresets();

    // Put the workspace back exactly as it was: same tab, same niche
    // drill-down, same ticked keywords.
    restoreBoard();
    try {
      const last = localStorage.getItem(LAST_SCREEN);
      if (last && SCREEN_INFO[last]) showScreen(last);
    } catch { /* private mode */ }

    await loadResults();
  } catch (err) {
    banner(`Failed to start: ${err.message}`, 'error');
  }
}

async function refreshSession() {
  try {
    const { status } = await api('/session/status');
    setSessionBadge(status);
  } catch {
    // M2 endpoint not present yet.
    setSessionBadge('unknown');
  }
}

/* --------------------------------------------------- screen 1 handlers */

$('#btn-check-session').addEventListener('click', async () => {
  banner('Checking session…', 'info');
  try {
    const { status, reason } = await api('/session/probe', { method: 'POST' });
    setSessionBadge(status);
    banner(`Session ${status} — ${reason}`, status === 'active' ? 'info' : 'warn');
  } catch (err) {
    banner(`Session check failed: ${err.message}`, 'error');
  }
});

$('#btn-login').addEventListener('click', async (e) => {
  const btn = e.currentTarget;
  btn.disabled = true;
  banner('Opening a Chrome window — log in to PinClicks there. '
       + 'This page will detect it automatically.', 'info');
  try {
    await api('/session/login', { method: 'POST' });
    await waitForLogin();
  } catch (err) {
    banner(`Could not open the login window: ${err.message}`, 'error');
  } finally {
    btn.disabled = false;
  }
});

/* --------------------------------------------------- clear all history */

$('#btn-reset').addEventListener('click', async (e) => {
  const ok = confirm('Delete ALL niches, keywords, pins, filter results and '
                   + 'overrides?\n\nYour login, sites and API keys are kept. '
                   + 'This cannot be undone.');
  if (!ok) return;
  const btn = e.currentTarget;
  btn.disabled = true;
  try {
    const r = await api('/reset', { method: 'POST' });
    try {
      localStorage.removeItem(BOARD_KEY);
      localStorage.removeItem(LAST_SCREEN);
    } catch { /* private mode */ }
    $('#reset-status').textContent =
      `Cleared ${r.pins} pins, ${r.keywords} keywords, ${r.runs} runs.`;
    banner('History cleared. Reloading…', 'info');
    setTimeout(() => location.reload(), 800);
  } catch (err) {
    banner(`Could not clear history: ${err.message}`, 'error');
    btn.disabled = false;
  }
});

/* Wait for login to complete.
 *
 * Uses /session/observe, which READS the page. It must never use
 * /session/probe, which NAVIGATES — polling that while the user is typing
 * reloads the login form out from under them and makes login impossible. */
async function waitForLogin({ attempts = 150, everyMs = 2000 } = {}) {
  for (let i = 0; i < attempts; i++) {
    await new Promise((r) => setTimeout(r, everyMs));
    let result;
    try {
      result = await api('/session/observe');
    } catch {
      continue;  // browser not ready yet
    }
    if (result.blocked) {
      banner(result.reason, 'error');
      return false;
    }
    if (result.status === 'active') {
      setSessionBadge('active');
      banner('Logged in. Now click "Calibrate against live site".', 'info');
      return true;
    }
  }
  banner('Stopped waiting for login. Click Re-check when you have finished.', 'warn');
  return false;
}





$('#btn-save-sites').addEventListener('click', async () => {
  const sites = $('#sites-input').value
    .split('\n').map((s) => s.trim()).filter(Boolean);
  try {
    state.settings = await api('/settings', {
      method: 'PATCH',
      body: JSON.stringify({ sites }),
    });
    banner(`Saved ${sites.length} site column(s).`, 'info');
  } catch (err) {
    banner(err.message, 'error');
  }
});

/* --------------------------------------------------- screen 3 handlers */

$('#depth-slider').addEventListener('input', (e) => {
  state.depth = Number(e.target.value);
  $('#depth-value').textContent = state.depth;
});

async function loadPresets() {
  try {
    const presets = await api('/presets');
    const sel = $('#preset-select');
    sel.innerHTML = '<option value="">— none —</option>';
    for (const p of presets) {
      const opt = document.createElement('option');
      opt.value = p.name;
      opt.textContent = p.name;
      opt.dataset.payload = JSON.stringify(p.payload);
      sel.appendChild(opt);
    }
  } catch { /* presets are optional */ }
}

$('#btn-load-preset').addEventListener('click', () => {
  const opt = $('#preset-select').selectedOptions[0];
  if (!opt?.dataset.payload) return;
  const p = JSON.parse(opt.dataset.payload);
  $('#f-min').value = p.min ?? 0;
  $('#f-max').value = p.max ?? '';
  $('#f-negative').value = (p.negative || []).join(', ');
});

$('#btn-save-preset').addEventListener('click', async () => {
  const name = prompt('Preset name?');
  if (!name) return;
  const payload = {
    min: Number($('#f-min').value) || 0,
    max: $('#f-max').value ? Number($('#f-max').value) : null,
    negative: $('#f-negative').value.split(',').map((s) => s.trim()).filter(Boolean),
  };
  await api('/presets', { method: 'POST', body: JSON.stringify({ name, payload }) });
  await loadPresets();
  banner(`Preset "${name}" saved.`, 'info');
});

/* -------------------------------------------------------- run log poll */

async function pollLog() {
  if (state.runId) {
    try {
      const { entries, last_id } = await api(`/runs/${state.runId}/log?after_id=${state.logCursor}`);
      state.logCursor = last_id;
      const box = $('#run-log');
      for (const e of entries) {
        const div = document.createElement('div');
        div.className = e.level;
        const time = document.createElement('time');
        time.textContent = new Date(e.ts * 1000).toLocaleTimeString();
        div.append(time, document.createTextNode(e.message));
        box.appendChild(div);
      }
      if (entries.length) box.scrollTop = box.scrollHeight;
    } catch { /* run may not exist yet */ }
  }
  setTimeout(pollLog, 2000);
}

/* ------------------------------------------------- helpers */

state.niches = [];
state.keywords = [];
state.pins = [];

function escapeHtml(str) {
  const d = document.createElement('div');
  d.textContent = str ?? '';
  return d.innerHTML;
}

function gotoScreen(id) { showScreen(id); }

/* ------------------------------------------------- L1: niches */

/* ------------------------------------------------- research board
 *
 * Three columns side by side: Niches | Sub-niches | Keywords.
 * The whole point is that researching never leaves this screen, so nothing
 * here navigates away. Clicking a keyword opens its Top Pins in a NEW browser
 * tab instead.
 *
 * One row = one button. An earlier version split each row into "click the
 * arrow to go deeper, click the name to pick it", which was two competing
 * targets in one row. Now the row does the main thing, and the rare
 * go-deeper action is a small separate pill.
 */

const BOARD_KEY = 'pinclicks.board';

/* The niche you opened and the keywords you ticked are work too. Losing them
   on refresh meant starting the drill-down again every time. */
function saveBoard() {
  try {
    localStorage.setItem(BOARD_KEY, JSON.stringify({
      niches: state.board.niches,
      subs: state.board.subs,
      keywords: state.board.keywords,
      path: state.board.path,
      niche: state.board.niche,
      sub: state.board.sub,
    }));
  } catch { /* quota or private mode -- not worth failing over */ }
}

function restoreBoard() {
  try {
    const raw = localStorage.getItem(BOARD_KEY);
    if (!raw) return false;
    const b = JSON.parse(raw);
    Object.assign(state.board, b);
    if (state.board.niches.length) renderNiches();
    if (state.board.subs.length) renderSubs();
    if (state.board.keywords.length) renderKeywordCol();
    if (state.board.path.length) renderTrail(state.board.path);
    return true;
  } catch {
    return false;
  }
}

state.board = {
  niches: [],
  subs: [],
  keywords: [],
  path: [],          // [{id, name}] of opened niches, for the sub-niche column
  niche: null,       // selected column-1 row
  sub: null,         // selected column-2 row
};

function colEmpty(bodyId, text) {
  $(bodyId).innerHTML = '<div class="col-empty">' + escapeHtml(text) + '</div>';
}

function colBusy(bodyId, text) {
  $(bodyId).innerHTML = '<div class="loading-note">' + escapeHtml(text) + '</div>';
}

function setCount(id, n, cached) {
  const el = $(id);
  el.textContent = n ? String(n) : '';
  el.classList.toggle('is-cached', !!cached && !!n);
}

/* Rows carry a --share so the hairline under each one reads as its portion
   of the biggest value in the column. Uses a square root so the long tail
   stays visible instead of collapsing to nothing next to a 4M outlier. */
function shareScale(items) {
  const max = Math.max(1, ...items.map((i) => i.volume || 0));
  return (v) => Math.round(Math.sqrt((v || 0) / max) * 100) + '%';
}

/* Build one row button. `deeper` adds the small go-deeper pill. */
function rowButton(item, { active, onPick, onDeeper, share }) {
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'row-btn' + (active ? ' active' : '');
  if (share) btn.style.setProperty('--share', share);

  const name = document.createElement('span');
  name.className = 'name';
  name.textContent = item.name;
  btn.appendChild(name);

  const vol = document.createElement('span');
  vol.className = 'vol';
  vol.textContent = (item.volume || 0).toLocaleString();
  btn.appendChild(vol);

  if (onDeeper) {
    const pill = document.createElement('span');
    pill.className = 'chip';
    pill.textContent = 'open';
    pill.title = 'Open the sub-niches inside ' + item.name;
    pill.addEventListener('click', (e) => {
      e.stopPropagation();   // the row's own click must not also fire
      onDeeper();
    });
    btn.appendChild(pill);
  }

  btn.addEventListener('click', onPick);
  return btn;
}

function renderColumn(bodyId, items, opts) {
  const body = $(bodyId);
  body.innerHTML = '';
  if (opts.back) {
    const back = document.createElement('button');
    back.type = 'button';
    back.className = 'col-back';
    back.textContent = '‹ ' + opts.back.label;
    back.addEventListener('click', opts.back.onClick);
    body.appendChild(back);
  }
  if (!items.length) {
    body.appendChild(Object.assign(document.createElement('div'), {
      className: 'col-empty', textContent: opts.emptyText || 'Nothing here.',
    }));
    return;
  }
  const share = shareScale(items);
  items.forEach((item) => body.appendChild(
    rowButton(item, Object.assign({ share: share(item.volume) }, opts.rowOpts(item)))));
}

/* The trail is the board's "where am I" readout, so each level is its own
   element rather than one joined string -- the current level reads brighter
   than its ancestors. */
function renderTrail(path) {
  const el = $('#board-crumb');
  el.innerHTML = '';
  path.forEach((p, i) => {
    if (i) {
      const sep = document.createElement('span');
      sep.className = 'sep';
      sep.textContent = '/';
      el.appendChild(sep);
    }
    const seg = document.createElement('span');
    if (i === path.length - 1) seg.className = 'here';
    seg.textContent = p.name;
    el.appendChild(seg);
  });
}

/* ---------------- column 1: niches ---------------- */

function renderNiches() {
  saveBoard();
  setCount('#c1-count', state.board.niches.length, state.board.nichesCached);
  renderColumn('#c1-body', state.board.niches, {
    emptyText: 'No niches.',
    rowOpts: (n) => ({
      active: state.board.niche && state.board.niche.id === n.id,
      onPick: () => pickNiche(n),
    }),
  });
}

$('#btn-discover').addEventListener('click', async (e) => {
  const btn = e.currentTarget;
  btn.disabled = true;
  colBusy('#c1-body', 'Loading niches…');
  try {
    const result = await api('/niches' + (state.forceRefresh ? '?refresh=true' : ''));
    const niches = result.niches;
    state.board.niches = niches;
    state.board.nichesCached = result.cached;
    state.board.niche = null;
    state.board.subs = [];
    state.board.keywords = [];
    state.board.path = [];
    renderNiches();
    colEmpty('#c2-body', 'Pick a niche.');
    colEmpty('#c3-body', 'Pick a sub-niche.');
    $('#c2-count').textContent = '';
    $('#c3-count').textContent = '';
    $('#board-crumb').textContent = '';
    banner('', 'info');
  } catch (err) {
    colEmpty('#c1-body', 'Could not load niches.');
    banner('Could not load niches: ' + err.message, 'error');
  } finally {
    btn.disabled = false;
  }
});

/* ---------------- column 2: sub-niches ---------------- */

async function pickNiche(n) {
  state.board.niche = n;
  state.board.path = [{ id: n.id, name: n.name }];
  state.board.keywords = [];
  state.board.sub = null;
  renderNiches();
  colEmpty('#c3-body', 'Pick a sub-niche.');
  $('#c3-count').textContent = '';
  await loadSubs();
}

async function loadSubs(refresh) {
  const path = state.board.path;
  renderTrail(path);
  colBusy('#c2-body', refresh ? 'Re-scraping sub-niches…' : 'Loading sub-niches…');
  try {
    const result = await api('/niches/children', {
      method: 'POST',
      body: JSON.stringify({ path: path.map((p) => p.id), refresh: !!refresh }),
    });
    state.board.subs = result.niches;
    state.board.subsCached = result.cached;
    renderSubs();
  } catch (err) {
    colEmpty('#c2-body', 'Could not open that niche.');
    banner('Could not open that niche: ' + err.message, 'error');
  }
}

function renderSubs() {
  saveBoard();
  setCount('#c2-count', state.board.subs.length, state.board.subsCached);
  const path = state.board.path;
  const back = path.length > 1
    ? { label: 'Back to ' + path[path.length - 2].name,
        onClick: () => { state.board.path.pop(); loadSubs(); } }
    : null;

  renderColumn('#c2-body', state.board.subs, {
    back: back,
    emptyText: 'No sub-niches.',
    rowOpts: (s) => ({
      active: state.board.sub && state.board.sub.id === s.id,
      onPick: () => pickSub(s),
      // Only offered where it exists; the row itself always loads keywords.
      onDeeper: s.has_children
        ? () => { state.board.path.push({ id: s.id, name: s.name }); loadSubs(); }
        : null,
    }),
  });
}

/* ---------------- column 3: keywords ---------------- */

async function pickSub(s, refresh) {
  state.board.sub = s;
  renderSubs();
  colBusy('#c3-body', (refresh ? 'Re-scraping' : 'Loading')
        + ' keywords for "' + s.name + '"…');
  try {
    const result = await api('/keywords', {
      method: 'POST',
      body: JSON.stringify({
        search: s.name, min_volume: 0, negative_words: [], refresh: !!refresh,
      }),
    });
    state.board.keywords = result.keywords.map((k) => ({
      name: k.keyword, volume: k.volume,
    }));
    state.board.keywordsCached = result.cached;
    renderKeywordCol();
  } catch (err) {
    colEmpty('#c3-body', 'Could not load keywords.');
    banner('Could not load keywords: ' + err.message, 'error');
  }
}

function renderKeywordCol() {
  saveBoard();
  setCount('#c3-count', state.board.keywords.length, state.board.keywordsCached);

  const body = $('#c3-body');
  body.innerHTML = '';
  if (!state.board.keywords.length) {
    colEmpty('#c3-body', 'No keywords.');
    updateSelCount();
    return;
  }

  const share = shareScale(state.board.keywords);
  state.board.keywords.forEach((k, i) => {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'row-btn' + (k.selected ? ' picked' : '');
    btn.style.setProperty('--share', share(k.volume));

    const box = document.createElement('input');
    box.type = 'checkbox';
    box.className = 'pick-box';
    box.checked = !!k.selected;
    // The row itself toggles; the box is a visual echo you can also click.
    box.addEventListener('click', (e) => e.stopPropagation());
    box.addEventListener('change', () => toggleKeyword(i, box.checked));
    btn.appendChild(box);

    const name = document.createElement('span');
    name.className = 'name';
    name.textContent = k.name;
    btn.appendChild(name);

    const vol = document.createElement('span');
    vol.className = 'vol';
    vol.textContent = (k.volume || 0).toLocaleString();
    btn.appendChild(vol);

    // Viewing one keyword's pins is now the secondary action; bulk scraping
    // the selection is the primary one.
    const open = document.createElement('span');
    open.className = 'chip';
    open.textContent = 'pins';
    open.title = 'Open this keyword’s Top Pins in a new browser tab';
    open.addEventListener('click', (e) => { e.stopPropagation(); openPins(k.name); });
    btn.appendChild(open);

    btn.addEventListener('click', () => toggleKeyword(i, !k.selected));
    body.appendChild(btn);
  });
  updateSelCount();
}

function toggleKeyword(index, on) {
  state.board.keywords[index].selected = on;
  renderKeywordCol();
}

function selectedKeywordNames() {
  return state.board.keywords.filter((k) => k.selected).map((k) => k.name);
}

function updateSelCount() {
  const n = selectedKeywordNames().length;
  $('#kw-selected').textContent = n ? n + ' selected' : '0 selected';
  $('#btn-scrape-selected').disabled = n === 0;
}

/* "All" respects the min-volume box, so you can tick the whole worthwhile
   half of a 100-keyword list in one action instead of clicking 50 rows. */
$('#kw-all').addEventListener('change', (e) => {
  const min = Number($('#kw-min').value || 0);
  state.board.keywords.forEach((k) => {
    k.selected = e.target.checked && (k.volume || 0) >= min;
  });
  renderKeywordCol();
});

$('#kw-min').addEventListener('input', () => {
  if ($('#kw-all').checked) $('#kw-all').dispatchEvent(new Event('change'));
});

/* Ask before discarding collected work.
 *
 * A scrape used to silently replace everything already gathered, so a second
 * session wiped the first and you only found out afterwards. */
async function chooseAppendMode() {
  let existing;
  try {
    existing = await api('/scrape/existing');
  } catch {
    return { append: false };
  }
  if (!existing.count) return { append: false };

  const list = existing.keywords.slice(0, 4).join(', ')
    + (existing.keywords.length > 4
        ? ' and ' + (existing.keywords.length - 4) + ' more' : '');

  const keep = window.confirm(
    'You already have ' + existing.count + ' pins from: ' + list + '.\n\n'
    + 'OK  →  ADD the new pins to these\n'
    + 'Cancel  →  START FRESH (the current pins are cleared)'
  );
  return { append: keep };
}

$('#btn-scrape-selected').addEventListener('click', async () => {
  const keywords = selectedKeywordNames();
  if (!keywords.length) { banner('Select some keywords first.', 'warn'); return; }
  const mode = await chooseAppendMode();
  try {
    await api('/scrape', {
      method: 'POST',
      body: JSON.stringify({
        keywords: keywords,
        pins_per_keyword: Number($('#kw-depth').value) || state.depth,
        niche: state.board.sub ? state.board.sub.name : '',
        refresh: !!state.forceRescrape,
        append: mode.append,
      }),
    });
    gotoScreen('s4');
    if (mode.append) banner('Adding to your existing pins…', 'info');
    const depth = Number($('#kw-depth').value) || state.depth;
    banner('Scraping up to ' + depth + ' pins for ' + keywords.length
         + ' keyword(s)…' + (depth > 25
             ? ' Above ~25 PinClicks fetches live from Pinterest, which is slow.'
             : ''), 'info');
    pollScrape();
  } catch (err) {
    banner('Could not start: ' + err.message, 'error');
  }
});

async function openPins(keyword) {
  banner('Opening Top Pins for "' + keyword + '" in a new tab…', 'info');
  try {
    await api('/open-pins', {
      method: 'POST',
      body: JSON.stringify({ keyword: keyword }),
    });
    banner('Opened Top Pins for "' + keyword + '" in a new browser tab.', 'info');
  } catch (err) {
    banner('Could not open Top Pins: ' + err.message, 'error');
  }
}

/* ------------------------------------------------- L3: keywords */

function currentFilters() {
  const max = $('#f-max').value.trim();
  return {
    min_volume: Number($('#f-min').value || 0),
    max_volume: max ? Number(max) : null,
    negative_words: $('#f-negative').value.split(',').map((s) => s.trim()).filter(Boolean),
  };
}

function selectedKeywords() {
  return $$('#l2-table tbody input[type=checkbox]:checked')
    .map((cb) => state.keywords[Number(cb.dataset.i)].keyword);
}

function updateKeywordCount() {
  $('#l2-count').textContent = selectedKeywords().length + ' selected of ' + state.keywords.length;
}

function renderKeywords() {
  const tbody = $('#l2-table tbody');
  if (!state.keywords.length) {
    tbody.innerHTML = '<tr><td colspan="4" class="empty">No keywords yet.</td></tr>';
    $('#l2-count').textContent = '';
    return;
  }
  tbody.innerHTML = '';
  state.keywords.forEach((k, i) => {
    const tr = document.createElement('tr');
    if (!k.included) tr.style.opacity = '0.45';
    const status = k.included
      ? '<span class="badge active">included</span>'
      : '<span class="badge warning">' + escapeHtml(k.excluded_by) + '</span>';
    tr.innerHTML =
      '<td><input type="checkbox" data-i="' + i + '"' + (k.included ? ' checked' : '') + '></td>'
      + '<td>' + escapeHtml(k.keyword) + '</td>'
      + '<td class="num">' + (k.volume || 0).toLocaleString() + '</td>'
      + '<td>' + status + '</td>';
    tbody.appendChild(tr);
  });
  tbody.querySelectorAll('input[type=checkbox]').forEach((cb) => {
    cb.addEventListener('change', updateKeywordCount);
  });
  updateKeywordCount();
}

async function fetchKeywords() {
  const search = $('#l2-main').value.trim();
  if (!search) { banner('Enter a main keyword first.', 'warn'); return; }
  banner('Loading keywords for "' + search + '"...', 'info');
  try {
    const body = Object.assign({ search: search }, currentFilters());
    const result = await api('/keywords', { method: 'POST', body: JSON.stringify(body) });
    state.keywords = result.keywords;
    renderKeywords();
    banner(result.total + ' keywords, ' + result.included + ' passed your filters.', 'info');
  } catch (err) {
    banner('Could not load keywords: ' + err.message, 'error');
  }
}

$('#btn-get-keywords').addEventListener('click', fetchKeywords);
$('#btn-apply-filters').addEventListener('click', fetchKeywords);
$('#l2-main').addEventListener('keydown', (e) => { if (e.key === 'Enter') fetchKeywords(); });

$('#l2-check-all').addEventListener('change', (e) => {
  $$('#l2-table tbody input[type=checkbox]').forEach((cb) => { cb.checked = e.target.checked; });
  updateKeywordCount();
});

/* ------------------------------------------------- L4: scraping */

$('#btn-scrape').addEventListener('click', async () => {
  const keywords = selectedKeywords();
  if (!keywords.length) { banner('Select at least one keyword.', 'warn'); return; }
  try {
    await api('/scrape', {
      method: 'POST',
      body: JSON.stringify({
        keywords: keywords,
        pins_per_keyword: state.depth,
        niche: $('#l2-main').value.trim(),
      }),
    });
    gotoScreen('s4');
    banner('Scraping ' + keywords.length + ' keyword(s)...', 'info');
    pollScrape();
  } catch (err) {
    banner('Could not start: ' + err.message, 'error');
  }
});

async function pollScrape() {
  let status;
  try {
    status = await api('/scrape/status');
  } catch {
    setTimeout(pollScrape, 3000);
    return;
  }

  const pct = status.total ? Math.round((status.done / status.total) * 100) : 0;
  const bar = $('#progress-bar');
  if (bar) bar.style.width = pct + '%';
  const label = $('#progress-label');
  if (label) {
    if (status.running) {
      label.textContent = (status.keyword || '...') + ' - ' + status.done + '/'
                        + status.total + ' keywords, ' + status.pins + ' pins';
    } else if (status.finished) {
      label.textContent = 'Done - ' + status.pins + ' pins from ' + status.total + ' keyword(s)';
    } else {
      label.textContent = 'Idle';
    }
  }

  if (status.running) { setTimeout(pollScrape, 2000); return; }

  if (status.finished) {
    const failed = status.failed || [];
    banner(failed.length
      ? 'Finished with ' + status.pins + ' pins. ' + failed.length
        + ' keyword(s) failed: ' + failed.join(', ')
      : 'Finished - ' + status.pins + ' pins collected. Go to Screen 5 to export.',
      failed.length ? 'warn' : 'info');
    await loadResults();
  }
}

/* Called on boot as well as after a run, so a browser refresh brings back the
   pins instead of showing an empty Review screen. */
async function loadResults() {
  try {
    const result = await api('/scrape/results');
    state.pins = result.pins;
    renderPins(result.pins);
    const acc = $('#accepted-count');
    if (acc) acc.textContent = result.count + ' collected';
    if (result.count) {
      // Load the PREVIOUS result rather than recomputing it. A full pass over
      // hundreds of pins takes ~96s, and re-running it on every refresh made
      // the app look frozen while it redid finished work.
      try {
        const last = await api('/filter/last');
        if (!last.empty) {
          state.review = last;
          renderReview();

          // Rules-only result: show it now, then finish the AI pass in the
          // background so the screen is never blank while it runs.
          if (last.quick && last.summary && last.summary.ai_pending) {
            banner('Showing your pins. Checking relevance in the background…',
                   'info');
            runFilters({ quiet: true }).then((full) => {
              if (full) banner('Relevance check done: ' + full.summary.accepted
                + ' accepted, ' + full.summary.rejected + ' rejected.', 'info');
            });
          }
        }
      } catch { /* nothing saved yet */ }
      if (result.restored) {
        banner('Restored ' + result.count + ' pins from your last run.', 'info');
      }
    }
  } catch {
    /* nothing collected */
  }
}

function renderPins(pins) {
  const tbody = $('#pins-table tbody');
  if (!tbody) return;
  if (!pins.length) {
    tbody.innerHTML = '<tr><td colspan="5" class="empty">No pins yet.</td></tr>';
    return;
  }
  tbody.innerHTML = '';
  pins.slice(0, 300).forEach((p) => {
    const tr = document.createElement('tr');
    const img = (p.image_url && p.image_url !== 'N/A')
      ? '<img src="' + escapeHtml(p.image_url) + '" alt="" style="width:44px;height:44px;'
        + 'object-fit:cover;border-radius:6px">'
      : '';
    tr.innerHTML =
      '<td>' + img + '</td>'
      + '<td>' + escapeHtml(String(p.spy_title || '').slice(0, 90)) + '</td>'
      + '<td>' + escapeHtml(p.seed_keyword || '') + '</td>'
      + '<td class="num">' + escapeHtml(String(p.pin_score == null ? '' : p.pin_score)) + '</td>'
      + '<td class="num">' + escapeHtml(String(p.saves_count == null ? '' : p.saves_count)) + '</td>';
    tbody.appendChild(tr);
  });
}

/* ------------------------------------------------- export */

$('#btn-export').addEventListener('click', async (e) => {
  const btn = e.currentTarget;
  btn.disabled = true;
  banner('Building the CSV…', 'info');
  try {
    const result = await api('/export', {
      method: 'POST',
      body: JSON.stringify({ label: state.board.sub ? state.board.sub.name : 'keywords' }),
    });

    // Export means export: the accepted file downloads straight away rather
    // than leaving a second link to hunt for.
    const name = result.names && result.names.accepted;
    if (name) {
      const a = document.createElement('a');
      a.href = '/api/exports/' + encodeURIComponent(name);
      a.download = name;
      document.body.appendChild(a);
      a.click();
      a.remove();
    }

    // Rejects stay behind a link, since they are the occasional case.
    const rej = $('#dl-rejected');
    const rejName = result.names && result.names.rejected;
    if (rej) {
      if (rejName) {
        rej.href = '/api/exports/' + encodeURIComponent(rejName);
        rej.setAttribute('download', rejName);
        rej.textContent = 'Download rejects (' + result.rejected + ')';
        rej.style.display = '';
      } else {
        rej.style.display = 'none';
      }
    }

    banner('Exported ' + result.rows + ' accepted pins.'
      + (result.rejected ? ' ' + result.rejected + ' rejected available below.' : ''),
      'info');
  } catch (err) {
    banner('Export failed: ' + err.message, 'error');
  } finally {
    btn.disabled = false;
  }
});

['#btn-pause', '#btn-stop'].forEach((sel) => {
  const el = $(sel);
  if (el) el.addEventListener('click', () => banner('Not implemented yet.', 'warn'));
});


boot();
pollLog();


/* Refresh re-scrapes only the level you are actually looking at, rather than
   throwing the whole cache away. Deepest selection wins. */
$('#btn-refresh-level').addEventListener('click', async (e) => {
  const btn = e.currentTarget;
  btn.disabled = true;
  try {
    if (state.board.sub) {
      await pickSub(state.board.sub, true);
      banner('Re-scraped keywords for "' + state.board.sub.name + '".', 'info');
    } else if (state.board.path.length) {
      await loadSubs(true);
      banner('Re-scraped sub-niches.', 'info');
    } else {
      state.forceRefresh = true;
      $('#btn-discover').click();
      state.forceRefresh = false;
    }
  } finally {
    btn.disabled = false;
  }
});

/* ------------------------------------------------- phase-2 filtering */

state.review = { accepted: [], rejected: [], unreviewed: [] };

async function runFilters({ quiet = false } = {}) {
  if (!quiet) banner('Applying filters…', 'info');
  try {
    const result = await api('/filter', {
      method: 'POST',
      body: JSON.stringify({
        niche: state.board.sub ? state.board.sub.name : '',
        use_ai: true,
      }),
    });
    state.review = result;
    renderReview();
    if (quiet) return result;

    const s = result.summary;
    let msg = s.accepted + ' accepted, ' + s.rejected + ' rejected';
    if (s.unreviewed) msg += ', ' + s.unreviewed + ' unreviewed';
    banner(s.note ? msg + ' — ' + s.note : msg, s.note ? 'warn' : 'info');
    return result;
  } catch (err) {
    if (!quiet) banner('Filtering failed: ' + err.message, 'error');
    return null;
  }
}

$('#btn-refilter').addEventListener('click', async (e) => {
  const btn = e.currentTarget;
  btn.disabled = true;
  try {
    await runFilters();
  } finally {
    btn.disabled = false;
  }
});

function matchesSearch(p, q) {
  if (!q) return true;
  return String(p.spy_title || '').toLowerCase().includes(q)
      || String(p.seed_keyword || '').toLowerCase().includes(q);
}

function num(v) { return Number(String(v ?? '').replace(/,/g, '')) || 0; }

/* Screen-level filters. These narrow what you are LOOKING at; they do not
   change what the rules decided, so nothing is lost by using them. */
function passesBar(p) {
  const minSaves = num($('#rv-min-saves') && $('#rv-min-saves').value);
  const minScore = num($('#rv-min-score') && $('#rv-min-score').value);
  if (minSaves && num(p.saves_count) < minSaves) return false;
  if (minScore && num(p.pin_score) < minScore) return false;
  return true;
}

function sortBar(rows) {
  const by = ($('#rv-sort') && $('#rv-sort').value) || 'saves_count';
  const copy = rows.slice();
  if (by === 'position') {
    copy.sort((a, b) => (num(a.position) || 1e6) - (num(b.position) || 1e6));
  } else {
    copy.sort((a, b) => num(b[by]) - num(a[by]));
  }
  return copy;
}

/* Reject reasons are too varied to list one by one -- the AI writes a fresh
   sentence for every pin ("Not fruit pizza", "Fruit platter, not pizza").
   Grouping by their leading phrase turns hundreds of strings into a handful
   of useful choices. */
function reasonGroup(reason) {
  const r = String(reason || '').trim();
  if (!r) return 'other';
  if (/^ai:/i.test(r)) {
    return /listicle/i.test(r) ? 'AI: roundup' : 'AI: off-niche';
  }
  // "other language (es words (de))" -> "other language"
  // "under 1,000 saves"              -> "under N saves"
  return r.split('(')[0].trim().replace(/\d[\d,]*/g, 'N');
}

function rebuildReasonOptions(rejected) {
  const sel = $('#rv-reason');
  if (!sel) return;
  const counts = new Map();
  rejected.forEach((p) => {
    const g = reasonGroup(p.reject_reason);
    counts.set(g, (counts.get(g) || 0) + 1);
  });

  const chosen = sel.value;
  sel.innerHTML = '';
  const all = document.createElement('option');
  all.value = '';
  all.textContent = 'all reasons (' + rejected.length + ')';
  sel.appendChild(all);

  [...counts.entries()]
    .sort((a, b) => b[1] - a[1])
    .forEach(([group, n]) => {
      const o = document.createElement('option');
      o.value = group;
      o.textContent = group + ' (' + n + ')';
      sel.appendChild(o);
    });

  // Keep the selection if that reason still exists after a re-filter.
  sel.value = [...counts.keys()].includes(chosen) ? chosen : '';
}

function updatePickedCount() {
  const el = $('#rv-selected');
  if (el) el.textContent = state.picked.size + ' selected';
}

/* One pin, rendered the same way in both columns so they can be compared at
   a glance. Rejected pins show WHY -- an unauditable filter is not trustworthy. */
state.picked = new Set();

function pinKey(p) {
  return (p.pin_url && p.pin_url !== 'N/A')
    ? p.pin_url.toLowerCase()
    : String(p.spy_title || '').toLowerCase().replace(/[^a-z0-9]+/g, ' ').trim();
}

function pinRow(p, rejected) {
  const row = document.createElement('div');
  row.className = 'pin-row' + (rejected ? ' is-rejected' : '');

  const key = pinKey(p);
  const pick = document.createElement('input');
  pick.type = 'checkbox';
  pick.className = 'pin-pick';
  pick.checked = state.picked.has(key);
  pick.addEventListener('change', () => {
    if (pick.checked) state.picked.add(key); else state.picked.delete(key);
    updatePickedCount();
  });
  row.appendChild(pick);

  if (p.image_url && p.image_url !== 'N/A') {
    const a = document.createElement('a');
    a.href = (p.pin_url && p.pin_url !== 'N/A') ? p.pin_url : p.image_url;
    a.target = '_blank';
    a.rel = 'noreferrer';
    const img = document.createElement('img');
    img.src = p.image_url;
    img.loading = 'lazy';
    img.alt = '';
    a.appendChild(img);
    row.appendChild(a);
  }

  const main = document.createElement('div');
  main.className = 'pin-main';

  const title = document.createElement('a');
  title.className = 'pin-title';
  title.href = (p.pin_url && p.pin_url !== 'N/A') ? p.pin_url : '#';
  title.target = '_blank';
  title.rel = 'noreferrer';
  title.textContent = p.spy_title && p.spy_title !== 'N/A'
    ? p.spy_title
    : (p.spy_description && p.spy_description !== 'N/A'
        ? p.spy_description : '(no title)');
  main.appendChild(title);

  const meta = document.createElement('div');
  meta.className = 'pin-meta';
  const saves = document.createElement('span');
  saves.textContent = Number(p.saves_count || 0).toLocaleString() + ' saves';
  meta.appendChild(saves);
  const score = document.createElement('span');
  score.textContent = 'score ' + Number(p.pin_score || 0).toLocaleString();
  meta.appendChild(score);
  const kw = document.createElement('span');
  kw.className = 'pin-kw';
  kw.textContent = p.seed_keyword || '';
  meta.appendChild(kw);
  main.appendChild(meta);

  if (rejected && p.reject_reason) {
    const why = document.createElement('div');
    why.className = 'pin-why';
    why.textContent = p.reject_reason;
    main.appendChild(why);
  }

  row.appendChild(main);

  // Your judgement overrules the filters, so every row can be moved.
  const act = document.createElement('button');
  act.className = 'chip pin-act';
  act.textContent = rejected ? 'keep' : 'reject';
  act.title = rejected
    ? 'Move this pin to Accepted and keep it there'
    : 'Move this pin to Rejected';
  act.addEventListener('click', (e) => {
    e.stopPropagation();
    overridePin(p, rejected ? 'accepted' : 'rejected');
  });
  row.appendChild(act);

  if (p.manual) {
    const flag = document.createElement('span');
    flag.className = 'pin-manual';
    flag.textContent = 'yours';
    flag.title = 'You set this. Re-running the filters will not change it.';
    row.appendChild(flag);
  }

  return row;
}

/* Overrides are stored server-side, so they survive Re-run filters, a rules
   change and a restart. */
async function overridePin(pin, status) {
  const id = (pin.pin_url && pin.pin_url !== 'N/A')
    ? pin.pin_url.toLowerCase()
    : String(pin.spy_title || '').toLowerCase().replace(/[^a-z0-9]+/g, ' ').trim();
  try {
    await api('/filter/override', {
      method: 'POST',
      body: JSON.stringify({ pin_id: id, status }),
    });
    const fresh = await api('/filter', {
      method: 'POST',
      body: JSON.stringify({ niche: '', use_ai: false }),
    });
    state.review = fresh;
    renderReview();
    banner(status === 'accepted' ? 'Moved to Accepted.' : 'Moved to Rejected.', 'info');
  } catch (err) {
    banner('Could not move that pin: ' + err.message, 'error');
  }
}

function fillColumn(bodyId, pins, rejected, emptyText) {
  const body = $(bodyId);
  if (!body) return;
  body.innerHTML = '';
  if (!pins.length) {
    body.innerHTML = '<div class="col-empty">' + escapeHtml(emptyText) + '</div>';
    return;
  }
  pins.slice(0, 400).forEach((p) => body.appendChild(pinRow(p, rejected)));
}

function renderReview() {
  const r = state.review || {};
  const q = ($('#review-search') ? $('#review-search').value : '')
    .trim().toLowerCase();

  const keep = (p) => matchesSearch(p, q) && passesBar(p);
  const accepted = sortBar([...(r.accepted || []), ...(r.unreviewed || [])].filter(keep));

  // Options come from everything rejected, so a reason never disappears from
  // the list just because it is currently filtered out.
  const rejectedAll = (r.rejected || []).filter(keep);
  rebuildReasonOptions(rejectedAll);

  const wantReason = ($('#rv-reason') && $('#rv-reason').value) || '';
  const rejected = sortBar(wantReason
    ? rejectedAll.filter((p) => reasonGroup(p.reject_reason) === wantReason)
    : rejectedAll);

  const rc = $('#rv-reason-count');
  if (rc) {
    rc.textContent = wantReason && rejected.length !== rejectedAll.length
      ? rejected.length + ' of ' + rejectedAll.length : '';
  }

  const shown = accepted.length + rejected.length;
  const total = (r.accepted || []).length + (r.unreviewed || []).length
              + (r.rejected || []).length;
  const showing = $('#rv-showing');
  if (showing) {
    showing.textContent = shown === total
      ? total + ' pins' : 'showing ' + shown + ' of ' + total;
  }

  setCount('#accepted-count', accepted.length, false);
  setCount('#rejected-count', rejected.length, false);

  fillColumn('#accepted-body', accepted, false,
             q ? 'No accepted pins match.' : 'Run a scrape, then Re-run filters.');
  fillColumn('#rejected-body', rejected, true,
             q ? 'No rejected pins match.' : 'Nothing rejected yet.');
}

const _reviewSearch = $('#review-search');
if (_reviewSearch) _reviewSearch.addEventListener('input', renderReview);

/* ------------------------------------------------- settings: API keys */

async function renderKeys() {
  let status;
  try {
    status = await api('/keys');
  } catch {
    return;
  }
  const list = $('#key-list');
  if (!list) return;

  if (!status.count) {
    list.innerHTML = '<p class="hint" style="margin:0">'
      + 'No keys yet. Off-niche filtering stays off until you add one.</p>';
  } else {
    list.innerHTML = '';
    status.keys.forEach((k, i) => {
      const row = document.createElement('div');
      row.className = 'row';
      row.style.cssText = 'padding:8px 0;border-bottom:1px solid var(--line-soft)';

      const pill = document.createElement('span');
      pill.className = 'pill ' + (k.state === 'ready' ? 'active'
                       : k.state === 'resting' ? 'warning' : 'expired');
      pill.textContent = k.state === 'resting'
        ? 'resting ' + k.resting_for + 's' : k.state;
      row.appendChild(pill);

      const name = document.createElement('span');
      name.style.cssText = 'font-family:var(--mono);font-size:12px';
      name.textContent = k.masked + (k.label && k.label !== k.masked
        ? '  ' + k.label : '');
      row.appendChild(name);

      const stat = document.createElement('span');
      stat.className = 'hint';
      stat.style.marginLeft = 'auto';
      stat.textContent = k.calls + ' calls'
        + (k.failures ? ', ' + k.failures + ' failed' : '');
      row.appendChild(stat);

      const del = document.createElement('button');
      del.className = 'btn quiet';
      del.textContent = 'Remove';
      del.addEventListener('click', () => removeKey(i));
      row.appendChild(del);

      list.appendChild(row);
    });
  }

  $('#key-summary').textContent = status.count
    ? status.count + ' key(s): ' + status.ready + ' ready, '
      + status.resting + ' resting, ' + status.disabled + ' disabled'
    : 'No keys yet.';
}

/* The masked value is sent back for untouched keys; the server treats that as
   "unchanged" so the real secret never has to travel again. */
async function currentKeyEntries() {
  const status = await api('/keys');
  return status.keys.map((k) => ({ value: k.masked, label: k.label }));
}

async function saveKeys(entries) {
  await api('/keys', { method: 'PUT', body: JSON.stringify({ keys: entries }) });
  await renderKeys();
}

async function removeKey(index) {
  const entries = await currentKeyEntries();
  entries.splice(index, 1);
  await saveKeys(entries);
  banner('Key removed.', 'info');
}

if ($('#btn-add-key')) {
  $('#btn-add-key').addEventListener('click', async () => {
    const value = $('#key-value').value.trim();
    if (!value) { banner('Paste a key first.', 'warn'); return; }
    try {
      const entries = await currentKeyEntries();
      entries.push({ value: value, label: $('#key-label').value.trim() });
      await saveKeys(entries);
      $('#key-value').value = '';
      $('#key-label').value = '';
      banner('Key added.', 'info');
    } catch (err) {
      banner('Could not save the key: ' + err.message, 'error');
    }
  });

  $('#btn-test-keys').addEventListener('click', async (e) => {
    e.currentTarget.disabled = true;
    banner('Testing keys…', 'info');
    try {
      const r = await api('/keys/test', { method: 'POST' });
      banner(r.ok ? 'Keys work. Model replied: ' + (r.reply || '').slice(0, 60)
                  : 'Test failed: ' + (r.error || 'unknown'),
             r.ok ? 'info' : 'error');
      await renderKeys();
    } catch (err) {
      banner('Test failed: ' + err.message, 'error');
    } finally {
      e.currentTarget.disabled = false;
    }
  });
}

/* Bulk key entry. Someone running thousands of keywords needs a dozen keys,
   and adding them one at a time is not a workflow. Accepts one per line,
   optionally "key, label", and also a pasted .txt/.csv/.json file. */

function parseKeyBlob(text) {
  const out = [];
  const seen = new Set();

  // A JSON array or object is accepted as-is, so an export from elsewhere
  // can be pasted without reformatting.
  const trimmed = text.trim();
  if (trimmed.startsWith('[') || trimmed.startsWith('{')) {
    try {
      const data = JSON.parse(trimmed);
      const items = Array.isArray(data) ? data
        : (data.ollama_api_keys || data.keys || []);
      items.forEach((it) => {
        const value = (typeof it === 'string' ? it : (it.value || it.key || '')).trim();
        if (value && !seen.has(value)) {
          seen.add(value);
          out.push({ value, label: (typeof it === 'object' && it.label) || '' });
        }
      });
      if (out.length) return out;
    } catch { /* fall through to line parsing */ }
  }

  text.split(/[\r\n]+/).forEach((line) => {
    const raw = line.trim();
    if (!raw || raw.startsWith('#')) return;
    const [value, ...rest] = raw.split(',');
    const key = value.trim();
    if (!key || seen.has(key)) return;
    seen.add(key);
    out.push({ value: key, label: rest.join(',').trim() });
  });
  return out;
}

if ($('#btn-add-bulk')) {
  $('#btn-add-bulk').addEventListener('click', async () => {
    const parsed = parseKeyBlob($('#key-bulk').value);
    if (!parsed.length) { banner('No keys found in that text.', 'warn'); return; }
    try {
      const existing = await currentKeyEntries();
      const known = new Set(existing.map((e) => e.value));
      const added = parsed.filter((k) => !known.has(k.value));
      await saveKeys(existing.concat(added));
      $('#key-bulk').value = '';
      banner('Added ' + added.length + ' key(s).'
        + (parsed.length - added.length
            ? ' ' + (parsed.length - added.length) + ' already present.' : ''), 'info');
    } catch (err) {
      banner('Could not save keys: ' + err.message, 'error');
    }
  });

  $('#key-file').addEventListener('change', async (e) => {
    const file = e.target.files && e.target.files[0];
    if (!file) return;
    $('#key-bulk').value = await file.text();
    e.target.value = '';
    banner('File loaded. Check it, then press "Add all".', 'info');
  });
}

/* ------------------------------------------------- review filter bar */

['#rv-min-saves', '#rv-min-score'].forEach((sel) => {
  const el = $(sel);
  if (el) el.addEventListener('input', renderReview);
});
if ($('#rv-sort')) $('#rv-sort').addEventListener('change', renderReview);

/* "Select all shown" deliberately means SHOWN, not everything: with a saves
   filter applied, ticking it should select exactly what you can see. */
if ($('#rv-select-all')) {
  $('#rv-select-all').addEventListener('change', (e) => {
    const boxes = $$('.pin-pick');
    boxes.forEach((b) => {
      b.checked = e.target.checked;
      b.dispatchEvent(new Event('change'));
    });
  });
}

async function bulkOverride(status) {
  const ids = [...state.picked];
  if (!ids.length) { banner('Select some pins first.', 'warn'); return; }

  banner('Moving ' + ids.length + ' pin(s)…', 'info');
  try {
    // Sequential: each call rewrites the shared override map, so parallel
    // writes would race and lose some of them.
    for (const id of ids) {
      await api('/filter/override', {
        method: 'POST',
        body: JSON.stringify({ pin_id: id, status }),
      });
    }
    state.picked.clear();
    if ($('#rv-select-all')) $('#rv-select-all').checked = false;

    const fresh = await api('/filter', {
      method: 'POST', body: JSON.stringify({ niche: '', use_ai: false }),
    });
    state.review = fresh;
    renderReview();
    banner('Moved ' + ids.length + ' pin(s) to ' + status + '.', 'info');
  } catch (err) {
    banner('Could not move those pins: ' + err.message, 'error');
  }
}

if ($('#rv-accept')) $('#rv-accept').addEventListener('click', () => bulkOverride('accepted'));
if ($('#rv-reject')) $('#rv-reject').addEventListener('click', () => bulkOverride('rejected'));

if ($('#rv-reason')) $('#rv-reason').addEventListener('change', renderReview);
