{
const CATS = [
  { id: 'book', name: 'Literature & readings' },
  { id: 'read', name: 'Reading together' },
  { id: 'philo', name: 'Philosophy & talks' },
  { id: 'art', name: 'Art & workshops' },
  { id: 'dance', name: 'Dance' },
  { id: 'event', name: 'Events & music' },
  { id: 'offer', name: 'Offers & discounts' },
];
const catName = id => (CATS.find(c => c.id === id) || {}).name || id;
const PRICE_ANY = 105;

const cfg = window.ZE_CONFIG || {};
const $ = id => document.getElementById(id);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const safeUrl = u => /^https?:\/\//i.test(u || '') ? u : '#';

let sb, user;
let events = [], sources = [], swipes = new Map(), lastRun = null;
let prefs = { hidden_sources: [], hidden_categories: [], price_max: null, show_unpriced: true, from_date: null };
const HIDDEN_STATUS = ['registration_closed', 'sold_out', 'cancelled'];
let undoStack = [];
let tab = 'discover';

const toast = msg => { const t = $('toast'); t.textContent = msg; t.classList.add('on'); clearTimeout(toast.h); toast.h = setTimeout(() => t.classList.remove('on'), 2200); };

/* ---------- time + formatting ---------- */
const tz = 'Europe/Zurich';
const fmtDay = d => d.toLocaleDateString('en-GB', { weekday: 'short', day: 'numeric', month: 'short', timeZone: tz });
const fmtTime = d => d.toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit', timeZone: tz });
const fmtPrice = e => {
  if (e.price_min == null) return null;
  if (+e.price_min === 0 && (e.price_max == null || +e.price_max === 0)) return 'Free';
  const a = +e.price_min, b = e.price_max == null ? a : +e.price_max;
  const f = n => Number.isInteger(n) ? n : n.toFixed(2);
  return a === b ? `CHF ${f(a)}` : `CHF ${f(a)}–${f(b)}`;
};
const endOf = e => {
  if (e.end_at) return new Date(e.end_at);
  const s = new Date(e.start_at);
  return e.all_day ? new Date(s.getTime() + 86400000) : new Date(s.getTime() + 2 * 3600000);
};

/* ---------- visibility rules ---------- */
function visible(e) {
  const now = new Date();
  if (swipes.has(e.id)) return false;                                     // A) swiped
  if (e.kind === 'event' && (!e.start_at || endOf(e) < now)) return false; // B) passed
  if (e.registration_deadline && new Date(e.registration_deadline) < now) return false; // C) registration closed
  if (HIDDEN_STATUS.includes(e.status)) return false;                     // closed, sold out or cancelled
  if (prefs.hidden_sources.includes(e.source_id)) return false;
  if (prefs.hidden_categories.includes(e.category)) return false;
  return true;
}
function priceOk(e) {
  if (e.price_min == null) return prefs.show_unpriced;
  return prefs.price_max == null || +e.price_min <= prefs.price_max;
}
const todayStr = () => new Date().toLocaleDateString('en-CA', { timeZone: tz });
const fromOk = e => {
  if (!prefs.from_date || prefs.from_date <= todayStr()) return true;
  return new Date(e.start_at).toLocaleDateString('en-CA', { timeZone: tz }) >= prefs.from_date;
};
const deck = () => events.filter(e => e.kind === 'event' && visible(e) && priceOk(e) && fromOk(e))
  .sort((a, b) => new Date(a.start_at) - new Date(b.start_at));
const offers = () => events.filter(e => e.kind === 'offer' && visible(e));
const saved = () => events.filter(e => swipes.get(e.id) === 'saved' && (e.kind === 'offer' || endOf(e) >= new Date()))
  .sort((a, b) => (a.start_at ? new Date(a.start_at) : Infinity) - (b.start_at ? new Date(b.start_at) : Infinity));
const srcName = id => (sources.find(s => s.id === id) || {}).name || id;

/* ---------- rendering ---------- */
function cardHTML(e, cls = '') {
  const s = new Date(e.start_at);
  const price = fmtPrice(e);
  const dl = e.registration_deadline ? new Date(e.registration_deadline) : null;
  return `<article class="card ${cls}" data-id="${esc(e.id)}">
    <span class="stamp save">SAVE</span><span class="stamp drop">DISCARD</span>
    <div class="cat"><span class="dot"></span>${esc(catName(e.category))}</div>
    <div class="when">${esc(fmtDay(s))}<small>${e.all_day ? 'All day' : esc(fmtTime(s)) + (e.end_at ? '–' + esc(fmtTime(new Date(e.end_at))) : '')}</small></div>
    <h2>${esc(e.title)}</h2>
    ${e.summary ? `<div class="sum">${esc(e.summary)}</div>` : ''}
    ${e.venue ? `<div class="venue">${esc(e.venue)}</div>` : ''}
    <div class="tags">
      <span class="tag ${price ? 'price' : ''}">${esc(price || 'Price not listed')}</span>
      ${e.price_text && price ? `<span class="tag">${esc(e.price_text.slice(0, 60))}</span>` : ''}
      ${dl ? `<span class="tag deadline">Register by ${esc(fmtDay(dl))}</span>` : ''}
    </div>
    <div class="foot"><span>${esc(srcName(e.source_id))}</span><a href="${esc(safeUrl(e.url))}" target="_blank" rel="noopener">Details ↗</a></div>
  </article>`;
}

function renderDeck() {
  const d = deck();
  const st = $('stack');
  if (!d.length) {
    st.innerHTML = `<div class="empty"><div><strong>You're all caught up</strong>New events arrive every morning. Loosen the price filter or switch on more categories to see more.</div></div>`;
  } else {
    st.innerHTML = (d[1] ? cardHTML(d[1], 'next') : '') + cardHTML(d[0], 'top');
    bindDrag(st.querySelector('.card.top'));
  }
  $('btn-save').disabled = $('btn-drop').disabled = !d.length;
  $('btn-undo').disabled = !undoStack.length;
  $('n-discover').textContent = d.length || '';
}

function listItem(e, buttons) {
  const when = e.start_at ? `${fmtDay(new Date(e.start_at))}${e.all_day ? '' : ', ' + fmtTime(new Date(e.start_at))}` : 'Ongoing';
  const bits = [when, e.venue, fmtPrice(e) || 'price not listed', srcName(e.source_id)].filter(Boolean);
  return `<li><a class="t" href="${esc(safeUrl(e.url))}" target="_blank" rel="noopener">${esc(e.title)}</a>
    <span class="m">${esc(bits.join(' · '))}${e.summary && e.kind === 'offer' ? '<br>' + esc(e.summary) : ''}</span>
    <span class="b">${buttons}</span></li>`;
}

function renderSaved() {
  const s = saved();
  $('saved').innerHTML = s.length ? s.map(e => listItem(e, `<button data-unsave="${esc(e.id)}">Remove</button>`)).join('')
    : `<li style="display:block;color:var(--soft)">Swipe right on an event to save it here.</li>`;
  $('n-saved').textContent = s.length || '';
}

function renderOffers() {
  const o = offers();
  $('offers').innerHTML = o.length ? o.map(e => listItem(e,
    `<button data-swipe="discarded" data-id="${esc(e.id)}">Hide</button><button data-swipe="saved" data-id="${esc(e.id)}">Save</button>`)).join('')
    : `<li style="display:block;color:var(--soft)">No offers right now.</li>`;
  $('n-offers').textContent = o.length || '';
}

function renderSources() {
  const ago = t => t ? new Date(t).toLocaleString('en-GB', { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit', timeZone: tz }) : 'never';
  $('sources').innerHTML = CATS.map(c => {
    const list = sources.filter(s => s.category === c.id);
    if (!list.length) return '';
    const catOn = !prefs.hidden_categories.includes(c.id);
    return `<h3><span class="dot"></span>${esc(c.name)}</h3>
      <label class="switch"><span><strong>Whole category</strong></span><input type="checkbox" data-cat="${c.id}" ${catOn ? 'checked' : ''}></label>
      ${list.map(s => {
        const on = !prefs.hidden_sources.includes(s.id);
        const err = s.last_error && (!s.last_ok || new Date(s.last_ok) < new Date(s.last_checked));
        return `<label class="switch"><span>${esc(s.name)}
          <small class="${err ? 'err' : ''}">${err ? 'Last check failed: ' + esc(s.last_error.slice(0, 90)) : `${s.upcoming || 0} upcoming · checked ${esc(ago(s.last_checked))}`}</small></span>
          <input type="checkbox" data-src="${esc(s.id)}" ${on ? 'checked' : ''}></label>`;
      }).join('')}`;
  }).join('');
}

function renderChips() {
  $('cats').innerHTML = CATS.filter(c => c.id !== 'offer').map(c =>
    `<button class="chip" data-chip="${c.id}" aria-pressed="${!prefs.hidden_categories.includes(c.id)}"><span class="dot" style="color:var(--blue)"></span>${esc(c.name)}</button>`).join('');
  const v = prefs.price_max == null ? PRICE_ANY : prefs.price_max;
  $('pmax').value = v; $('pmax-out').textContent = v >= PRICE_ANY ? 'Any' : v === 0 ? 'Free only' : `≤ CHF ${v}`;
  $('unpriced').checked = prefs.show_unpriced;
  $('from').min = todayStr();
  $('from').value = prefs.from_date && prefs.from_date > todayStr() ? prefs.from_date : todayStr();
}

function renderStatus() {
  if (!lastRun) { $('status').textContent = 'No check yet'; return; }
  const t = new Date(lastRun.finished_at || lastRun.started_at);
  $('status').innerHTML = `Updated ${esc(fmtDay(t))}, ${esc(fmtTime(t))}<br>${lastRun.new_events ?? 0} new${lastRun.sources_failed ? ` · ${lastRun.sources_failed} source${lastRun.sources_failed > 1 ? 's' : ''} failed` : ''}`;
}

function renderAll() { renderChips(); renderDeck(); renderSaved(); renderOffers(); renderSources(); renderStatus(); }

/* ---------- data ---------- */
async function load() {
  const since = new Date(Date.now() - 86400000).toISOString();
  const [ev, src, sw, pr, run] = await Promise.all([
    sb.from('events').select('*').or(`start_at.gte.${since},kind.eq.offer`).limit(2000),
    sb.from('sources').select('*'),
    sb.from('swipes').select('event_id,decision'),
    sb.from('prefs').select('*').maybeSingle(),
    sb.from('runs').select('*').order('id', { ascending: false }).limit(1),
  ]);
  for (const r of [ev, src, sw]) if (r.error) throw r.error;
  events = ev.data; sources = src.data;
  swipes = new Map(sw.data.map(s => [s.event_id, s.decision]));
  if (pr.data) prefs = { ...prefs, ...pr.data, hidden_sources: pr.data.hidden_sources || [], hidden_categories: pr.data.hidden_categories || [] };
  lastRun = run.data && run.data[0];
  renderAll();
}

let prefTimer;
function savePrefs() {
  clearTimeout(prefTimer);
  prefTimer = setTimeout(async () => {
    const { hidden_sources, hidden_categories, price_max, show_unpriced, from_date } = prefs;
    const { error } = await sb.from('prefs').upsert({ user_id: user.id, hidden_sources, hidden_categories, price_max, show_unpriced, from_date, updated_at: new Date().toISOString() });
    if (error) toast('Could not save settings');
  }, 400);
}

async function swipe(id, decision) {
  swipes.set(id, decision);
  undoStack.push(id);
  renderAll();
  const { error } = await sb.from('swipes').upsert({ user_id: user.id, event_id: id, decision });
  if (error) { swipes.delete(id); undoStack.pop(); renderAll(); toast('Could not save, try again'); }
}

async function unswipe(id) {
  const prev = swipes.get(id);
  swipes.delete(id);
  renderAll();
  const { error } = await sb.from('swipes').delete().eq('event_id', id);
  if (error) { swipes.set(id, prev); renderAll(); toast('Could not undo'); }
}

/* ---------- swipe gesture ---------- */
const reduce = matchMedia('(prefers-reduced-motion: reduce)').matches;
function fling(card, dir) {
  const id = card.dataset.id;
  if (reduce) return swipe(id, dir > 0 ? 'saved' : 'discarded');
  card.style.transition = 'transform .25s ease-out, opacity .25s';
  card.style.transform = `translateX(${dir * 130}%) rotate(${dir * 18}deg)`;
  card.style.opacity = '0';
  setTimeout(() => swipe(id, dir > 0 ? 'saved' : 'discarded'), 200);
}
function bindDrag(card) {
  if (!card) return;
  let x0 = null, y0 = 0, dx = 0, pid = null;
  const stamps = card.querySelectorAll('.stamp');
  card.addEventListener('pointerdown', e => {
    if (e.target.closest('a')) return;
    x0 = e.clientX; y0 = e.clientY; dx = 0; pid = e.pointerId;
    card.setPointerCapture(pid); card.style.transition = 'none';
  });
  card.addEventListener('pointermove', e => {
    if (x0 === null || e.pointerId !== pid) return;
    dx = e.clientX - x0;
    const dy = e.clientY - y0;
    card.style.transform = `translate(${dx}px, ${dy * 0.15}px) rotate(${dx / 18}deg)`;
    stamps[0].style.opacity = Math.max(0, Math.min(1, dx / 90));
    stamps[1].style.opacity = Math.max(0, Math.min(1, -dx / 90));
  });
  const end = () => {
    if (x0 === null) return;
    x0 = null;
    if (Math.abs(dx) > Math.min(110, card.offsetWidth * 0.28)) return fling(card, Math.sign(dx));
    card.style.transition = 'transform .2s ease-out';
    card.style.transform = '';
    stamps.forEach(s => s.style.opacity = 0);
  };
  card.addEventListener('pointerup', end);
  card.addEventListener('pointercancel', end);
}

/* ---------- events ---------- */
function bind() {
  $('tabs').addEventListener('click', e => {
    const b = e.target.closest('button[data-tab]'); if (!b) return;
    tab = b.dataset.tab;
    document.querySelectorAll('#tabs button').forEach(x => x.setAttribute('aria-current', x === b));
    ['discover', 'saved', 'offers', 'sources'].forEach(t => $('tab-' + t).hidden = t !== tab);
    $('tab-discover').style.display = tab === 'discover' ? 'flex' : '';
  });
  $('btn-save').onclick = () => { const c = $('stack').querySelector('.card.top'); if (c) fling(c, 1); };
  $('btn-drop').onclick = () => { const c = $('stack').querySelector('.card.top'); if (c) fling(c, -1); };
  $('btn-undo').onclick = () => { const id = undoStack.pop(); if (id) unswipe(id); };
  document.addEventListener('keydown', e => {
    if (tab !== 'discover' || e.target.matches('input')) return;
    if (e.key === 'ArrowRight') $('btn-save').click();
    if (e.key === 'ArrowLeft') $('btn-drop').click();
    if ((e.key === 'z' || e.key === 'Z') && (e.metaKey || e.ctrlKey)) $('btn-undo').click();
  });
  $('pmax').addEventListener('input', e => {
    const v = +e.target.value;
    prefs.price_max = v >= PRICE_ANY ? null : v;
    renderChips(); renderDeck(); savePrefs();
  });
  $('unpriced').addEventListener('change', e => { prefs.show_unpriced = e.target.checked; renderDeck(); savePrefs(); });
  $('from').addEventListener('change', e => { prefs.from_date = e.target.value && e.target.value > todayStr() ? e.target.value : null; renderDeck(); savePrefs(); });
  $('cats').addEventListener('click', e => {
    const b = e.target.closest('[data-chip]'); if (!b) return;
    toggle(prefs.hidden_categories, b.dataset.chip); renderAll(); savePrefs();
  });
  $('sources').addEventListener('change', e => {
    const t = e.target;
    if (t.dataset.src) toggle(prefs.hidden_sources, t.dataset.src, !t.checked);
    if (t.dataset.cat) toggle(prefs.hidden_categories, t.dataset.cat, !t.checked);
    renderAll(); savePrefs();
  });
  document.addEventListener('click', e => {
    const u = e.target.closest('[data-unsave]');
    if (u) { swipe(u.dataset.unsave, 'discarded'); toast('Removed from saved'); }
    const s = e.target.closest('[data-swipe]');
    if (s) swipe(s.dataset.id, s.dataset.swipe);
  });
  $('logout').onclick = async e => { e.preventDefault(); await sb.auth.signOut(); location.reload(); };
  $('signin').onclick = async () => {
    const email = $('email').value.trim(), password = $('password').value;
    if (!email || !password) return toast('Enter email and password');
    $('signin').disabled = true;
    const { error } = await sb.auth.signInWithPassword({ email, password });
    $('signin').disabled = false;
    if (error) toast(/invalid/i.test(error.message) ? 'Wrong email or password' : error.message);
  };
  $('password').addEventListener('keydown', e => { if (e.key === 'Enter') $('signin').click(); });
  $('send').onclick = async e => {
    e.preventDefault();
    const email = $('email').value.trim(); if (!email) return toast('Enter your email first');
    const { error } = await sb.auth.signInWithOtp({ email, options: { shouldCreateUser: false, emailRedirectTo: location.origin + location.pathname } });
    if (error) toast(error.message); else $('sent').hidden = false;
  };
  $('pwform').addEventListener('submit', async e => {
    e.preventDefault();
    const password = $('newpw').value;
    if (password.length < 8) return toast('At least 8 characters');
    const { error } = await sb.auth.updateUser({ password });
    if (error) toast(error.message); else { $('newpw').value = ''; toast('Password set. Use it in the home-screen app.'); }
  });
  document.addEventListener('visibilitychange', () => { if (!document.hidden && user) load().catch(() => {}); });
}
function toggle(arr, v, force) {
  const i = arr.indexOf(v);
  const want = force === undefined ? i < 0 : force;
  if (want && i < 0) arr.push(v);
  if (!want && i >= 0) arr.splice(i, 1);
}

/* ---------- start ---------- */
async function start() {
  if (!cfg.SUPABASE_URL || !window.supabase) { $('status').textContent = 'Not configured yet'; return; }
  sb = window.supabase.createClient(cfg.SUPABASE_URL, cfg.SUPABASE_ANON_KEY);
  bind();
  const { data } = await sb.auth.getSession();
  user = data.session && data.session.user;
  sb.auth.onAuthStateChange((_ev, session) => {
    const was = user; user = session && session.user;
    if (!was && user) show();
  });
  if (user) show(); else { $('login').hidden = false; $('status').textContent = ''; }
}
async function show() {
  $('login').hidden = true; $('main').hidden = false;
  try { await load(); } catch (e) { $('status').textContent = 'Could not load'; toast(e.message || 'Load failed'); }
}
start();
}
