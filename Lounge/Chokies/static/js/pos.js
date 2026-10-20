/* pos.js — Nova PoS front end (single file). Sections: core, data, auth, home, orders, ticket, payment, shift, main. */

/* ======================================================================== */
/* CORE */
/* ======================================================================== */
/* core.js — shared state, constants, formatting helpers, API client, toasts and the prompt dialog.
   Loaded first; every other file builds on what is declared here. */

const $ = (s, r = document) => r.querySelector(s), $$ = (s, r = document) => [...r.querySelectorAll(s)];

/* ---------- state ---------- */
const S = {
  me: null, menu: [], tables: [], orders: [], paid: [], bookings: [],
  sales: { summary: { count: 0, total: 0, by_method: {} }, bills: [] },   // waiter's own paid bills (shift/sales/)
  drawer: null, order: null,            // order = the one shown in the ticket
  sp: 'MENU', cat: 0, q: '',            // order form: selling point, category, search
  filter: 'ACTIVE', waiter: 0, pm: 'CASH', mpMode: 'STK', cardMode: 'CARD',
};
let payBill = null, payKey = null, payCfg = { mpesa: {}, bank: {} }, stk = null;

/* ---------- constants ---------- */
// An order starts at one selling point; items from either can be added to it. Each item is routed to its own station when sent.
const SPS = [
  { key: 'MENU', name: 'Restaurant', station: 'KITCHEN', hint: 'Meals from the kitchen' },
  { key: 'BAR', name: 'Bar', station: 'BAR', hint: 'Drinks from the bar' },
];
const ICONS = {
  MENU: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M11 9H9V2H7v7H5V2H3v7c0 2.12 1.66 3.84 3.75 3.97V22h2.5v-9.03C11.34 12.84 13 11.12 13 9V2h-2v7zm5-3v8h2.5v8H21V2c-2.76 0-5 2.24-5 4z"/></svg>',
  BAR: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M21 5V3H3v2l8 9v5H6v2h12v-2h-5v-5l8-9zM7.43 7L5.66 5h12.69l-1.78 2H7.43z"/></svg>',
  ROOM: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M7 13c1.66 0 3-1.34 3-3S8.66 7 7 7s-3 1.34-3 3 1.34 3 3 3zm12-6h-8v7H3V5H1v15h2v-3h18v3h2v-9c0-2.21-1.79-4-4-4z"/></svg>',
};
const METHODS = { CASH: 'Cash', MOBILE_MONEY: 'M-Pesa', CARD: 'Card', VOUCHER: 'Voucher' };

/* ---------- formatting ---------- */
const money = v => Number(v || 0).toLocaleString('en-KE', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const plural = (n, w) => `${n} ${w}${n === 1 ? '' : 's'}`;
const timeOf = iso => iso ? new Date(iso).toLocaleTimeString('en-KE', { hour: '2-digit', minute: '2-digit' }) : '';
const ago = iso => { const m = Math.max(0, Math.round((Date.now() - new Date(iso)) / 60000)); return m < 1 ? 'just now' : m < 60 ? `${m} min` : `${Math.floor(m / 60)}h ${m % 60}m`; };
const spName = k => k === 'ROOM' ? 'Rooms' : SPS.find(s => s.key === k)?.name || k;
const statusLabel = s => s === 'SENT' ? 'in progress' : s.toLowerCase();
const whereOf = o => o.table_name || (o.room_number ? 'Room ' + o.room_number : o.source === 'BAR' ? 'Bar' : 'No table');
const liveItems = o => o.items.filter(i => i.status !== 'VOID');

/* ---------- role helpers ---------- */
const isCashier = () => S.me?.shift?.role === 'CASHIER';
const canPrint = () => isCashier() || !!S.me?.is_manager;   // paid receipts are printed by the cashier (or a manager); a waiter prints only the unpaid bill (see renderActions)
const csrf = () => $('[name=csrfmiddlewaretoken]')?.value || '';

/* ---------- API ---------- */
async function api(path, method = 'GET', body) {
  const h = { 'Content-Type': 'application/json' };
  h['X-CSRFToken'] = csrf();
  const r = await fetch('/api/' + path, { method, headers: h, credentials: 'same-origin', body: body ? JSON.stringify(body) : undefined });
  if (r.status === 401) { showLogin(); throw new Error('Please sign in'); }
  const d = r.status === 204 ? {} : await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(d.detail || Object.entries(d).map(([k, v]) => `${k}: ${v}`).join(' ') || 'Request failed');
  return d;
}

/* ---------- toasts (top-layer popover, so they show above open dialogs) ---------- */
function toast(msg, type = 'ok') {
  const box = $('#toasts'), t = document.createElement('div');
  t.className = 'toast ' + type; t.setAttribute('role', type === 'bad' ? 'alert' : 'status'); t.textContent = msg; box.append(t);
  try { if (box.matches(':popover-open')) box.hidePopover(); box.showPopover(); } catch { /* browser without popover: the fixed box still shows */ }
  setTimeout(() => { t.remove(); if (!box.children.length) try { box.hidePopover(); } catch { } }, 5000);
}
const run = fn => async (...a) => { try { await fn(...a); } catch (e) { toast(e.message, 'bad'); } };   // wrap click handlers: errors become toasts

/* ---------- prompt dialog: ask(title, fields, okLabel) -> {values} | null ---------- */
function ask(title, fields, ok = 'Confirm') {
  return new Promise(res => {
    const d = $('#dlg'); $('#dlgTitle').textContent = title; $('#dlgOk').textContent = ok;
    $('#dlgBody').innerHTML = fields.map(f => f.type === 'note' ? `<p class="fnote">${esc(f.label)}</p>` : f.type === 'check'
      ? `<label class="chk${f.pick ? ' pick' : ''}"><input type="checkbox" name="${f.name}"> <span>${esc(f.label)}</span></label>`
      : `<label>${esc(f.label)}<input name="${f.name}" type="${f.type || 'text'}" ${f.type === 'number' ? 'step="0.01" inputmode="decimal"' : ''} ${f.req ? 'required' : ''}></label>`).join('');
    d.returnValue = ''; d.showModal(); $('input', d)?.focus();
    d.onclose = () => { if (d.returnValue !== 'ok') return res(null); const o = {}; $$('input', d).forEach(i => o[i.name] = i.type === 'checkbox' ? i.checked : i.value); res(o); };
  });
}
const pinField = () => S.me.is_manager ? [] : [{ name: 'manager_pin', label: 'Manager PIN', type: 'password', req: true }];

/* ======================================================================== */
/* DATA */
/* ======================================================================== */
/* data.js — loads server data into S and re-renders every part of the screen that depends on it. */

async function loadMenu() { if (!isCashier()) S.menu = await api('menu/'); }

async function refresh() {
  const cashier = isCashier();
  const [tables, orders, paid, sales, bookings] = await Promise.all([
    api('tables/'), api('orders/'),
    cashier ? api('bills/') : [],
    cashier ? null : api('shift/sales/').catch(() => null),   // the waiter's own paid bills (needs the shift/sales/ route)
    api('bookings/').catch(() => []),                          // room bookings (waiters: their own)
  ]);
  Object.assign(S, { tables, orders, paid, bookings }); if (sales) S.sales = sales;
  if (S.order) S.order = S.orders.find(o => o.id === S.order.id) || null;
  renderDrawer(await api('shift/current/'));
  renderOrders(); renderTicket(); renderHome(); renderOrdersPage(); renderBookingBadges();
  if (page === 'book') await fetchRooms().catch(() => { });          // room availability changes as others book
  if (page === 'booking' && BPG.id) await loadBookingPage().catch(() => { });
  if (page === 'order' && OPG.id) await loadOrderPage().catch(() => { });   // the order page shows current numbers too (an order that was paid stays visible there)
}

/* ======================================================================== */
/* AUTH */
/* ======================================================================== */
/* auth.js — shaping the screen for the signed-in role. Signing in happens on the login page (login.html), which starts the
   session and sends each person to the right place; this file only ever sends people back there. */

/* Back to the login page. `flash` (optional) is shown there once, e.g. the cash count after ending a shift. */
function showLogin(flash) {
  S.me = null; S.order = null; OPG.id = null; OPG.d = null; clearOrderHash();
  $$('dialog[open]').forEach(d => d.close()); toggleProfile(false);
  if (flash) try { sessionStorage.setItem('login-flash', flash); } catch (e) { /* storage blocked: skip the message */ }
  location.replace(document.body.dataset.loginUrl || '/pin-login/');   // POS_LOGIN_URL, set by pos_page
}

/* Waiters get the home screen; cashiers get the desk (order list + drawer). Orders open on their own page for both. Both get the top tabs. */
function applyRole() {
  const cashier = isCashier();
  document.body.dataset.role = cashier ? 'cashier' : 'waiter';
  buildNav(); showPage('home');
}

async function start() {
  const deep = orderRoute(), deepBk = bookingRoute();   // a reload on an order / booking page: showPage('home') below clears the hash, so remember it
  try { S.me = await api('auth/me/'); } catch { return; }
  $('#app').classList.remove('hidden');
  const m = S.me, sh = m.shift;
  applyRole();
  $('#whoName').textContent = $('#pmName').textContent = m.name; $('#whoRole').textContent = sh ? sh.role.toLowerCase() : m.role.toLowerCase();
  $('#avatar').textContent = $('#avatarBig').textContent = m.name.split(' ').map(x => x[0]).join('').slice(0, 2).toUpperCase();
  $('#shiftChip').textContent = sh ? `${sh.name || 'Shift #' + sh.id}${sh.terminal ? ' · ' + sh.terminal : ''}` : 'No shift';
  $('#endShift').classList.toggle('hidden', !sh);
  $('#notice').classList.toggle('hidden', !!sh);
  await Promise.all([loadMenu(), refresh()]);
  if (deep) await openOrderPage(deep);
  else if (deepBk) await openBookingPage(deepBk, 'bookings');
}

/* ======================================================================== */
/* HOME */
/* ======================================================================== */
/* home.js — waiter home screen (greeting, selling points), the top navigation,
   the "My sales" table modal, and the full "Open orders" page. */

/* ---------- home ---------- */
const greeting = () => { const h = new Date().getHours(); return h < 12 ? 'Good morning' : h < 17 ? 'Good afternoon' : 'Good evening'; };

function renderHome() {
  if (isCashier() || !S.me) return;
  const sh = S.me.shift;
  $('#greet').textContent = `${greeting()}, ${S.me.name.split(' ')[0]}`;
  $('#greetSub').textContent = sh ? `${sh.name || 'Shift #' + sh.id} · where does the next order start?` : '';
  renderSellingPoints();
}

function renderSellingPoints() {
  $('#spGrid').innerHTML = SPS.map(s => {
    const n = S.orders.filter(o => o.source === s.key && ['OPEN', 'SENT'].includes(o.status)).length;
    return `<button type="button" class="spcard" data-sp="${s.key}"><span class="spicon">${ICONS[s.key]}</span>
      <b>${s.name}</b><small>${s.hint}</small><span class="pill">${plural(n, 'open order')}</span><span class="spgo">Start order →</span></button>`;
  }).join('');
  const due = S.bookings.filter(b => b.status === 'RESERVED' && b.check_in <= ymd(Date.now())).length;
  $('#spGrid').insertAdjacentHTML('beforeend', `<button type="button" class="spcard" data-go="book"><span class="spicon">${ICONS.ROOM}</span>
    <b>Rooms</b><small>Book a stay for a guest</small><span class="pill">${due ? plural(due, 'arrival') + ' due' : ''}</span><span class="spgo">Book room →</span></button>`);
  $$('#spGrid .spcard[data-sp]').forEach(b => b.onclick = run(() => openOrderForm(b.dataset.sp)));
  $('#spGrid [data-go]').onclick = run(() => navTo('book'));
}

/* ---------- navigation ---------- */
// Tabs depend on the role. "Home" / "Desk" is the main screen; "Open orders" / "Orders" and (cashier) "Paid bills" are table pages
// inside #app (body[data-page] decides which shows); "My sales" is a modal table.
let page = 'home';
const NAV_ICONS = {
  home: 'M10 20v-6h4v6h5v-8h3L12 3 2 12h3v8z', sales: 'M5 9.2h3V19H5zM10.6 5h2.8v14h-2.8zm5.6 8H19v6h-2.8z',
  orders: 'M3 13h2v-2H3v2zm0 4h2v-2H3v2zm0-8h2V7H3v2zm4 4h14v-2H7v2zm0 4h14v-2H7v2zM7 7v2h14V7H7z',
  book: 'M7 13c1.66 0 3-1.34 3-3S8.66 7 7 7s-3 1.34-3 3 1.34 3 3 3zm12-6h-8v7H3V5H1v15h2v-3h18v3h2v-9c0-2.21-1.79-4-4-4z',
  bookings: 'M17 10H7v2h10v-2zm2-7h-1V1h-2v2H8V1H6v2H5c-1.11 0-1.99.9-1.99 2L3 19c0 1.1.89 2 2 2h14c1.1 0 2-.9 2-2V5c0-1.1-.9-2-2-2zm0 16H5V8h14v11zm-5-5H7v2h7v-2z',
  online: 'M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm-1 17.93c-3.95-.49-7-3.85-7-7.93 0-.62.08-1.21.21-1.79L9 15v1c0 1.1.9 2 2 2v1.93zm6.9-2.54c-.26-.81-1-1.39-1.9-1.39h-1v-3c0-.55-.45-1-1-1H8v-2h2c.55 0 1-.45 1-1V7h2c1.1 0 2-.9 2-2v-.41c2.93 1.19 5 4.06 5 7.41 0 2.08-.8 3.97-2.1 5.39z',
  paid: 'M18 17H6v-2h12v2zm0-4H6v-2h12v2zm0-4H6V7h12v2zM3 22l1.5-1.5L6 22l1.5-1.5L9 22l1.5-1.5L12 22l1.5-1.5L15 22l1.5-1.5L18 22l1.5-1.5L21 22V2l-1.5 1.5L18 2l-1.5 1.5L15 2l-1.5 1.5L12 2l-1.5 1.5L9 2 7.5 3.5 6 2 4.5 3.5 3 2v20z',
};
const NAVS = {   // [key, label, badge id]
  waiter: [['home', 'Home'], ['sales', 'My sales'], ['orders', 'Open orders', 'openN'], ['book', 'Book room'], ['bookings', 'Bookings', 'bookN'], ['online', 'Online bookings', 'onlineN']],
  cashier: [['home', 'Desk'], ['orders', 'Orders', 'openN'], ['paid', 'Paid bills', 'paidN'], ['book', 'Book room'], ['bookings', 'Bookings', 'bookN'], ['online', 'Online bookings', 'onlineN']],
};
function buildNav() {
  $('#mainNav').innerHTML = NAVS[isCashier() ? 'cashier' : 'waiter'].map(([k, label, badge]) =>
    `<button type="button" data-nav="${k}"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="${NAV_ICONS[k]}"/></svg>${label}${badge ? ` <i id="${badge}" class="badge"></i>` : ''}</button>`).join('');
}
const setNav = k => $$('#mainNav button').forEach(b => b.classList.toggle('on', b.dataset.nav === k));
const navKey = () => page === 'order' ? OPG.from : page === 'booking' ? BPG.from : page;   // the order page belongs to the tab it was opened from
function showPage(p) {
  const back = (page === 'order' && p === OPG.from) || (page === 'booking' && p === BPG.from);   // returning from an order / booking page: keep the table's filters, sort and page
  if (PAGES[p] && p !== page && (!back || OP.cur !== p)) resetPage(p);   // a different table page: start from its own defaults
  if (p !== 'order') { clearOrderHash(); OPG.id = null; OPG.d = null; OPG.pushed = false; setMenu(false); }   // leaving the order page by any route
  if (p !== 'booking') { clearBookingHash(); BPG.id = null; BPG.d = null; BPG.pushed = false; }   // leaving the booking page by any route
  page = p; document.body.dataset.page = p; setNav(navKey());
  if (PAGES[p]) renderOrdersPage();
}
async function navTo(k) {
  if (k === 'sales') return openData('sales');
  $('#dataDlg').close();
  if (PAGES[k]) await refresh();   // always show current numbers
  showPage(k);
  if (k === 'book') await enterBook();
  if (k === 'online') renderOnline();
}

/* ---------- "My sales" modal ---------- */
// Says where its rows come from and how to draw them. v = plain text (also used by search), html = optional rich cell, sum = column total.
const VIEWS = {
  sales: {
    title: 'My sales', noun: 'bill', empty: 'No paid bills on this shift yet',
    sub: () => S.me.shift ? (S.me.shift.name || 'Shift #' + S.me.shift.id) : '',
    rows: () => S.sales.bills,
    stats: () => { const s = S.sales.summary; return [['Total sales', 'KSh ' + money(s.total)], ['Bills paid', s.count], ...Object.entries(s.by_method || {}).map(([k, v]) => [METHODS[k] || k, 'KSh ' + money(v)])]; },
    cols: [
      { h: 'Bill', v: r => r.number }, { h: 'Order', v: r => r.order_number },
      { h: 'Where', v: r => r.table || (r.source === 'BAR' ? 'Bar' : r.source === 'ROOM' ? 'Room booking' : '—') },
      { h: 'Paid', v: r => timeOf(r.paid_at) },
      { h: 'Payment', v: r => (r.methods || []).map(m => METHODS[m] || m).join(', ') },
      { h: 'Items', v: r => r.items, cls: 'num' },
      { h: 'Total', v: r => money(r.total), sum: r => +r.total, cls: 'num strong' },
    ],
  },
};
let dataKind = null;

function renderData() {
  const V = VIEWS[dataKind]; if (!V) return;
  const q = $('#dSearch').value.trim().toLowerCase();
  const rows = V.rows().filter(r => !q || V.cols.map(c => c.v(r)).join(' ').toLowerCase().includes(q));
  $('#dTitle').textContent = V.title; $('#dSub').textContent = V.sub();
  $('#dStats').innerHTML = V.stats().map(([k, v]) => `<div class="stat"><small>${esc(k)}</small><b>${esc(v)}</b></div>`).join('');
  $('#dHead').innerHTML = '<tr>' + V.cols.map(c => `<th class="${c.cls?.includes('num') ? 'num' : ''}">${c.h}</th>`).join('') + '</tr>';
  $('#dBody').innerHTML = rows.length
    ? rows.map(r => `<tr${r.order ? ` class="click" data-order="${r.order}"` : r.booking ? ` class="click" data-booking="${r.booking}"` : ''}>` + V.cols.map(c => `<td class="${c.cls || ''}">${c.html ? c.html(r) : esc(c.v(r))}</td>`).join('') + '</tr>').join('')
    : `<tr><td colspan="${V.cols.length}" class="empty">${q ? 'No matches' : V.empty}</td></tr>`;
  $('#dFoot').innerHTML = rows.length && V.cols.some(c => c.sum)
    ? '<tr>' + V.cols.map((c, i) => c.sum ? `<td class="num">${money(rows.reduce((n, r) => n + c.sum(r), 0))}</td>` : `<td>${i ? '' : 'Total · ' + plural(rows.length, V.noun)}</td>`).join('') + '</tr>' : '';
}

async function openData(kind) {
  await refresh();   // always show current numbers
  dataKind = kind; $('#dSearch').value = ''; renderData(); setNav(kind);
  if (!$('#dataDlg').open) $('#dataDlg').showModal();
}

/* ---------- table pages: Open orders (waiter + cashier) and Paid bills (cashier) ---------- */
// Filter card + table card: sortable columns, search, page size, pager. Orders expand with a "+" to show their items.
const OP = { rangeKey: '-30,0', size: 10, page: 1, sort: { k: 'date', dir: 1 }, open: new Set(), f: { sp: '', st: '', w: 0, from: '', to: '' } };
const ymd = t => new Date(t).toLocaleDateString('en-CA');   // yyyy-mm-dd in the browser's own time zone
const dateTime = iso => new Date(iso).toLocaleString('en-GB', { day: '2-digit', month: '2-digit', year: 'numeric', hour: '2-digit', minute: '2-digit' });
const PLUS = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 2a10 10 0 1 0 0 20 10 10 0 0 0 0-20zm5 11h-4v4h-2v-4H7v-2h4V7h2v4h4z"/></svg>';
const MINUS = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 2a10 10 0 1 0 0 20 10 10 0 0 0 0-20zm5 11H7v-2h10z"/></svg>';
const receiptText = p => p.printed ? 'printed' + (p.printed > 1 ? ' ×' + p.printed : '') : 'not printed';

const ORDER_COLS = [
  { k: 'num', h: 'Order', v: o => o.number },
  { k: 'where', h: 'Where', v: whereOf },
  { k: 'sp', h: 'Selling point', v: o => spName(o.source) },
  { k: 'date', h: 'Date', v: o => o.created_at, show: o => dateTime(o.created_at), cls: 'wrap' },
  { k: 'att', h: 'Attendant', v: o => o.waiter_name },
  { k: 'items', h: 'Items', v: o => liveItems(o).length, cls: 'num' },
  { k: 'status', h: 'Status', v: o => statusLabel(o.status), html: o => `<span class="pill ${o.status}">${statusLabel(o.status)}</span>` },
  { k: 'net', h: 'Net amount', v: o => +o.totals.subtotal, show: o => money(o.totals.subtotal), cls: 'num', sum: true },
  { k: 'bal', h: 'Balance', v: o => +o.totals.total, show: o => money(o.totals.total), cls: 'num strong', sum: true },
  { k: 'act', h: '', html: () => '<button type="button" class="btn primary sm oopen">Open</button>', cls: 'num' },
];
const PAID_COLS = [
  { k: 'num', h: 'Bill', v: p => p.number },
  { k: 'ord', h: 'Order', v: p => p.order_number },
  { k: 'where', h: 'Where', v: p => p.table || (p.source === 'BAR' ? 'Bar' : p.source === 'ROOM' ? 'Room booking' : '—') },
  { k: 'sp', h: 'Selling point', v: p => spName(p.source) },
  { k: 'date', h: 'Paid', v: p => p.paid_at, show: p => dateTime(p.paid_at), cls: 'wrap' },
  { k: 'att', h: 'Waiter', v: p => p.waiter_name },
  { k: 'rcpt', h: 'Receipt', v: receiptText, html: p => `<span class="pill ${p.printed ? 'PAID' : 'WARN'}">${receiptText(p)}</span>` },
  { k: 'bal', h: 'Total', v: p => +p.total, show: p => money(p.total), cls: 'num strong', sum: true },
  { k: 'act', h: '', html: p => `<button type="button" class="btn sm pprint ${p.printed ? 'ghost' : 'primary'}">${p.printed ? 'Reprint' : 'Print receipt'}</button>`, cls: 'num' },
];
/* room bookings: shared helpers and the Bookings table (drawn by the same table-page code as Open orders) */
const PLANS = [['BO', 'Bed only', 'BO'], ['BNB', 'Bed & breakfast', 'BnB'], ['HB', 'Half board', 'HB'], ['FB', 'Full board', 'FB']];
const planName = k => PLANS.find(p => p[0] === k)?.[1] || k;
const dmy = iso => iso ? new Date(String(iso).slice(0, 10) + 'T12:00:00').toLocaleDateString('en-GB', { day: '2-digit', month: 'short', year: 'numeric' }) : '';
const bookingStatus = s => ({ RESERVED: 'reserved', CHECKED_IN: 'checked in', CHECKED_OUT: 'checked out', CANCELLED: 'cancelled' }[s] || s.toLowerCase());
const payState = s => ({ UNBILLED: 'not billed', UNPAID: 'unpaid', PART: 'part paid', PAID: 'paid', REFUNDED: 'refunded' }[s] || String(s).toLowerCase());
const BOOKING_COLS = [
  { k: 'num', h: 'Booking', v: b => b.number },
  { k: 'guest', h: 'Guest', v: b => b.guest_name, cls: 'wrap' },
  { k: 'phone', h: 'Phone', v: b => b.phone },
  { k: 'rooms', h: 'Rooms', v: b => b.rooms.join(', '), cls: 'wrap' },
  { k: 'in', h: 'Check-in', v: b => b.check_in, show: b => dmy(b.check_in) },
  { k: 'out', h: 'Check-out', v: b => b.check_out, show: b => dmy(b.check_out) },
  { k: 'status', h: 'Status', v: b => bookingStatus(b.status), html: b => `<span class="pill ${b.status}">${bookingStatus(b.status)}</span>` },
  { k: 'pay', h: 'Payment', v: b => payState(b.pay_state), html: b => `<span class="pill P-${b.pay_state}">${payState(b.pay_state)}</span>` },
  { k: 'att', h: 'Booked by', v: b => b.waiter_name },
  { k: 'total', h: 'Total', v: b => b.status === 'CANCELLED' ? 0 : +b.total, show: b => money(b.status === 'CANCELLED' ? 0 : b.total), cls: 'num', sum: true },
  { k: 'bal', h: 'Balance', v: b => b.status === 'CANCELLED' ? 0 : +b.balance, show: b => money(b.status === 'CANCELLED' ? 0 : b.balance), cls: 'num strong', sum: true },
  { k: 'act', h: '', html: () => '<button type="button" class="btn primary sm oopen">Open</button>', cls: 'num' },
];
const bookingDetail = (b, n) => `<tr class="detail"><td colspan="${n}"><div class="dwrap"><table class="itab"><thead><tr><th>Room</th><th>Meal plan</th><th class="num">Price per night</th><th class="num">Nights</th><th class="num">Subtotal</th></tr></thead><tbody>${b.lines.map(l =>
  `<tr><td>${esc(l.name)}</td><td>${planName(l.meal_plan)}</td><td class="num">${money(l.unit_price)}</td><td class="num">${b.nights}</td><td class="num">${money(l.unit_price * b.nights)}</td></tr>`).join('')}</tbody></table>`
  + (b.narrative ? `<p class="dnote"><b>Note</b> ${esc(b.narrative)}</p>` : '') + (b.status === 'CANCELLED' && b.cancel_reason ? `<p class="dnote"><b>Cancelled</b> ${esc(b.cancel_reason)}</p>` : '') + '</div></td></tr>';

const PAGES = {
  bookings: {
    title: 'Bookings', cap: 'Filter by check-in date', cols: BOOKING_COLS, rows: () => S.bookings, date: b => b.check_in + 'T12:00:00', sort: { k: 'in', dir: -1 }, expand: true, detail: bookingDetail, range: [-30, 90],
    optLabel: 'Booking status', opts: [['', 'All bookings'], ['RESERVED', 'Reserved'], ['CHECKED_IN', 'Checked in'], ['CHECKED_OUT', 'Checked out'], ['CANCELLED', 'Cancelled']], test: (b, v) => b.status === v,
    noun: 'booking', unit: 'bookings', empty: 'No bookings yet', newBtn: [['book', '+ Book room']],
  },
  orders: {
    title: 'Open orders', cols: ORDER_COLS, rows: () => S.orders, date: o => o.created_at, sort: { k: 'date', dir: 1 }, expand: true,
    optLabel: 'Order option', opts: [['', 'All open orders'], ['OPEN', 'Open'], ['SENT', 'In progress'], ['BILLED', 'Billed (awaiting payment)']], test: (o, v) => o.status === v,
    noun: 'order', unit: 'open orders', empty: 'No open orders',
  },
  paid: {
    title: 'Paid bills', cols: PAID_COLS, rows: () => S.paid, date: p => p.paid_at, sort: { k: 'date', dir: -1 },
    optLabel: 'Receipt', opts: [['', 'All bills'], ['NO', 'Not printed'], ['YES', 'Printed']], test: (p, v) => v === 'YES' ? p.printed > 0 : !p.printed,
    noun: 'bill', unit: 'paid bills', empty: 'No paid bills on the open shifts yet',
  },
};

function resetPage(p) {   // called when switching to a different table page
  const P = PAGES[p]; OP.cur = p;
  OP.sort = { ...P.sort }; OP.page = 1; OP.open.clear(); OP.f = { ...OP.f, sp: '', st: '', w: 0 };
  const [a, b] = P.range || [-30, 0], rk = a + ',' + b;   // bookings look ahead as well as back; switching between order pages keeps your dates
  if (OP.rangeKey !== rk) { OP.rangeKey = rk; OP.f.from = $('#fFrom').value = ymd(Date.now() + a * 864e5); OP.f.to = $('#fTo').value = ymd(Date.now() + b * 864e5); }
  $('#fCap').textContent = P.cap || 'Filter by date range';
  $('#opNew').innerHTML = P.newBtn ? P.newBtn.map(([go, t]) => `<button type="button" class="btn dark sm" data-go="${go}">${t}</button>`).join('')
    : isCashier() ? '' : SPS.map(s => `<button type="button" class="btn dark sm" data-sp="${s.key}">+ ${s.name} order</button>`).join('');   // cashiers don't start orders
  $('#opTitle').textContent = P.title; $('#fStatusLbl').textContent = P.optLabel;
  $('#fStatus').innerHTML = P.opts.map(([v, l]) => `<option value="${v}">${l}</option>`).join(''); $('#fSp').value = ''; $('#opSearch').value = '';
  $('#fWaiter').dataset.key = '';   // force the waiter list to rebuild for this page
}

function fillWaiters() {   // cashier filter: only the waiters that have rows on this page; rebuilt only when that set changes (so an open dropdown isn't reset)
  const P = PAGES[page]; if (!P || !isCashier()) return;
  const by = new Map(); P.rows().forEach(r => by.set(r.waiter, r.waiter_name)); const key = [...by.keys()].join(','), sel = $('#fWaiter');
  if (sel.dataset.key === key) return;
  sel.dataset.key = key; sel.innerHTML = '<option value="0">All waiters</option>' + [...by].map(([id, n]) => `<option value="${id}">${esc(n)}</option>`).join('');
  sel.value = by.has(OP.f.w) ? OP.f.w : '0';
}

function opRows() {
  const P = PAGES[page], { sp, st, w, from, to } = OP.f, q = $('#opSearch').value.trim().toLowerCase();
  return P.rows().filter(r => (!sp || r.source === sp) && (!st || P.test(r, st)) && (!w || r.waiter === w) && (!from || ymd(P.date(r)) >= from) && (!to || ymd(P.date(r)) <= to)
    && (!q || P.cols.filter(c => c.v).map(c => c.show ? c.show(r) : c.v(r)).join(' ').toLowerCase().includes(q)));
}

const opDetail = (o, n) => {
  const items = liveItems(o);
  return `<tr class="detail"><td colspan="${n}"><div class="dwrap">` + (items.length
    ? `<table class="itab"><thead><tr><th>Item</th><th class="num">Qty</th><th>Station</th><th class="num">Price</th><th class="num">Subtotal</th></tr></thead><tbody>${items.map(i =>
      `<tr><td>${esc(i.name)}</td><td class="num">${i.quantity}</td><td>${i.station === 'BAR' ? 'Bar' : 'Kitchen'}</td><td class="num">${money(i.unit_price)}</td><td class="num">${money(i.line_total)}</td></tr>`).join('')}</tbody></table>`
    : '<p class="muted">No items on this order yet.</p>')
    + (o.room_number && o.table_name ? `<p class="dnote"><b>Room</b> ${esc(o.room_number)}</p>` : '') + (o.notes ? `<p class="dnote"><b>Note</b> ${esc(o.notes)}</p>` : '') + '</div></td></tr>';
};

function opPager(pages) {
  const cur = OP.page, nums = [...new Set([1, cur - 1, cur, cur + 1, pages])].filter(n => n >= 1 && n <= pages).sort((a, b) => a - b);
  const btn = (label, p, on = false, dis = false) => `<button type="button" data-p="${p}" class="${on ? 'on' : ''}"${dis ? ' disabled' : ''}>${label}</button>`;
  let out = btn('Previous', cur - 1, false, cur <= 1);
  nums.forEach((n, i) => { if (i && n - nums[i - 1] > 1) out += '<span class="gap">…</span>'; out += btn(n, n, n === cur); });
  return out + btn('Next', cur + 1, false, cur >= pages);
}

function renderOrdersPage(focusId) {
  const P = PAGES[page]; if (!P || !S.me) return;
  fillWaiters();
  const cols = P.cols, col = cols.find(c => c.k === OP.sort.k) || cols[0], dir = OP.sort.dir, rows = opRows(), all = P.rows().length;
  rows.sort((a, b) => { const x = col.v(a), y = col.v(b); return dir * (typeof x === 'number' ? x - y : String(x).localeCompare(String(y), undefined, { numeric: true })); });
  const pages = Math.max(1, Math.ceil(rows.length / OP.size)); OP.page = Math.min(Math.max(1, OP.page), pages);
  const start = (OP.page - 1) * OP.size, view = rows.slice(start, start + OP.size);
  $('#opHead').innerHTML = '<tr>' + cols.map(c => c.v
    ? `<th data-k="${c.k}" class="sortable${c.cls?.includes('num') ? ' num' : ''}${c.k === col.k ? ' on' : ''}" aria-sort="${c.k === col.k ? (dir > 0 ? 'ascending' : 'descending') : 'none'}">${c.h}<span class="sa" aria-hidden="true">${c.k === col.k ? (dir > 0 ? '↑' : '↓') : '↕'}</span></th>`
    : '<th></th>').join('') + '</tr>';
  $('#opBody').innerHTML = view.length ? view.map(r => {
    const open = P.expand && OP.open.has(r.id);
    return `<tr class="row${open ? ' exp' : ''}" data-id="${r.id}">` + cols.map(c =>
      c.k === 'num' ? `<td>${P.expand ? `<button type="button" class="xbtn" aria-expanded="${open}" aria-label="${open ? 'Hide' : 'Show'} items of ${esc(r.number)}">${open ? MINUS : PLUS}</button>` : ''}<button type="button" class="olink">${esc(r.number)}</button></td>`
        : `<td class="${c.cls || ''}">${c.html ? c.html(r) : esc(c.show ? c.show(r) : c.v(r))}</td>`).join('') + '</tr>' + (open ? (P.detail || opDetail)(r, cols.length) : '');
  }).join('') : `<tr><td colspan="${cols.length}" class="empty">${all ? 'No ' + P.unit + ' match these filters' : P.empty}</td></tr>`;
  $('#opFoot').innerHTML = rows.length ? '<tr>' + cols.map((c, i) => c.sum ? `<td class="num">${money(rows.reduce((n, r) => n + c.v(r), 0))}</td>` : `<td>${i ? '' : 'Total · ' + plural(rows.length, P.noun)}</td>`).join('') + '</tr>' : '';
  $('#opInfo').textContent = `Showing ${rows.length ? start + 1 : 0} to ${start + view.length} of ${rows.length} ${rows.length === 1 ? 'entry' : 'entries'}` + (rows.length !== all ? ` (filtered from ${all} ${P.unit})` : '');
  $('#opPager').innerHTML = pages > 1 ? opPager(pages) : '';
  if (focusId) $(`#opBody tr[data-id="${focusId}"] .xbtn`)?.focus();
}

function initOrdersPage() {
  OP.f.to = $('#fTo').value = ymd(Date.now()); OP.f.from = $('#fFrom').value = ymd(Date.now() - 30 * 864e5);
  $('#fSp').innerHTML = '<option value="">All selling points</option>' + SPS.map(s => `<option value="${s.key}">${s.name}</option>`).join('');
  $('#opNew').onclick = run(async e => { const b = e.target.closest('[data-sp],[data-go]'); if (!b) return; if (b.dataset.go) await navTo(b.dataset.go); else await openOrderForm(b.dataset.sp); });
  $('#fGo').onclick = () => {
    const from = $('#fFrom').value, to = $('#fTo').value;
    if (!from || !to) return toast('Pick both dates', 'bad');
    if (from > to) return toast('The From date is after the To date', 'bad');
    OP.f = { sp: $('#fSp').value, st: $('#fStatus').value, w: isCashier() ? +$('#fWaiter').value || 0 : 0, from, to }; OP.page = 1; renderOrdersPage();
  };
  $('#opSearch').oninput = () => { OP.page = 1; renderOrdersPage(); };
  $('#opSize').onchange = e => { OP.size = +e.target.value; OP.page = 1; renderOrdersPage(); };
  $('#opHead').onclick = e => {
    const th = e.target.closest('th[data-k]'); if (!th) return;
    OP.sort = { k: th.dataset.k, dir: OP.sort.k === th.dataset.k ? -OP.sort.dir : 1 }; renderOrdersPage();
  };
  $('#opPager').onclick = e => { const b = e.target.closest('button[data-p]'); if (b && !b.disabled) { OP.page = +b.dataset.p; renderOrdersPage(); } };
  $('#opBody').onclick = run(async e => {
    const tr = e.target.closest('tr[data-id]'); if (!tr) return; const id = +tr.dataset.id;
    if (e.target.closest('.xbtn')) { OP.open.has(id) ? OP.open.delete(id) : OP.open.add(id); renderOrdersPage(id); }
    else if (e.target.closest('.olink, .oopen')) {   // an order (or a paid bill's order, or a booking) opens on its own page
      if (page === 'bookings') return openBookingPage(id);
      const row = page === 'paid' ? S.paid.find(x => x.id === id) : null;
      if (row?.booking) return openBookingPage(row.booking);
      const oid = page === 'paid' ? row?.order : id;
      if (oid) await openOrderPage(oid);
    }
    else if (e.target.closest('.pprint')) await printBill(id);
  });
}

function initHome() {
  $('#mainNav').onclick = run(async e => { const b = e.target.closest('button[data-nav]'); if (b) await navTo(b.dataset.nav); });
  $('#dClose').onclick = () => $('#dataDlg').close();
  $('#dSearch').oninput = renderData;
  $('#dataDlg').addEventListener('close', () => { dataKind = null; setNav(navKey()); });
  $('#dBody').onclick = run(async e => { const tr = e.target.closest('tr[data-order],tr[data-booking]'); if (!tr) return; $('#dataDlg').close(); if (tr.dataset.booking) await openBookingPage(+tr.dataset.booking, 'bookings'); else await openOrderPage(+tr.dataset.order); });   // a sale opens its order page
  initOrdersPage();
}

/* ======================================================================== */
/* ORDERS */
/* ======================================================================== */
/* orders.js — order lists (waiter side panel, cashier desk), selecting an order, and the new-order / add-items form. */

/* ---------- lists ---------- */
function orderRow(o, host) {   // waiter panel and cashier desk: a plain row, separated from the next by a thin line
  const b = document.createElement('button'); b.type = 'button'; b.className = `orow${S.order?.id === o.id ? ' on' : ''}`;
  b.innerHTML = `<span class="ro-top"><b>${esc(o.number)}</b><span class="pill ${o.status}">${statusLabel(o.status)}</span></span><b class="ro-amt">KSh ${money(o.totals.total)}</b>
    <span class="ro-sub"><span>${esc(whereOf(o))}</span><span>${plural(liveItems(o).length, 'item')}</span></span><span class="ro-sub ro-time"><span>${esc(spName(o.source))}${isCashier() ? ' · ' + esc(o.waiter_name) : ''}</span><span>${ago(o.created_at)}</span></span>`;
  b.onclick = () => selectOrder(o); host.append(b);
}

function paidRow(p) {   // cashier: a paid bill waiting for its receipt (or already printed: reprint), same flat row as an order
  const r = document.createElement('div'); r.className = 'orow paid';
  r.innerHTML = `<span class="ro-top"><b>${esc(p.order_number)}</b><span class="pill ${p.printed ? 'PAID' : 'WARN'}">${receiptText(p)}</span></span><b class="ro-amt">KSh ${money(p.total)}</b>
    <span class="ro-sub"><span>${esc(p.table || (p.source === 'BAR' ? 'Bar' : p.source === 'ROOM' ? 'Room booking' : 'No table'))}</span><span>${esc(p.waiter_name)}</span></span><span class="ro-sub ro-time"><span>${esc(p.number)}</span><span>paid ${timeOf(p.paid_at)}</span></span>`;
  const b = document.createElement('button'); b.type = 'button'; b.className = 'btn sm ' + (p.printed ? 'ghost' : 'primary'); b.textContent = p.printed ? 'Reprint receipt' : 'Print receipt';
  b.onclick = run(e => { e.stopPropagation(); return printBill(p.id); }); r.append(b); $('#orderList').append(r);
  r.classList.add('click'); r.onclick = run(() => p.booking ? openBookingPage(p.booking, 'paid') : openOrderPage(p.order));   // the row opens the bill's order page
}

function renderWaiterBar(rows) {   // cashier: narrow the list to one waiter's orders / bills
  const by = new Map(); rows.forEach(x => by.set(x.waiter, { name: x.waiter_name, n: (by.get(x.waiter)?.n || 0) + 1 }));
  if (by.size < 2 || !by.has(S.waiter)) S.waiter = 0;
  $('#waiterBar').innerHTML = by.size < 2 ? '' : [[0, 'All', rows.length], ...[...by].map(([id, v]) => [id, v.name, v.n])]
    .map(([id, name, n]) => `<button data-w="${id}" class="${S.waiter === id ? 'on' : ''}">${esc(name)} <i>${n}</i></button>`).join('');
  $$('#waiterBar button').forEach(b => b.onclick = () => { S.waiter = +b.dataset.w; renderOrders(); });
}

function renderCashierList() {
  const paidTab = S.filter === 'PAID', waiting = S.paid.filter(p => !p.printed).length;   // receipts still to print
  $('#filterSeg [data-f=PAID]').innerHTML = 'Paid' + (waiting ? ` <i class="badge">${waiting}</i>` : '');
  let list = paidTab ? S.paid : S.filter === 'ACTIVE' ? S.orders : S.orders.filter(o => o.status === S.filter);
  renderWaiterBar(list);
  if (S.waiter) list = list.filter(x => x.waiter === S.waiter);
  $('#orderList').innerHTML = list.length ? '' : `<div class="empty">${paidTab ? 'No paid bills on the open shifts yet' : 'No orders yet'}</div>`;
  list.forEach(x => paidTab ? paidRow(x) : orderRow(x, $('#orderList')));
  $('#cTitle').textContent = paidTab ? 'Paid bills' : 'Orders'; $('#cCount').textContent = list.length || '';
  $('#cSum').textContent = list.length ? `KSh ${money(list.reduce((t, x) => t + +(paidTab ? x.total : x.totals.total), 0))} ${paidTab ? 'collected' : 'on these orders'}` : '';
  $('#openN') && ($('#openN').textContent = S.orders.length || ''); $('#paidN') && ($('#paidN').textContent = waiting || '');   // tab badges
}

function renderOpenPanel() {   // waiter: the right-hand panel only exists while there are open orders
  const host = $('#openList'), n = S.orders.length; host.innerHTML = '';
  S.orders.forEach(o => orderRow(o, host));
  $('#openSum').textContent = n ? `KSh ${money(S.orders.reduce((t, o) => t + +o.totals.total, 0))} still open` : '';
  $('#openN').textContent = $('#openCount').textContent = n || '';
  $('#openPanel').classList.toggle('hidden', !n); $('#app').classList.toggle('has-orders', !!n);
}

const renderOrders = () => isCashier() ? renderCashierList() : renderOpenPanel();

/* ---------- selecting an order ---------- */
function selectOrder(o) { S.order = o; setMenu(false); renderOrders(); renderTicket(); openTicket(); }
function openTicket() { if (S.order) return openOrderPage(S.order.id); }   // waiters and cashiers: an order opens on its own page

/* ---------- order form ---------- */
let M = null;   // the open form: { order (set when adding to an existing order), start, sp (menu being browsed), cart }

async function openOrderForm(start, order = null) {
  await loadMenu().catch(() => { });   // fresh availability and portions left
  M = { order, start: order ? order.source : start, sp: order ? order.source : start, cart: [], catBy: {} };   // catBy remembers the category per selling point
  S.sp = M.start; S.cat = 0; S.q = ''; $('#q').value = '';
  $('#oTitle').textContent = order ? `Add items to ${order.number}` : 'New order';
  $('#oSub').textContent = order ? [order.table_name || 'No table', order.waiter_name].join(' · ') : '';
  $('#oFrom').textContent = spName(M.start);
  $('#oMeta').classList.toggle('hidden', !!order);
  $('#oCreate').textContent = order ? 'Add to order' : 'Create order';
  $('#oHint').textContent = order ? 'New items go to the kitchen and bar as soon as you add them.' : 'The order goes to the kitchen and bar as soon as you create it.';
  $('#oTable').innerHTML = '<option value="">No table</option>' + S.tables.map(t => `<option value="${t.id}">${esc(t.name)}${t.open_orders ? ` · ${t.open_orders} open` : ''}</option>`).join('');
  $('#oRoom').value = ''; $('#oNotes').value = '';
  syncTabs(); renderMenu(); renderForm(); $('#orderDlg').showModal();
}

const spMenu = () => { const st = SPS.find(s => s.key === M.sp).station; return S.menu.map(c => ({ ...c, items: c.items.filter(i => i.station === st) })).filter(c => c.items.length); };

/* selling-point tabs: built once, so the highlight can slide between them instead of being redrawn */
function buildTabs() {
  const t = $('#oSp'); t.style.setProperty('--n', SPS.length);
  t.innerHTML = '<span class="ind"></span>' + SPS.map(s => `<button type="button" role="tab" data-sp="${s.key}"><span>${s.name}</span><i class="badge"></i></button>`).join('');
  $$('button', t).forEach(b => b.onclick = () => switchSp(b.dataset.sp));
  t.onkeydown = e => {   // ← / → move between selling points
    const d = { ArrowRight: 1, ArrowLeft: -1 }[e.key]; if (!d || !M) return; e.preventDefault();
    const i = (SPS.findIndex(s => s.key === M.sp) + d + SPS.length) % SPS.length; switchSp(SPS[i].key); $$('button', t)[i].focus();
  };
}
function syncTabs() {
  $('#oSp').style.setProperty('--i', SPS.findIndex(s => s.key === M.sp));
  $$('#oSp button').forEach(b => { const on = b.dataset.sp === M.sp; b.classList.toggle('on', on); b.setAttribute('aria-selected', on); b.tabIndex = on ? 0 : -1; });
}
function switchSp(k) {
  if (!M || k === M.sp) return;
  M.catBy[M.sp] = S.cat; M.sp = k; S.cat = M.catBy[k] || 0; S.q = ''; $('#q').value = '';
  syncTabs(); renderMenu(true);
}

const replay = el => { el.classList.remove('swap'); void el.offsetWidth; el.classList.add('swap'); };   // restart the fade-in animation

function renderMenu(animate = false) {
  if (!M) return;
  const cats = spMenu(); if (S.cat >= cats.length) S.cat = 0;
  $('#cats').innerHTML = S.q ? '' : cats.map((c, i) => `<button type="button" data-i="${i}" class="${i === S.cat ? 'on' : ''}">${esc(c.name)}</button>`).join('');
  $$('#cats button').forEach(b => b.onclick = () => { S.cat = +b.dataset.i; renderMenu(true); });
  const items = S.q ? S.menu.flatMap(c => c.items).filter(m => m.name.toLowerCase().includes(S.q)) : (cats[S.cat]?.items || []);   // a search covers both selling points
  const g = $('#menu'); g.innerHTML = items.length ? '' : '<div class="empty">Nothing found</div>';
  items.forEach(m => {
    const b = document.createElement('button'); b.type = 'button'; b.className = 'mitem' + (m.available ? '' : ' off'); b.dataset.id = m.id;
    const meta = !m.available ? 'sold out' : [m.portions_left !== null && `${m.portions_left} left`, S.q && (m.station === 'BAR' ? 'bar' : 'kitchen')].filter(Boolean).join(' · ');
    b.innerHTML = `<span class="mn">${esc(m.name)}</span><span class="mf"><span class="pr">${money(m.price)}</span>${meta ? `<em>${meta}</em>` : ''}</span><span class="inc"></span>`;
    b.onclick = () => addToCart(m); g.append(b);
  });
  if (animate) { replay($('#cats')); replay(g); g.scrollTop = 0; }
  padGrid(); markCart();
}

/* the menu is a ruled grid: top up the last row with empty cells so every row is closed off by grid lines */
function padGrid() {
  const g = $('#menu'); $$('.fill', g).forEach(e => e.remove());
  const n = $$('.mitem', g).length; if (!n || !g.offsetWidth) return;   // hidden dialog: ResizeObserver calls this again once it is shown
  const cols = getComputedStyle(g).gridTemplateColumns.split(' ').length;
  for (let i = (cols - n % cols) % cols; i > 0; i--) { const d = document.createElement('div'); d.className = 'mitem fill'; d.setAttribute('aria-hidden', 'true'); g.append(d); }
}

/* show what is already in the order: ×N on each menu cell and a count on each selling-point tab */
function markCart() {
  if (!M) return;
  const qty = new Map(M.cart.map(l => [l.id, l.qty])), per = {};
  $$('#menu .mitem[data-id]').forEach(b => { const n = qty.get(+b.dataset.id) || 0; b.classList.toggle('in', !!n); $('.inc', b).textContent = n ? '×' + n : ''; });
  M.cart.forEach(l => { const k = SPS.find(s => s.station === l.station)?.key; per[k] = (per[k] || 0) + l.qty; });
  $$('#oSp button').forEach(b => $('.badge', b).textContent = per[b.dataset.sp] || '');
}

function addToCart(m) {
  if (!m.available) return toast(`${m.name} is sold out`, 'bad');
  const l = M.cart.find(x => x.id === m.id), qty = (l?.qty || 0) + 1;
  if (m.portions_left !== null && qty > m.portions_left) return toast(`Only ${m.portions_left} × ${m.name} left`, 'bad');
  if (qty > 500) return;
  if (l) l.qty = qty; else M.cart.push({ id: m.id, name: m.name, price: Number(m.price), tax: Number(m.tax_rate), station: m.station, left: m.portions_left, qty });
  renderForm();
}

function renderForm() {
  const L = $('#oLines'); L.innerHTML = M.cart.length ? '' : '<div class="empty">Tap items on the right to add them</div>';
  M.cart.forEach(l => {
    const r = document.createElement('div'); r.className = 'oline';
    r.innerHTML = `<span class="n">${esc(l.name)}<small>${l.station === 'BAR' ? 'Bar' : 'Kitchen'}</small></span><span class="step"><button type="button">−</button><input class="qin" type="number" min="1" max="500" inputmode="numeric" value="${l.qty}" aria-label="Quantity"><button type="button">+</button></span><span class="pr">${money(l.price)}</span><b class="sub">${money(l.price * l.qty)}</b><button type="button" class="x" title="Remove">✕</button>`;
    const [mi, pl, rm] = $$('button', r);
    mi.onclick = () => { if (l.qty > 1) l.qty--; else M.cart = M.cart.filter(x => x !== l); renderForm(); };
    pl.onclick = () => { if (l.left !== null && l.qty >= l.left) return toast(`Only ${l.left} × ${l.name} left`, 'bad'); if (l.qty < 500) l.qty++; renderForm(); };
    rm.onclick = () => { M.cart = M.cart.filter(x => x !== l); renderForm(); };
    const qi = $('.qin', r);   // type a quantity directly (e.g. 12) instead of tapping + repeatedly
    qi.onfocus = () => qi.select();
    qi.onchange = () => { let n = Math.max(1, Math.min(500, Math.round(+qi.value) || 1)); if (l.left !== null && n > l.left) { toast(`Only ${l.left} × ${l.name} left`, 'bad'); n = l.left; } l.qty = n; renderForm(); };
    L.append(r);
  });
  // a preview only: same rule as the server (VAT inside the price, or on top); the saved order's totals are the real ones
  const incl = !!S.me?.prices_include_tax, r2 = x => Math.round((x + Number.EPSILON) * 100) / 100; let net = 0, tax = 0;
  M.cart.forEach(l => { const line = l.price * l.qty, t = incl ? line * l.tax / (100 + l.tax) : line * l.tax / 100; tax += t; net += incl ? line - t : line; });
  $('#oTax').textContent = money(r2(tax)); $('#oTot').textContent = money(r2(net) + r2(tax));
  $('#oCreate').disabled = !M.cart.length;
  markCart();
}

async function submitForm() {
  if (!M?.cart.length) return toast('Add at least one item', 'bad');
  const btn = $('#oCreate'); if (btn.disabled) return; btn.disabled = true;   // one tap = one order, even on a double-tap
  try {
    const items = M.cart.map(l => ({ menu_item: l.id, quantity: l.qty })), adding = !!M.order, count = M.cart.reduce((n, l) => n + l.qty, 0);
    let o;
    if (adding) o = await api(`orders/${M.order.id}/items/bulk/`, 'POST', { items, version: M.order.version });
    else {
      const table = +$('#oTable').value, room = $('#oRoom').value.trim(), notes = $('#oNotes').value.trim();
      o = await api('orders/', 'POST', { source: M.start, items, notes, ...(table ? { table } : {}), ...(room ? { room_number: room } : {}) });
    }
    M.cart = []; $('#orderDlg').close();
    // the server created the order and sent it in one step: say where the items went
    const sent = (o.kots || []).map(k => `${k.station.toLowerCase()} ${k.number}`).join(', ');
    S.order = o; toast((adding ? `${plural(count, 'item')} added to ${o.number}` : `Order ${o.number} created`) + (sent ? ` · sent to ${sent}` : ''));
    await refresh(); openTicket();   // waiters land straight on the order's details
  } finally { btn.disabled = !M?.cart.length; }
}

const discardOk = () => !M?.cart.length || confirm('Discard this order? The items you added will be lost.');

function initOrders() {
  $('#q').oninput = e => { S.q = e.target.value.toLowerCase(); renderMenu(); };
  buildTabs(); new ResizeObserver(padGrid).observe($('#menu'));   // re-pad the grid when the dialog opens or is resized
  $('#oCreate').onclick = run(submitForm);
  $('#oClose').onclick = () => { if (discardOk()) $('#orderDlg').close(); };
  $('#orderDlg').addEventListener('cancel', e => { if (!discardOk()) e.preventDefault(); });   // Esc
  $('#orderDlg').addEventListener('close', () => { if (M) M.cart = []; });
  $('#addItems').onclick = run(() => S.order && openOrderForm(S.order.source, S.order));
  $$('#filterSeg button').forEach(b => b.onclick = () => { S.filter = b.dataset.f; $$('#filterSeg button').forEach(x => x.classList.toggle('on', x === b)); renderOrders(); });
}

/* ======================================================================== */
/* TICKET */
/* ======================================================================== */
/* ticket.js — the order ticket (lines, totals, action buttons) and what its buttons do. */

const ticketMeta = o => {
  const t = ago(o.created_at);
  return [`<span class="where">${esc(whereOf(o))}</span>`, o.room_number && o.table_name && `<span>Room ${esc(o.room_number)}</span>`, `<span>${esc(spName(o.source))}</span>`,
    `<span>${esc(o.waiter_name)}</span>`, `<span>${t === 'just now' ? 'Just opened' : `Opened ${t} ago`}</span>`].filter(Boolean).join('');
};

function renderTicket() {
  const o = S.order, st = o?.status, editable = o && ['OPEN', 'SENT'].includes(st), cashier = isCashier();
  $('#ticket').classList.toggle('empty', !o);
  $('#tTitle').textContent = o ? o.number : 'No order selected';
  $('#tSub').innerHTML = o ? ticketMeta(o) : '<span>Pick or start an order</span>';
  $('#tNote').classList.toggle('hidden', !o?.notes); $('#tNote').innerHTML = o?.notes ? `<b>Note</b> ${esc(o.notes)}` : '';
  $('#lCount').textContent = o ? plural(liveItems(o).length, 'item') : '';
  $('#addItems').classList.toggle('hidden', !(editable && !cashier));
  $('#tStatus').textContent = o ? statusLabel(st) : ''; $('#tStatus').className = 'pill ' + (st || '');
  $('#lines').innerHTML = o?.items.length ? '' : `<div class="empty">${cashier ? 'Pick an order from the list to bill it' : o ? 'No items yet. Tap “+ Add items”' : 'Pick an open order, or start a new one'}</div>`;
  (o?.items || []).forEach(i => $('#lines').append(ticketLine(i, editable && !cashier)));
  $('#vSub').textContent = money(o?.totals.subtotal); $('#vTax').textContent = money(o?.totals.tax); $('#vTot').textContent = 'KSh ' + money(o?.totals.total);
  renderActions(o);
}

function ticketLine(i, canEdit) {
  const d = document.createElement('div'); d.className = 'line ' + i.status;
  const editing = canEdit && i.status === 'PENDING';   // only reachable for items that somehow were not sent; sent items are voided, not edited
  const extra = [...(i.modifiers || []).map(m => m.name), i.notes && `“${i.notes}”`, i.status === 'VOID' && i.void_reason && `Void: ${i.void_reason}`].filter(Boolean);
  const tag = i.status === 'PENDING' ? '<span class="dot">not sent</span>' : i.status === 'VOID' ? '<span class="dot VOID">void</span>' : '';
  d.innerHTML = `<span class="q">${editing ? '' : i.quantity + '×'}</span>
    <span class="n"><b>${esc(i.name)}</b><small>${[i.station === 'BAR' ? 'Bar' : 'Kitchen', ...extra].map(x => `<span>${esc(x)}</span>`).join('')}${tag}</small></span>
    <span class="r"><b class="lt">${money(i.line_total)}</b></span>`;
  if (editing) {   // quantity stepper
    const step = document.createElement('span'); step.className = 'step';
    step.innerHTML = '<button type="button" aria-label="Fewer">−</button><b>' + i.quantity + '</b><button type="button" aria-label="More">+</button>';
    const [mi, pl] = $$('button', step);
    mi.onclick = run(() => i.quantity > 1 ? setQty(i, i.quantity - 1) : removeItem(i)); pl.onclick = run(() => setQty(i, i.quantity + 1)); $('.q', d).append(step);
  } else if (canEdit && i.status !== 'VOID') {   // already sent: voiding needs a reason (and a manager PIN)
    const x = document.createElement('button'); x.type = 'button'; x.className = 'vbtn'; x.textContent = 'Void'; x.setAttribute('aria-label', 'Void ' + i.name);
    x.onclick = run(() => removeItem(i)); $('.r', d).append(x);
  }
  return d;
}

/* ---------- action area ----------
   Open order  : [Merge / Split ▾] [Create bill]      (+ "Bill with a discount…")
   Billed order: [Take payment]   [Print bill] [Cancel bill]
   Items are sent to the kitchen / bar by the server when they are added, so there is no send button. */
let msOpen = false;   // is the Merge / Split menu open? remembered so the 8-second refresh doesn't snap it shut

function setMenu(open) {
  msOpen = open;
  $$('.menuwrap').forEach(w => { $('.menu', w).classList.toggle('hidden', !open); $('.mbtn', w).setAttribute('aria-expanded', open); if (open) $('[role=menuitem]:not(:disabled)', w)?.focus(); });
}

function mergeSplitMenu(o) {
  const others = S.orders.filter(x => x.id !== o.id && ['OPEN', 'SENT'].includes(x.status)), canSplit = liveItems(o).length > 1;
  const w = document.createElement('div'); w.className = 'menuwrap';
  w.innerHTML = `<button type="button" class="btn ghost mbtn" aria-haspopup="menu" aria-expanded="${msOpen}">Merge / Split<svg class="caret" viewBox="0 0 24 24" aria-hidden="true"><path d="M7 10l5 5 5-5z"/></svg></button>
    <div class="menu${msOpen ? '' : ' hidden'}" role="menu">
      <button type="button" role="menuitem" data-k="merge"${others.length ? '' : ' disabled'}><b>Merge orders</b><small>${others.length ? `Bring other open orders into ${esc(o.number)}` : 'No other open orders to merge'}</small></button>
      <button type="button" role="menuitem" data-k="split"${canSplit ? '' : ' disabled'}><b>Split items</b><small>${canSplit ? 'Move items to their own order and bill' : 'Needs at least two items'}</small></button>
    </div>`;
  const mb = $('.mbtn', w);
  mb.onclick = () => setMenu(!msOpen);
  $$('[data-k]', w).forEach(b => b.onclick = run(async () => { setMenu(false); await (b.dataset.k === 'merge' ? mergeOrders() : splitOrder()); }));
  w.onkeydown = e => {
    const items = $$('[role=menuitem]:not(:disabled)', w), at = items.indexOf(document.activeElement), step = { ArrowDown: 1, ArrowUp: -1 }[e.key];
    if (e.key === 'Escape' && msOpen) { e.preventDefault(); e.stopPropagation(); setMenu(false); mb.focus(); }
    else if (step) { e.preventDefault(); if (!msOpen) return setMenu(true); if (items.length) items[(at + step + items.length) % items.length].focus(); }
  };
  return w;
}

function renderActions(o, A = $('#actions')) {
  A.innerHTML = '';
  if (!o) return;
  const st = o.status, editable = ['OPEN', 'SENT'].includes(st), cashier = isCashier();
  const pending = o.items.some(i => i.status === 'PENDING'), live = o.items.some(i => i.status !== 'VOID');
  const el = (cls, html = '', tag = 'div') => { const e = document.createElement(tag); e.className = cls; e.innerHTML = html; return e; };
  const btn = (t, cls, fn, dis = false, host = A) => { const b = el('btn ' + cls, '', 'button'); b.type = 'button'; b.textContent = t; b.disabled = dis; b.onclick = run(fn); host.append(b); return b; };

  if (editable) {
    if (pending) {   // safety net only: items normally leave the moment they are added
      const a = el('alert', '<span>Some items have not reached the kitchen or bar.</span>');
      if (!cashier) btn('Send now', 'sm', sendOrder, false, a);
      A.append(a);
    }
    const canBill = !pending && live;   // waiters bill their own orders; cashiers can bill any
    const row = el('arow'); row.append(mergeSplitMenu(o)); btn('Create bill', 'primary', () => billOrder(false), !canBill, row); A.append(row);
    const disc = el('linkbtn', 'Bill with a discount…', 'button'); disc.type = 'button'; disc.disabled = !canBill; disc.onclick = run(() => billOrder(true)); A.append(disc);
    if (!live && !cashier) btn('Cancel order', 'danger', async () => { await api(`orders/${o.id}/cancel/`, 'POST', {}); S.order = null; await refresh(); });
  } else if (st === 'BILLED') {   // payment only exists once the bill does
    A.append(el('alert info', '<span>Bill created. Take payment to close this order.</span>'));
    btn('Take payment', 'success', () => openPay(o.bill_id));
    const row = el('arow eq'); btn('Print bill', 'ghost', () => printBill(o.bill_id), false, row); btn('Cancel bill', 'danger', cancelBill, false, row); A.append(row);   // anyone can print the unpaid bill; only the cashier prints the paid receipt
  }
}

/* ---------- actions ---------- */
async function setQty(i, n) { S.order = await api(`orders/${S.order.id}/items/${i.id}/`, 'PATCH', { quantity: n }); await refresh(); }

async function removeItem(i) {
  if (i.status === 'PENDING') await api(`orders/${S.order.id}/items/${i.id}/void/`, 'POST', {});
  else {
    const v = await ask('Void ' + i.name, [{ name: 'reason', label: 'Reason', req: true }, { name: 'wasted', label: 'Already made and thrown away', type: 'check' }, ...pinField()], 'Void item'); if (!v) return;
    await api(`orders/${S.order.id}/items/${i.id}/void/`, 'POST', v);
  }
  await refresh();
}

async function sendToStations() {   // no refresh here: callers decide when to redraw
  const d = await api(`orders/${S.order.id}/send/`, 'POST', {});
  toast('Sent: ' + d.kots.map(k => `${k.station.toLowerCase()} ${k.number}`).join(', '));
}
async function sendOrder() { await sendToStations(); await refresh(); }

async function billOrder(disc) {
  let body = {};
  if (disc) { body = await ask('Apply discount', [{ name: 'discount_percent', label: 'Discount %', type: 'number', req: true }, { name: 'discount_reason', label: 'Reason', req: true }, ...pinField()], 'Create bill'); if (!body) return; }
  const b = await api(`orders/${S.order.id}/bill/`, 'POST', body); toast(`Bill ${b.number} · ${money(b.total)}`); await refresh();
}

async function cancelBill() {
  const v = await ask('Cancel bill', [{ name: 'reason', label: 'Reason', req: true }, ...pinField()], 'Cancel bill'); if (!v) return;
  await api(`bills/${S.order.bill_id}/cancel/`, 'POST', v); await refresh();
}

async function mergeOrders() {
  const o = S.order, sameTable = x => o.table != null && x.table === o.table;
  const others = S.orders.filter(x => x.id !== o.id && ['OPEN', 'SENT'].includes(x.status)).sort((a, b) => sameTable(b) - sameTable(a) || a.id - b.id);   // same-table orders first
  const label = x => `${x.number} · ${whereOf(x)} · ${plural(liveItems(x).length, 'item')} · ${money(x.totals.total)}${isCashier() ? ' · ' + x.waiter_name : ''}`;
  const v = await ask(`Merge into ${o.number}`, [{ type: 'note', label: `Choose the orders to bring into ${o.number}. They will be billed together; same-table orders are listed first.` },
    ...others.map(x => ({ name: 'o' + x.id, label: label(x), type: 'check', pick: true }))], 'Merge');
  if (!v) return;
  const from = others.filter(x => v['o' + x.id]).map(x => x.id);
  if (!from.length) return toast('Tick the orders to merge', 'bad');
  S.order = await api(`orders/${o.id}/merge/`, 'POST', { from, version: o.version });
  toast(`${plural(from.length, 'order')} merged into ${o.number}`); await refresh();
}

async function splitOrder() {
  const o = S.order, lines = liveItems(o);
  const v = await ask(`Split ${o.number}`, [{ type: 'note', label: 'Choose the items to move to a new order with its own bill. At least one item has to stay.' },
    ...lines.map(i => ({ name: 'i' + i.id, label: `${i.quantity} × ${i.name} · ${money(i.line_total)}`, type: 'check', pick: true }))], 'Move to new order');
  if (!v) return;
  const items = lines.filter(i => v['i' + i.id]).map(i => i.id);
  if (!items.length) return toast('Tick the items to move', 'bad');
  if (items.length === lines.length) return toast('Leave at least one item on this order', 'bad');
  const d = await api(`orders/${o.id}/split/`, 'POST', { items, version: o.version });
  S.order = d.new_order; toast(`${plural(items.length, 'item')} moved to ${d.new_order.number}`); await refresh();
}

function initTicket() {
  document.addEventListener('click', e => { if (msOpen && !e.target.closest('.menuwrap')) setMenu(false); });
}

/* ======================================================================== */
/* ORDER PAGE */
/* ======================================================================== */
/* A full page for one order / paid bill, laid out like an invoice: who it is for and from, the items with VAT, the payments
   received, the totals, and a toolbar with everything you can do to it. It lives at #/order/<id> so Back, reload and links work.
   Opened from: the waiter's open-orders panel, the Open orders / Paid bills tables (both roles), and "My sales". */

const OPG = { id: null, d: null, from: 'home', pushed: false, seq: 0 };
const orderRoute = () => { const m = /^#\/order\/(\d+)$/.exec(location.hash); return m ? +m[1] : null; };
const clearOrderHash = () => { if (orderRoute()) history.replaceState(null, '', location.pathname + location.search); };
const bookingRoute = () => { const m = /^#\/booking\/(\d+)$/.exec(location.hash); return m ? +m[1] : null; };
const clearBookingHash = () => { if (bookingRoute()) history.replaceState(null, '', location.pathname + location.search); };
const stamp = iso => { const d = new Date(iso), p = n => String(n).padStart(2, '0'); return `${p(d.getDate())}-${p(d.getMonth() + 1)}-${d.getFullYear()} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`; };

async function openOrderPage(id) {
  if (page === 'order' && OPG.id === id) return loadOrderPage();   // already here (e.g. after adding items): just refresh it
  if (page !== 'order') OPG.from = page;
  OPG.pushed = true; location.hash = '#/order/' + id;   // the hashchange handler (onRoute) shows the page
}

async function onRoute() {   // the address changed: show / leave the order page or the booking page
  if (!S.me) return;
  const bid = bookingRoute();
  if (bid) {
    BPG.id = bid; BPG.d = null; showPage('booking'); $('#kpBody').innerHTML = '<p class="muted ipload">Loading…</p>'; $('#kpActions').innerHTML = '';
    try { await loadBookingPage(); } catch (e) { toast(e.message, 'bad'); leaveBookingPage(); }
    return;
  }
  if (page === 'booking') return leaveBookingPage();
  const id = orderRoute();
  if (id) {
    OPG.id = id; OPG.d = null; showPage('order'); $('#ipBody').innerHTML = '<p class="muted ipload">Loading…</p>'; $('#ipActions').innerHTML = '';
    try { await loadOrderPage(); } catch (e) { toast(e.message, 'bad'); leaveOrderPage(); }
  } else if (page === 'order') leaveOrderPage();
}

function leaveOrderPage() {
  const to = OPG.from === 'order' ? 'home' : OPG.from; OPG.id = null; OPG.d = null; OPG.pushed = false; setMenu(false);
  S.order = S.orders.find(o => o.id === S.order?.id) || null;   // a paid / closed order is not "selected" any more
  showPage(to); renderOrders(); renderTicket();
}

async function loadOrderPage() {
  const id = OPG.id, seq = ++OPG.seq, d = await api(`orders/${id}/page/`);
  if (seq !== OPG.seq || OPG.id !== id) return;   // a newer load, or the user already left
  OPG.d = d; S.order = d.order;   // the action buttons (bill, pay, merge …) work on S.order
  renderOrderPage();
}

const saleType = (b, pays) => {
  if (!b) return 'Open order';
  if (b.status !== 'PAID') return { OPEN: 'Awaiting payment', REFUNDED: 'Refunded', CANCELLED: 'Cancelled' }[b.status] || b.status;
  const m = [...new Set(pays.filter(p => p.kind === 'PAYMENT').map(p => METHODS[p.method] || p.method))];
  return (m.join(' + ') || 'Cash') + ' sale';
};

function renderOrderPage() {
  const d = OPG.d; if (!d || !S.me || page !== 'order') return;
  const o = d.order, b = d.bill, t = d.totals, biz = d.business, pays = d.payments, due = +t.balance > 0 && o.status !== 'CANCELLED';
  const status = b?.status === 'PAID' ? 'PAID' : o.status, itemsTotal = d.lines.reduce((n, l) => n + +l.line_total, 0);
  const row = (k, v, cls = '') => `<tr class="${cls}"><th>${k}</th><td>${v}</td></tr>`;
  const line = (k, v) => v ? `<p><b>${k}</b> ${esc(v)}</p>` : '';

  $('#ipPrepared').innerHTML = `Prepared by: <b>${esc(o.waiter_name)}</b>`;
  $('#ipBody').innerHTML = `
    <header class="inv-head"><div class="inv-brand">${esc(biz.name)}<span class="pill ${status}">${esc(status === 'PAID' ? 'paid' : statusLabel(status))}</span></div><small>Generated at: ${stamp(Date.now())}</small></header>
    <div class="inv-meta">
      <div><p class="lbl">To:</p><p><b>Walk-in client</b></p>${line('Table', o.table_name)}${line('Room', o.room_number)}${!o.table_name && !o.room_number && o.source === 'BAR' ? '<p>Bar counter</p>' : ''}${o.notes ? `<p class="inote"><b>Note</b> ${esc(o.notes)}</p>` : ''}</div>
      <div><p class="lbl">From:</p><p><b>${esc(biz.name)}</b></p>${[biz.address, biz.phone && 'Phone: ' + biz.phone, biz.email && 'Email: ' + biz.email].filter(Boolean).map(x => `<p>${esc(x)}</p>`).join('')}</div>
      <div class="inv-ref">
        <p class="big">${b ? `Bill #${esc(b.number)}` : `Order ${esc(o.number)}`}</p>
        ${biz.tax_id ? `<p>PIN: ${esc(biz.tax_id)}</p>` : ''}
        ${b ? `<p><b>Order:</b> ${esc(o.number)}</p>` : ''}
        <p><b>Date:</b> ${stamp(o.created_at)}</p>
        ${b?.paid_at ? `<p><b>Paid:</b> ${stamp(b.paid_at)}</p>` : ''}
        <p><b>Transaction type:</b> ${esc(saleType(b, pays))}</p>
        <p><b>Selling point:</b> ${esc(spName(o.source).toUpperCase())}</p>
        <p><b>Served by:</b> ${esc(o.waiter_name)}</p>
      </div>
    </div>
    <div class="inv-scroll"><table class="inv-items"><thead><tr><th>#</th><th>Product</th><th class="num">Qty</th><th class="num">Unit price</th><th class="num">Tax (VAT)</th><th class="num">Subtotal</th></tr></thead>
      <tbody>${d.lines.length ? d.lines.map((l, i) => `<tr><td>${i + 1}.</td><td><span class="pn">${esc(l.name)}</span><small>${[l.category, ...(l.modifiers || [])].filter(Boolean).map(esc).join(' · ')}</small></td><td class="num">${l.quantity}</td><td class="num">${money(l.unit_price)}</td><td class="num">${money(l.tax)}</td><td class="num">${money(l.line_total)}</td></tr>`).join('')
        : '<tr><td colspan="6" class="empty">No items on this order yet. Use “+ Add items”.</td></tr>'}</tbody></table></div>
    ${d.voided.length ? `<p class="inv-void"><b>Voided</b> ${d.voided.map(v => `${v.quantity} × ${esc(v.name)}${v.reason ? ' (' + esc(v.reason) + ')' : ''}`).join(', ')}</p>` : ''}
    <div class="inv-bottom">
      <section class="inv-pay"><h3>Payment(s) received</h3>
        ${pays.length ? `<div class="inv-scroll"><table><thead><tr><th>Date</th><th>Ref#</th><th>Payment mode</th><th class="num">Amount</th></tr></thead><tbody>${pays.map(p => `<tr>
          <td>${stamp(p.created_at)}</td><td>${esc(p.reference || '—')}</td>
          <td>${p.kind === 'REFUND' ? 'Refund · ' : ''}${esc(METHODS[p.method] || p.method)}<small>Received by: ${esc(p.by)}${p.tendered != null && p.kind === 'PAYMENT' && +p.change_due > 0 ? ` · tendered ${money(p.tendered)}, change ${money(p.change_due)}` : ''}</small></td>
          <td class="num${+p.amount < 0 ? ' neg' : ''}">${money(p.amount)}</td></tr>`).join('')}</tbody></table></div>`
          : `<p class="muted">${b?.status === 'OPEN' ? 'Nothing received yet. Take payment to close this order.' : 'No payments yet. Create the bill, then take payment.'}</p>`}
      </section>
      <table class="inv-totals"><tbody>
        ${row('Items total', money(itemsTotal))}
        ${+t.discount ? row('Discount' + (b?.discount_reason ? ` <small>${esc(b.discount_reason)}</small>` : ''), '− ' + money(t.discount)) : ''}
        ${+t.service_charge ? row('Service charge', money(t.service_charge)) : ''}
        ${row(d.prices_include_tax ? 'VAT (included)' : 'VAT', money(t.tax))}
        ${row('TOTAL', 'KES ' + money(t.total), 'grand')}
        ${row('Paid amount', 'KES ' + money(t.paid), 'paid')}
        ${row('Balance', 'KES ' + money(t.balance), due ? 'due' : 'clear')}
      </tbody></table>
    </div>`;
  renderPageActions();
}

/* The toolbar: the same actions the cashier's ticket has (merge / split, bill, discount, take payment, print bill, cancel),
   plus Add items for waiters and the two print buttons. Each one works on S.order, which this page keeps in step. */
function renderPageActions() {
  const d = OPG.d, o = d.order, b = d.bill, A = $('#ipActions'), cashier = isCashier(), editable = ['OPEN', 'SENT'].includes(o.status);
  renderActions(o, A);
  const add = (t, cls, fn, first = false) => { const x = document.createElement('button'); x.type = 'button'; x.className = 'btn ' + cls; x.textContent = t; x.onclick = run(fn); first ? A.prepend(x) : A.append(x); return x; };
  if (editable && !cashier) add('+ Add items', 'primary', () => openOrderForm(o.source, o), true);
  if (b?.status === 'PAID' && canPrint()) add('Print receipt', 'dark', () => printBill(b.id));
  add('Print (A4)', 'dark', async () => window.print());
}

function initOrderPage() {
  $('#ipBack').onclick = () => { if (OPG.pushed) history.back(); else leaveOrderPage(); };   // history.back() fires onRoute, which leaves the page
  window.addEventListener('hashchange', run(onRoute));
}

/* ======================================================================== */
/* ROOMS */
/* ======================================================================== */
/* rooms.js — Book room (room finder + booking form), the booking page, and the Online bookings placeholder (sample data).
   Billing and payment are the same bill + "Take payment" dialog the orders use; the server rules are in views.py (Rooms). */

const BOOK = { rooms: [], cart: [], total: 0, key: null };
const TODAY = () => ymd(Date.now());
const addDays = (iso, n) => { const d = new Date(iso + 'T12:00:00'); d.setDate(d.getDate() + n); return ymd(d); };
const nightsOf = (a, b) => Math.round((new Date(b + 'T12:00:00') - new Date(a + 'T12:00:00')) / 864e5);
const rate = v => Number(v).toLocaleString('en-KE', { maximumFractionDigits: 2 });
const r2 = x => Math.round((x + Number.EPSILON) * 100) / 100;
const REF_METHODS = ['MOBILE_MONEY', 'CARD', 'TRANSFER'];   // these need a reference (M-Pesa code, card slip, transfer no.)
const REF_HINT = { MOBILE_MONEY: 'M-Pesa confirmation code', CARD: 'Card slip / approval code', TRANSFER: 'Transfer reference / slip no.', VOUCHER: 'Voucher code (optional)' };
const CHECK = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 2a10 10 0 1 0 0 20 10 10 0 0 0 0-20zm-2 15-5-5 1.4-1.4L10 14.2l7.6-7.6L19 8z"/></svg>';

/* ---------- Book room: finder ---------- */
async function enterBook() {
  const t = TODAY(), from = $('#bkFrom');
  from.min = $('#bkTo').min = t;
  if (!from.value || from.value < t) from.value = t;
  if (!$('#bkTo').value || $('#bkTo').value <= from.value) $('#bkTo').value = addDays(from.value, 1);
  renderBookForm(); await loadRooms();
}

async function loadRooms() {   // the Search button, and any change of date
  const from = $('#bkFrom').value; let to = $('#bkTo').value;
  if (!from || !to) return toast('Pick both dates', 'bad');
  if (from < TODAY()) return toast('The From date cannot be in the past', 'bad');
  if (to <= from) { to = $('#bkTo').value = addDays(from, 1); toast('The To date must be after the From date. It is now ' + dmy(to)); }
  await fetchRooms();
}

async function fetchRooms() {
  const from = $('#bkFrom').value, to = $('#bkTo').value; if (!from || !to || to <= from) return;
  const d = await api(`rooms/?from=${from}&to=${to}`);
  BOOK.rooms = d.rooms;
  const gone = BOOK.cart.filter(l => BOOK.rooms.find(r => r.id === l.id)?.booked);   // someone else took it, or it is taken on the new dates
  if (gone.length) { BOOK.cart = BOOK.cart.filter(l => !gone.includes(l)); toast(`${gone.map(l => l.name).join(', ')} is not free on these dates and was removed from the form`, 'bad'); }
  renderRooms(); renderBookForm();
}

function renderRooms() {
  const scope = $('#bkScope').value, label = { free: 'unoccupied rooms', busy: 'occupied rooms', all: 'rooms' }[scope];
  const list = BOOK.rooms.filter(r => scope === 'all' || (scope === 'busy' ? r.booked : !r.booked && r.status !== 'MAINTENANCE'));
  $('#bkCount').textContent = `Total ${label}: ${list.length}`;
  $('#bkRooms').innerHTML = list.length ? list.map(r => {
    const inCart = BOOK.cart.some(l => l.id === r.id), plans = PLANS.filter(p => r.rates[p[0]]);
    const act = r.booked ? `<button type="button" class="btn ghost sm" data-view="${r.booking_id}">View booking</button>`
      : r.status === 'MAINTENANCE' ? '<button type="button" class="btn ghost sm" disabled>Unavailable</button>'
      : `<button type="button" class="btn primary sm bknow" data-add="${r.id}"${inCart ? ' disabled' : ''}>${inCart ? 'Added' : 'Book now'} ${CHECK}</button>`;
    const clean = r.status === 'CLEANING' ? `<button type="button" class="linkbtn" data-ready="${r.id}">Mark ready</button>` : '';
    return `<tr><td><span class="rname">${esc(r.name)}</span>${r.room_type ? `<span class="rtype">${esc(r.room_type)}</span>` : ''}
      ${r.note ? `<small class="rnote">(${esc(r.note)}) ${clean}</small>` : ''}${r.booked ? `<small>${esc(r.guest)} · until ${dmy(r.until)}</small>` : ''}</td>
      <td class="rates">${plans.length ? plans.map(p => `<span><b>${p[2]}:</b> ${rate(r.rates[p[0]])}</span>`).join('') : '<small>No rates set</small>'}</td><td class="num">${act}</td></tr>`;
  }).join('') : `<tr><td colspan="3" class="empty">No ${label} for these dates</td></tr>`;
}

function addRoom(id) {
  const r = BOOK.rooms.find(x => x.id === id); if (!r || BOOK.cart.some(l => l.id === id)) return;
  const plan = PLANS.find(p => r.rates[p[0]])?.[0];
  if (!plan) return toast(`${r.name} has no rates set. Ask a manager to add them`, 'bad');
  BOOK.cart.push({ id, name: r.name, type: r.room_type, rates: r.rates, plan });
  renderRooms(); renderBookForm();
}

/* ---------- Book room: form ---------- */
function renderBookForm() {
  const n = Math.max(1, nightsOf($('#bkFrom').value || TODAY(), $('#bkTo').value || addDays(TODAY(), 1)));
  const vatP = S.me?.room_vat_percent ?? 16, levyP = S.me?.room_levy_percent ?? 2, unit = l => +l.rates[l.plan];
  $('#bkNights').value = n;
  const L = $('#bkLines'); L.innerHTML = BOOK.cart.length ? '' : '<tr><td colspan="5" class="empty">Choose “Book now” beside a room to add it</td></tr>';
  BOOK.cart.forEach(l => {
    const tr = document.createElement('tr'); tr.dataset.id = l.id;
    tr.innerHTML = `<td><b>${esc(l.name)}</b>${l.type ? `<small>${esc(l.type)}</small>` : ''}</td>
      <td><select class="bplan" aria-label="Meal plan for ${esc(l.name)}">${PLANS.filter(p => l.rates[p[0]]).map(p => `<option value="${p[0]}"${p[0] === l.plan ? ' selected' : ''}>${p[1]}</option>`).join('')}</select></td>
      <td class="num">${money(unit(l))}</td><td class="num">${money(unit(l) * n)}</td>
      <td class="num"><button type="button" class="x" aria-label="Remove ${esc(l.name)}" title="Remove">✕</button></td>`;
    L.append(tr);
  });
  // same rule as the server: VAT on each line, the levy on the net (the saved bill is the real one)
  let net = 0, vat = 0; BOOK.cart.forEach(l => { const ln = r2(unit(l) * n); net += ln; vat += r2(ln * vatP / 100); });
  const levy = r2(net * levyP / 100); BOOK.total = r2(net + vat + levy);
  $('#bkVatP').textContent = vatP + '% VAT'; $('#bkLevyP').textContent = levyP + '%';
  $('#bkVat').textContent = money(vat); $('#bkLevy').textContent = money(levy); $('#bkTotal').textContent = money(BOOK.total);
  syncBookPay();
}

function syncBookPay() {
  const total = BOOK.total, paid = +$('#bkPaid').value || 0, m = $('#bkMethod').value, cash = m === 'CASH', needRef = REF_METHODS.includes(m);
  $('#bkBal').textContent = money(Math.max(0, total - paid)); $('#bkChg').textContent = money(cash ? Math.max(0, paid - total) : 0);
  $('#bkRefReq').classList.toggle('hidden', !needRef); $('#bkRef').placeholder = REF_HINT[m] || 'Reference';
  $('#bkSubmit').disabled = !BOOK.cart.length;
  $('#bkHint').textContent = !BOOK.cart.length ? 'Add a room to start' : paid > total && !cash ? 'Amount paid is more than the total'
    : !paid ? 'No payment now: the room is reserved and can be billed later' : paid < total ? 'Part payment: the balance stays on the bill' : 'Paid in full';
}

function clearBookForm() {
  BOOK.cart = []; BOOK.key = null;
  ['bkName', 'bkPhone', 'bkNote', 'bkPaid', 'bkRef'].forEach(id => $('#' + id).value = ''); $('#bkOcc').value = 1; $('#bkMethod').value = '';
  renderRooms(); renderBookForm();
}

async function submitBooking() {
  const btn = $('#bkSubmit'); if (btn.disabled) return;
  const name = $('#bkName').value.trim(), phone = $('#bkPhone').value.trim(), occ = +$('#bkOcc').value;
  const from = $('#bkFrom').value, to = $('#bkTo').value, paid = +$('#bkPaid').value || 0, m = $('#bkMethod').value, ref = $('#bkRef').value.trim();
  if (!BOOK.cart.length) return toast('Add at least one room', 'bad');
  if (!name) return toast('Enter the guest’s full name', 'bad');
  if (!phone) return toast('Enter the guest’s phone number', 'bad');
  if (!(occ >= 1)) return toast('Enter how many guests per room', 'bad');
  if (!from || !to || to <= from) return toast('Check the From and To dates', 'bad');
  let payment = null;
  if (paid > 0) {
    if (!m) return toast('Choose how the guest paid', 'bad');
    if (m !== 'CASH' && paid > BOOK.total) return toast('Amount paid is more than the total', 'bad');
    if (REF_METHODS.includes(m) && !ref) return toast('Enter the reference (M-Pesa code, card slip or transfer number)', 'bad');
    payment = { method: m === 'TRANSFER' ? 'CARD' : m, amount: Math.min(paid, BOOK.total).toFixed(2), reference: m === 'TRANSFER' ? 'TRANSFER ' + ref : ref,
      tendered: m === 'CASH' ? paid.toFixed(2) : null, idempotency_key: BOOK.key ||= crypto.randomUUID() };
  }
  btn.disabled = true;   // one tap = one booking
  try {
    const d = await api('bookings/', 'POST', { guest_name: name, phone, occupants: occ, narrative: $('#bkNote').value.trim(), check_in: from, check_out: to,
      rooms: BOOK.cart.map(l => ({ room: l.id, meal_plan: l.plan })), ...(payment ? { payment } : {}) });
    toast(`Booking ${d.booking.number} created` + (payment ? ` · KSh ${money(payment.amount)} received` : ''));
    clearBookForm(); await refresh(); await openBookingPage(d.booking.id, 'bookings');
  } finally { syncBookPay(); }
}

/* "Find customer": reuse a guest from an earlier booking */
let GUESTS = [];
function initGuests() {
  const dlg = $('#guestDlg'); let t;
  $('#bkFind').onclick = () => { $('#gSearch').value = ''; $('#gList').innerHTML = '<p class="muted gnote">Type at least 2 letters of a name or phone number.</p>'; dlg.showModal(); $('#gSearch').focus(); };
  $('#gClose').onclick = () => dlg.close();
  $('#gSearch').oninput = e => {
    clearTimeout(t); const q = e.target.value.trim();
    if (q.length < 2) { $('#gList').innerHTML = '<p class="muted gnote">Type at least 2 letters of a name or phone number.</p>'; return; }
    t = setTimeout(run(async () => {
      GUESTS = await api('rooms/guests/?q=' + encodeURIComponent(q));
      $('#gList').innerHTML = GUESTS.length ? GUESTS.map((g, i) => `<button type="button" class="gitem" data-i="${i}"><b>${esc(g.guest_name)}</b><small>${esc(g.phone)}${g.narrative ? ' · ' + esc(g.narrative) : ''}</small></button>`).join('') : '<p class="muted gnote">No earlier guest matches.</p>';
    }), 250);
  };
  $('#gList').onclick = e => {
    const b = e.target.closest('[data-i]'); if (!b) return; const g = GUESTS[+b.dataset.i];
    $('#bkName').value = g.guest_name; $('#bkPhone').value = g.phone; $('#bkNote').value = g.narrative || ''; $('#bkOcc').value = g.occupants || 1; dlg.close();
  };
}

/* ---------- booking badges ---------- */
function renderBookingBadges() {
  const due = S.bookings.filter(b => b.status === 'RESERVED' && b.check_in <= TODAY()).length;   // guests due in
  $('#bookN') && ($('#bookN').textContent = due || ''); $('#onlineN') && ($('#onlineN').textContent = ONLINE.filter(o => o.status === 'NEW').length || '');
}

/* ---------- booking page ---------- */
const BPG = { id: null, d: null, from: 'bookings', pushed: false, seq: 0 };

async function openBookingPage(id, from) {
  if (page === 'booking' && BPG.id === id) return loadBookingPage();   // already here: just refresh it
  if (page !== 'booking') BPG.from = from || page;
  BPG.pushed = true; location.hash = '#/booking/' + id;   // the hashchange handler (onRoute) shows the page
}

function leaveBookingPage() {
  const to = BPG.from === 'booking' ? 'bookings' : BPG.from; BPG.id = null; BPG.d = null; BPG.pushed = false;
  showPage(to);
}

async function loadBookingPage() {
  const id = BPG.id, seq = ++BPG.seq, d = await api(`bookings/${id}/`);
  if (seq !== BPG.seq || BPG.id !== id) return;   // a newer load, or the user already left
  BPG.d = d; renderBookingPage();
}

function renderBookingPage() {
  const d = BPG.d; if (!d || !S.me || page !== 'booking') return;
  const b = d.booking, bill = d.bill, t = d.totals, biz = d.business, pays = d.payments, due = +t.balance > 0 && b.status !== 'CANCELLED';
  const row = (k, v, cls = '') => `<tr class="${cls}"><th>${k}</th><td>${v}</td></tr>`, line = (k, v) => v ? `<p><b>${k}</b> ${esc(v)}</p>` : '';
  const txn = !bill ? 'Not billed yet' : bill.status === 'PAID' ? [...new Set(pays.filter(p => p.kind === 'PAYMENT').map(p => METHODS[p.method] || p.method))].join(' + ') + ' sale'
    : { OPEN: 'Awaiting payment', REFUNDED: 'Refunded', CANCELLED: 'Cancelled' }[bill.status] || bill.status;
  $('#kpPrepared').innerHTML = `Booked by: <b>${esc(b.waiter_name)}</b>`;
  $('#kpBody').innerHTML = `
    <header class="inv-head"><div class="inv-brand">${esc(biz.name)}<span class="pill ${b.status}">${bookingStatus(b.status)}</span><span class="pill P-${b.pay_state}">${payState(b.pay_state)}</span></div><small>Generated at: ${stamp(Date.now())}</small></header>
    <div class="inv-meta">
      <div><p class="lbl">Guest:</p><p><b>${esc(b.guest_name)}</b></p>${line('Phone', b.phone)}${line('Guests per room', b.occupants)}${b.narrative ? `<p class="inote"><b>Note</b> ${esc(b.narrative)}</p>` : ''}${b.status === 'CANCELLED' && b.cancel_reason ? `<p class="inote"><b>Cancelled</b> ${esc(b.cancel_reason)}</p>` : ''}</div>
      <div><p class="lbl">From:</p><p><b>${esc(biz.name)}</b></p>${[biz.address, biz.phone && 'Phone: ' + biz.phone, biz.email && 'Email: ' + biz.email].filter(Boolean).map(x => `<p>${esc(x)}</p>`).join('')}</div>
      <div class="inv-ref">
        <p class="big">Booking ${esc(b.number)}</p>
        ${bill ? `<p><b>Bill:</b> #${esc(bill.number)}</p>` : ''}${biz.tax_id ? `<p>PIN: ${esc(biz.tax_id)}</p>` : ''}
        <p><b>Check-in:</b> ${dmy(b.check_in)}</p><p><b>Check-out:</b> ${dmy(b.check_out)} (${plural(b.nights, 'night')})</p>
        <p><b>Booked:</b> ${stamp(b.created_at)}</p>${bill?.paid_at ? `<p><b>Paid:</b> ${stamp(bill.paid_at)}</p>` : ''}
        <p><b>Transaction type:</b> ${esc(txn)}</p>
      </div>
    </div>
    <div class="inv-scroll"><table class="inv-items"><thead><tr><th>#</th><th>Room</th><th>Meal plan</th><th class="num">Nights</th><th class="num">Price per night</th><th class="num">VAT</th><th class="num">Subtotal</th></tr></thead>
      <tbody>${d.lines.map((l, i) => `<tr><td>${i + 1}.</td><td><span class="pn">${esc(l.room)}</span></td><td>${esc(planName(l.meal_plan))}</td><td class="num">${l.quantity}</td><td class="num">${money(l.unit_price)}</td><td class="num">${money(l.tax)}</td><td class="num">${money(l.line_total)}</td></tr>`).join('')}</tbody></table></div>
    <div class="inv-bottom">
      <section class="inv-pay"><h3>Payment(s) received</h3>
        ${pays.length ? `<div class="inv-scroll"><table><thead><tr><th>Date</th><th>Ref#</th><th>Payment mode</th><th class="num">Amount</th></tr></thead><tbody>${pays.map(p => `<tr>
          <td>${stamp(p.created_at)}</td><td>${esc(p.reference || '—')}</td>
          <td>${p.kind === 'REFUND' ? 'Refund · ' : ''}${esc(METHODS[p.method] || p.method)}<small>Received by: ${esc(p.by)}${p.tendered != null && p.kind === 'PAYMENT' && +p.change_due > 0 ? ` · tendered ${money(p.tendered)}, change ${money(p.change_due)}` : ''}</small></td>
          <td class="num${+p.amount < 0 ? ' neg' : ''}">${money(p.amount)}</td></tr>`).join('')}</tbody></table></div>`
          : `<p class="muted">${bill?.status === 'OPEN' ? 'Nothing received yet. Take payment to settle this stay.' : b.status === 'CANCELLED' ? 'This booking was cancelled.' : 'No payments yet. Create the bill, then take payment.'}</p>`}
      </section>
      <table class="inv-totals"><tbody>
        ${row('Rooms total', money(t.subtotal))}${row(`VAT <small>${d.vat_percent}%</small>`, money(t.tax))}${+t.levy ? row(`Catering levy <small>${d.levy_percent}%</small>`, money(t.levy)) : ''}
        ${row('TOTAL', 'KES ' + money(t.total), 'grand')}${row('Paid amount', 'KES ' + money(t.paid), 'paid')}${row('Balance', 'KES ' + money(t.balance), due ? 'due' : 'clear')}
      </tbody></table>
    </div>`;
  renderBookingActions();
}

function renderBookingActions() {
  const d = BPG.d, b = d.booking, bill = d.bill, A = $('#kpActions'); A.innerHTML = '';
  const live = ['RESERVED', 'CHECKED_IN'].includes(b.status);
  const add = (t, cls, fn, dis = false) => { const x = document.createElement('button'); x.type = 'button'; x.className = 'btn ' + cls; x.textContent = t; x.disabled = dis; x.onclick = run(fn); A.append(x); return x; };
  const note = (cls, msg) => A.insertAdjacentHTML('beforeend', `<div class="alert ${cls}"><span>${esc(msg)}</span></div>`);
  if (live && !bill) note('', 'Not billed yet. Create the bill to take payment.');
  else if (bill?.status === 'OPEN') note('info', `Bill created. Balance to pay: KSh ${money(bill.balance ?? d.totals.balance)}.`);
  else if (b.status === 'CHECKED_IN') note('info', 'Guest is in the room. Check out when they leave.');
  if (live && !bill) add('Create bill', 'primary', () => bookingAct('bill', 'Bill created'));
  if (bill?.status === 'OPEN') { add('Take payment', 'success', () => openPay(bill.id)); add('Print bill', 'ghost', () => printBill(bill.id)); add('Cancel bill', 'danger', cancelRoomBill); }
  if (b.status === 'RESERVED') add('Check in', 'primary', () => bookingAct('check-in', `${b.guest_name} checked in`));
  if (b.status === 'CHECKED_IN') add('Check out', 'primary', () => bookingAct('check-out', `${b.guest_name} checked out`), bill?.status !== 'PAID');
  if (b.status === 'RESERVED' && !bill) add('Cancel booking', 'danger', cancelBooking);
  if (bill?.status === 'PAID' && canPrint()) add('Print receipt', 'dark', () => printBill(bill.id));
  add('Print (A4)', 'dark', async () => window.print());
}

async function bookingAct(path, msg) { await api(`bookings/${BPG.id}/${path}/`, 'POST', {}); toast(msg); await refresh(); }

async function cancelRoomBill() {
  const v = await ask('Cancel bill', [{ name: 'reason', label: 'Reason', req: true }, ...pinField()], 'Cancel bill'); if (!v) return;
  await api(`bills/${BPG.d.bill.id}/cancel/`, 'POST', v); toast('Bill cancelled'); await refresh();
}

async function cancelBooking() {
  const v = await ask('Cancel booking ' + BPG.d.booking.number, [{ name: 'reason', label: 'Reason', req: true }], 'Cancel booking'); if (!v) return;
  await api(`bookings/${BPG.id}/cancel/`, 'POST', v); toast('Booking cancelled'); await refresh();
}

/* ---------- Online bookings: SAMPLE DATA ----------
   Nothing here talks to the server. When online booking is built, replace ONLINE with an api('online-bookings/') call in renderOnline()
   and enable the Accept / Decline buttons. `in` = days from today. */
const ONLINE = [
  { ref: 'WEB-10482', guest: 'Amina Hassan', channel: 'Website', room: 'Executive', in: 1, nights: 2, guests: 2, amount: 12400, status: 'NEW' },
  { ref: 'BKG-77310', guest: 'Daniel Otieno', channel: 'Booking.com', room: 'Superior', in: 3, nights: 3, guests: 1, amount: 14200, status: 'NEW' },
  { ref: 'WEB-10479', guest: 'Grace Wambui', channel: 'Website', room: 'Studio', in: 5, nights: 1, guests: 2, amount: 4640, status: 'CONFIRMED' },
  { ref: 'EXP-55821', guest: 'Peter Schmidt', channel: 'Expedia', room: 'Executive', in: 8, nights: 4, guests: 2, amount: 31800, status: 'AWAITING_PAYMENT' },
  { ref: 'WEB-10471', guest: 'Faith Njeri', channel: 'Website', room: 'Superior', in: 10, nights: 2, guests: 3, amount: 9280, status: 'CONFIRMED' },
  { ref: 'BKG-77254', guest: 'Ahmed Yusuf', channel: 'Booking.com', room: 'Studio', in: 14, nights: 5, guests: 2, amount: 23200, status: 'CANCELLED' },
];
const onlineStatus = s => ({ NEW: 'new', CONFIRMED: 'confirmed', AWAITING_PAYMENT: 'awaiting payment', CANCELLED: 'cancelled' }[s]);
const ONLINE_COLS = [
  ['ref', 'Reference', o => o.ref], ['guest', 'Guest', o => o.guest], ['channel', 'Channel', o => o.channel], ['room', 'Room type', o => o.room],
  ['in', 'Check-in', o => addDays(TODAY(), o.in), o => dmy(addDays(TODAY(), o.in))], ['out', 'Check-out', o => addDays(TODAY(), o.in + o.nights), o => dmy(addDays(TODAY(), o.in + o.nights))],
  ['guests', 'Guests', o => o.guests, null, 'num'], ['amount', 'Amount', o => o.amount, o => money(o.amount), 'num strong'],
  ['status', 'Status', o => onlineStatus(o.status), o => `<span class="pill ${o.status}">${onlineStatus(o.status)}</span>`],
  ['act', '', null, o => o.status !== 'NEW' ? '' : '<button type="button" class="btn primary sm" disabled title="Available once online booking is connected">Accept</button> <button type="button" class="btn ghost sm" disabled title="Available once online booking is connected">Decline</button>', 'num'],
];

function renderOnline() {
  const q = $('#onSearch').value.trim().toLowerCase(), n = s => ONLINE.filter(o => o.status === s).length;
  $('#onStats').innerHTML = [['New', n('NEW')], ['Confirmed', n('CONFIRMED')], ['Awaiting payment', n('AWAITING_PAYMENT')], ['Value of live bookings', 'KSh ' + money(ONLINE.filter(o => o.status !== 'CANCELLED').reduce((t, o) => t + o.amount, 0))]]
    .map(([k, v]) => `<div class="stat"><small>${k}</small><b>${v}</b></div>`).join('');
  $('#onHead').innerHTML = '<tr>' + ONLINE_COLS.map(c => `<th class="${c[4]?.includes('num') ? 'num' : ''}">${c[1]}</th>`).join('') + '</tr>';
  const rows = ONLINE.filter(o => !q || ONLINE_COLS.filter(c => c[2]).map(c => c[2](o)).join(' ').toLowerCase().includes(q));
  $('#onBody').innerHTML = rows.length ? rows.map(o => '<tr>' + ONLINE_COLS.map(c => `<td class="${c[4] || ''}">${c[3] ? c[3](o) : esc(c[2](o))}</td>`).join('') + '</tr>').join('')
    : `<tr><td colspan="${ONLINE_COLS.length}" class="empty">No matches</td></tr>`;
  $('#onInfo').textContent = `Showing ${plural(rows.length, 'sample booking')}`;
}

function initRooms() {
  initGuests();
  $('#bkScope').onchange = renderRooms;
  $('#bkSearch').onclick = run(loadRooms);
  $('#bkFrom').onchange = run(async () => { if ($('#bkTo').value <= $('#bkFrom').value) $('#bkTo').value = addDays($('#bkFrom').value, 1); await loadRooms(); });
  $('#bkTo').onchange = run(loadRooms);
  $('#bkRooms').onclick = run(async e => {
    const add = e.target.closest('[data-add]'), view = e.target.closest('[data-view]'), ready = e.target.closest('[data-ready]');
    if (add) addRoom(+add.dataset.add);
    else if (view) await openBookingPage(+view.dataset.view, 'book');
    else if (ready) { await api(`rooms/${ready.dataset.ready}/status/`, 'POST', { status: 'AVAILABLE' }); toast('Room marked ready'); await fetchRooms(); }
  });
  $('#bkLines').onchange = e => { const sel = e.target.closest('.bplan'); if (!sel) return; const l = BOOK.cart.find(x => x.id === +sel.closest('tr').dataset.id); if (l) { l.plan = sel.value; renderBookForm(); } };
  $('#bkLines').onclick = e => { const x = e.target.closest('.x'); if (!x) return; BOOK.cart = BOOK.cart.filter(l => l.id !== +x.closest('tr').dataset.id); renderRooms(); renderBookForm(); };
  ['bkPaid', 'bkMethod'].forEach(id => $('#' + id).addEventListener('input', syncBookPay));
  $('#bkClear').onclick = () => { if (!BOOK.cart.length || confirm('Clear the booking form?')) clearBookForm(); };
  $('#bkForm').addEventListener('submit', run(async e => { e.preventDefault(); await submitBooking(); }));
  $('#kpBack').onclick = () => { if (BPG.pushed) history.back(); else leaveBookingPage(); };   // history.back() fires onRoute, which leaves the page
  $('#onSearch').oninput = renderOnline;
}

/* ======================================================================== */
/* PAYMENT */
/* ======================================================================== */
/* payment.js — the "Take payment" dialog: cash (tendered / change), M-Pesa (STK push or till number),
   card (machine slip or bank transfer) and vouchers; part payments. */

const STK_WAIT_MS = 120000, STK_POLL_MS = 3000;
const phoneOf = raw => { const m = /^(?:\+?254|0)?([17]\d{8})$/.exec(String(raw).replace(/[\s\-()]/g, '')); return m ? '254' + m[1] : null; };   // 0712…, +254712…, 254712… → 254712…
const tillCode = () => $('#tillRef').value.trim().toUpperCase();
const TILL_CODE = /^[A-Z0-9]{10}$/;   // an M-Pesa confirmation code is 10 letters / digits

async function openPay(billId = S.order?.bill_id) {   // any bill: an order's, or a room booking's
  stopStk();
  payBill = await api('bills/' + billId + '/'); payKey = crypto.randomUUID(); S.pm = 'CASH';
  payCfg = await api('payments/config/').catch(() => ({ mpesa: {}, bank: {} }));
  S.mpMode = payCfg.mpesa?.stk ? 'STK' : 'TILL'; S.cardMode = 'CARD';
  $('#payBal').textContent = 'KSh ' + money(payBill.balance); $('#payAmt').value = payBill.balance; $('#payTen').value = '';
  ['tillRef', 'cardRef', 'vchRef', 'stkPhone'].forEach(id => $('#' + id).value = '');
  $('#tillNo').textContent = payCfg.mpesa?.till || 'Not set';
  $('#bkName').textContent = payCfg.bank?.name || '—'; $('#bkAccName').textContent = payCfg.bank?.account_name || '—'; $('#bkAccNo').textContent = payCfg.bank?.account_number || 'Not set';
  $('#quick').innerHTML = [0, 500, 1000, 2000, 5000].map(v => `<button type="button" data-v="${v}">${v ? v : 'Exact'}</button>`).join('');
  $$('#quick button').forEach(b => b.onclick = () => { $('#payTen').value = +b.dataset.v || $('#payAmt').value; syncPay(); });
  syncPay(); $('#payDlg').showModal();
}

function syncPay() {
  const m = S.pm, waiting = !!stk?.pending, stkMode = S.mpMode === 'STK', trf = S.cardMode === 'TRANSFER';
  $$('#methods button').forEach(b => { b.classList.toggle('on', b.dataset.m === m); b.disabled = waiting && b.dataset.m !== m; });   // no switching away while a customer is being prompted
  [['cashBox', 'CASH'], ['mpBox', 'MOBILE_MONEY'], ['cardBox', 'CARD'], ['vchBox', 'VOUCHER']].forEach(([id, k]) => $('#' + id).classList.toggle('hidden', m !== k));
  $$('#mpMode button').forEach(b => {
    const off = b.dataset.mp === 'STK' && !payCfg.mpesa?.stk;
    b.classList.toggle('on', b.dataset.mp === S.mpMode); b.disabled = waiting || off; b.title = off ? 'STK push is not set up yet' : '';
  });
  $('#mpStk').classList.toggle('hidden', !stkMode); $('#mpTill').classList.toggle('hidden', stkMode);
  $$('#cardMode button').forEach(b => b.classList.toggle('on', b.dataset.cm === S.cardMode));
  $('#trfBox').classList.toggle('hidden', !trf);
  $('#cardRefLbl').textContent = trf ? 'Transfer reference / bank slip no.' : 'Card slip / approval code';
  $('#payAmt').readOnly = !!stk;   // the amount is fixed once the push has been sent
  $('#payChg').textContent = money(Math.max(0, ($('#payTen').value || $('#payAmt').value) - $('#payAmt').value));
  const ready = m === 'MOBILE_MONEY' ? (stkMode ? !!stk?.success : TILL_CODE.test(tillCode())) : m === 'CARD' ? !!$('#cardRef').value.trim() : true;
  $('#payOk').disabled = !ready; $('#payOk').textContent = m === 'MOBILE_MONEY' && stkMode && !stk?.success ? 'Waiting for M-Pesa…' : 'Confirm payment';
}

/* ---------- STK push ---------- */
function stkUI(kind, msg = '') {   // idle | wait | ok | bad
  const box = $('#stkStatus'); box.className = 'stkstat ' + kind + (kind === 'idle' ? ' hidden' : '');
  box.innerHTML = kind === 'idle' ? '' : (kind === 'wait' ? '<span class="spin" aria-hidden="true"></span>' : '') + `<span>${esc(msg)}</span>` + (kind === 'wait' ? '<button type="button" class="linkbtn" id="stkStop">Cancel request</button>' : '');
  $('#stkStop') && ($('#stkStop').onclick = () => { stopStk(); syncPay(); });
  $('#stkSend').classList.toggle('hidden', kind === 'wait' || kind === 'ok'); $('#stkSend').textContent = kind === 'bad' ? 'Resend STK push' : 'Send STK push';
  $('#stkPhone').readOnly = kind === 'wait' || kind === 'ok';
}

function stopStk() { if (stk?.timer) clearTimeout(stk.timer); stk = null; stkUI('idle'); }

async function sendStk() {
  const phone = phoneOf($('#stkPhone').value), amt = +$('#payAmt').value;
  if (!phone) return toast('Enter a valid Safaricom number, e.g. 0712 345 678', 'bad');
  if (!(amt > 0) || amt > +payBill.balance) return toast('Check the amount: it must be more than 0 and no more than the balance', 'bad');
  if (!Number.isInteger(amt)) return toast('STK push needs a whole-shilling amount. Use the till number to pay cents', 'bad');
  const btn = $('#stkSend'); btn.disabled = true;
  try {
    const d = await api('payments/mpesa/stk/', 'POST', { bill: payBill.id, amount: amt, phone });
    stk = { id: d.checkout_id, pending: true, success: false, until: Date.now() + STK_WAIT_MS };
    stkUI('wait', `Waiting for +${d.phone} to enter their M-Pesa PIN…`); syncPay(); pollStk();
  } finally { btn.disabled = false; }
}

function pollStk() {
  const s = stk; if (!s) return;
  s.timer = setTimeout(async () => {
    if (stk !== s) return;
    try {
      const d = await api('payments/mpesa/stk/' + encodeURIComponent(s.id) + '/');
      if (stk !== s) return;
      if (d.status === 'SUCCESS') { s.pending = false; s.success = true; stkUI('ok', `Payment received · ${d.receipt}`); syncPay(); return run(confirmPay)(); }   // the money is in: record it straight away
      if (d.status === 'FAILED' || d.status === 'UNKNOWN') { stk = null; stkUI('bad', d.message || 'The request expired. Resend it, or use the till number'); syncPay(); return; }
    } catch { /* a dropped connection: keep waiting */ }
    if (stk !== s) return;
    if (Date.now() > s.until) { stk = null; stkUI('bad', 'No reply from the customer yet. Ask them to check their phone, then resend or use the till number'); syncPay(); return; }
    pollStk();
  }, STK_POLL_MS);
}

async function confirmPay() {
  const m = S.pm, stkMode = m === 'MOBILE_MONEY' && S.mpMode === 'STK';
  const reference = m === 'MOBILE_MONEY' ? (stkMode ? '' : tillCode())
    : m === 'CARD' ? (S.cardMode === 'TRANSFER' ? 'TRANSFER ' : '') + $('#cardRef').value.trim()
      : m === 'VOUCHER' ? $('#vchRef').value.trim() : '';
  const d = await api(`bills/${payBill.id}/pay/`, 'POST', { method: m, amount: $('#payAmt').value, reference, ...(stkMode ? { stk_checkout_id: stk?.id } : {}),
    tendered: m === 'CASH' ? ($('#payTen').value || $('#payAmt').value) : null, idempotency_key: payKey });
  $('#payDlg').close();
  if (d.status !== 'PAID') { toast(`Part-paid · balance ${money(d.balance)}`); await refresh(); return openPay(payBill.id); }
  await refresh();   // a paid order leaves the open list; for waiters this also closes the ticket modal
  const note = canPrint() ? '' : ' · the cashier will print the receipt';
  if (await donePrompt(`${d.number} paid in full · change due KSh ${money(d.change_due)}${note}`) === 'print') await printBill(d.id);
}

function donePrompt(msg) {
  return new Promise(res => {
    const d = $('#doneDlg'); $('#doneMsg').textContent = msg; d.returnValue = '';
    $('[value=print]', d).classList.toggle('hidden', !canPrint());   // waiters can't print
    d.showModal(); (canPrint() ? $('[value=print]', d) : $('[value=done]', d)).focus(); d.onclose = () => res(d.returnValue);
  });
}

async function printBill(id) {
  const r = await fetch(`/api/bills/${id}/receipt/`, { credentials: 'same-origin' });
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || 'Could not load the receipt');
  const f = document.createElement('iframe'); f.style.cssText = 'position:fixed;right:0;bottom:0;width:0;height:0;border:0';
  f.srcdoc = await r.text(); f.onload = () => { f.contentWindow.focus(); f.contentWindow.print(); setTimeout(() => f.remove(), 60000); }; document.body.append(f);
  if (isCashier()) refresh().catch(() => { });   // the Paid tab now shows it as printed
}

function initPayment() {
  $$('#methods button').forEach(b => b.onclick = () => { S.pm = b.dataset.m; syncPay(); });
  $$('#mpMode button').forEach(b => b.onclick = () => { S.mpMode = b.dataset.mp; syncPay(); });
  $$('#cardMode button').forEach(b => b.onclick = () => { S.cardMode = b.dataset.cm; syncPay(); });
  ['payAmt', 'payTen', 'cardRef'].forEach(id => $('#' + id).oninput = syncPay);
  $('#tillRef').oninput = e => { e.target.value = e.target.value.toUpperCase().replace(/[^A-Z0-9]/g, ''); syncPay(); };
  $('#stkSend').onclick = run(sendStk);
  $('#payOk').onclick = run(confirmPay);
  $('#payDlg').addEventListener('click', e => {   // copy the till / account number
    const b = e.target.closest('[data-copy]'); const v = b && $('#' + b.dataset.copy).textContent;
    if (v && v !== 'Not set') navigator.clipboard?.writeText(v).then(() => toast('Copied ' + v), () => toast('Could not copy', 'bad'));
  });
  $('#payForm').addEventListener('keydown', e => {   // Enter must not fall through to the Cancel button (the form's default submit)
    if (e.key !== 'Enter' || e.target.tagName !== 'INPUT') return; e.preventDefault();
    if (e.target.id === 'stkPhone') run(sendStk)(); else if (!$('#payOk').disabled) $('#payOk').click();
  });
  const guard = e => { if (stk?.pending && !confirm('The customer has been prompted on their phone. Stop waiting for this payment?')) e.preventDefault(); };
  $('#payDlg').addEventListener('cancel', guard); $('#payForm [value=cancel]').addEventListener('click', guard);
  $('#payDlg').addEventListener('close', stopStk);
}

/* ======================================================================== */
/* SHIFT */
/* ======================================================================== */
/* shift.js — cash drawer summary, cash in/out, ending the shift, signing out. */

function renderDrawer(c) {
  const d = c.drawer; if (!d) return; S.drawer = d;
  $('#cashLabel').textContent = isCashier() ? 'Drawer' : 'Collected';
  $('#cashChip').textContent = 'KSh ' + money(isCashier() ? d.expected_cash : d.cash_sales);
  const sh = S.me?.shift; $('#drawerSub').textContent = sh ? `${sh.name || 'Shift #' + sh.id}${sh.terminal ? ' · ' + sh.terminal : ''}` : '';
  const row = (k, v) => `<div class="drow"><span>${k}</span><b>${money(v)}</b></div>`, methods = Object.entries(d.by_method || {});
  $('#drawerBox').innerHTML = `<div class="stat"><small>Expected cash in drawer</small><b>KSh ${money(d.expected_cash)}</b></div>`
    + '<div class="drows">' + [['Opening float', d.opening_float], ['Cash sales', d.cash_sales], ['Refunds', -d.cash_refunds], ['Paid in', d.paid_in], ['Paid out', -d.payouts]].map(([k, v]) => row(k, v)).join('') + '</div>'
    + (methods.length ? '<h4 class="subh">Collected by method</h4><div class="drows">' + methods.map(([k, v]) => row(METHODS[k] || k, v)).join('') + '</div>' : '');
}

const cashMovement = kind => run(async () => {
  const v = await ask(kind === 'PAID_IN' ? 'Cash in' : 'Cash out', [{ name: 'amount', label: 'Amount', type: 'number', req: true }, { name: 'reason', label: 'Reason', req: true }]); if (!v) return;
  await api('shift/cash/', 'POST', { kind, ...v }); await refresh();
});

async function endShift() {
  const counts = isCashier() || Object.keys(S.drawer?.by_method || {}).length;   // waiters count only if they collected
  const body = counts ? await ask(isCashier() ? 'End shift: count your drawer' : 'End shift: cash you collected', [{ name: 'counted_cash', label: 'Total cash counted', type: 'number', req: true }], 'End shift') : { ok: 1 };
  if (!body) return;
  const d = await api('shift/end/', 'POST', body);
  showLogin(d.variance != null ? `Shift ended. Expected ${money(d.expected)} · counted ${money(d.counted)} · difference ${money(d.variance)}` : 'Shift ended.');
}

/* profile dropdown: shift, cash, end shift and sign out live behind the avatar */
const profileOpen = () => !$('#profileMenu').classList.contains('hidden');
function toggleProfile(open = !profileOpen()) { $('#profileMenu').classList.toggle('hidden', !open); $('#profileBtn').setAttribute('aria-expanded', open); }

function initShift() {
  $('#profileBtn').onclick = e => { e.stopPropagation(); toggleProfile(); };
  document.addEventListener('click', e => { if (profileOpen() && !e.target.closest('.profile')) toggleProfile(false); });
  document.addEventListener('keydown', e => { if (e.key === 'Escape' && profileOpen()) { toggleProfile(false); $('#profileBtn').focus(); } });
  $('#profileMenu').addEventListener('click', e => { if (e.target.closest('button')) toggleProfile(false); });
  $('#cashIn').onclick = cashMovement('PAID_IN'); $('#cashOut').onclick = cashMovement('PAYOUT');
  $('#endShift').onclick = run(endShift);
  $('#signOut').onclick = run(async () => { await api('auth/logout/', 'POST'); showLogin(); });
}

/* ======================================================================== */
/* MAIN */
/* ======================================================================== */
/* main.js — boot: wire every module's events, start the clock and auto-refresh, then sign in or show the login. */

initHome(); initOrders(); initTicket(); initOrderPage(); initPayment(); initShift(); initRooms();

const tick = () => { $('#clock').textContent = new Date().toLocaleTimeString('en-KE', { hour: '2-digit', minute: '2-digit' }); };
tick(); setInterval(tick, 1000);
const clockBk = () => { const el = $('#bkClock'); if (el) el.textContent = stamp(Date.now()); };
setInterval(clockBk, 1000); clockBk();
setInterval(() => { if (S.me && !$('dialog[open]')) refresh().catch(() => { }); }, 8000);   // paused while a dialog is open so nothing moves under the user

JSON.parse($('#pos-user').textContent) ? start() : showLogin();   // the server only serves this page to a signed-in user