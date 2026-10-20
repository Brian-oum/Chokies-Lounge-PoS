/* manager.js - back-office scripts. Loaded once, on every page, by base.html.
 *
 *   1. initSidebar()    hamburger collapse / expand, hover-open menu groups, remembered state
 *   2. initItemLines()  add / remove rows on item-line tables, label units, suggest cost, show stock available
 *   3. initDashboard()  Overview page: KPI cards, SVG charts, insights, rooms board, 30-second live refresh
 *
 * Load order matters:
 *   - base.html places this script straight after </nav>, so initSidebar() runs before first paint
 *     and the current menu group is already open when the page appears.
 *   - initItemLines() waits for DOMContentLoaded because the table and its JSON data live further down the page.
 *   - initDashboard() does the same: it needs #dx and the #dash-data JSON, and does nothing on other pages.
 *
 * Sidebar markup contract (see base.html):
 *   .side > .brand > .side-toggle
 *   .side > .side-body > .grp > h3 > .grp-h   (heading button)
 *                      > .grp-b > .grp-in > a (links; the current page's link has class "on")
 * Sidebar state classes:
 *   html.side-min     sidebar collapsed (icon strip on desktop, hidden menu on phones)
 *   .grp.open         group expanded
 *   .grp.is-current   group that contains the current page
 */
(function () {
    'use strict';

    // Safety net: if a page template still includes this file a second time, do nothing.
    if (window.managerJsLoaded) return;
    window.managerJsLoaded = true;

    /* ====================================================================== */
    /* 1. SIDEBAR                                                             */
    /* ====================================================================== */
    function initSidebar() {
        const root = document.documentElement;
        const side = document.querySelector('.side');
        if (!side) return;

        const toggle = side.querySelector('.side-toggle');
        const body = side.querySelector('.side-body');
        if (!toggle || !body) return;

        const groups = [...side.querySelectorAll('.grp')];
        const links = [...side.querySelectorAll('a')];

        const STORE_KEY = 'bo-side'; // localStorage: 'min' | 'open'
        const OPEN_DELAY = 120; // ms the pointer must rest on a group before it opens
        const REST_DELAY = 350; // ms after leaving the sidebar before groups return to rest
        const canHover = window.matchMedia('(hover: hover)').matches;

        let openTimer = null;
        let restTimer = null;

        const isMin = () => root.classList.contains('side-min');

        /* ---- groups ---------------------------------------------------- */
        const current = groups.find(g => g.querySelector('a.on')) || null;
        if (current) current.classList.add('is-current');

        function setOpen(group, open) {
            group.classList.toggle('open', open);
            group.querySelector('.grp-h').setAttribute('aria-expanded', String(open));
        }

        // Accordion: open one group, close every other.
        function openOnly(target) {
            groups.forEach(g => setOpen(g, g === target));
        }

        // Resting state: only the group holding the current page is open.
        function rest() {
            openOnly(current);
        }

        /* ---- collapse / expand ----------------------------------------- */
        function syncToggle() {
            const min = isMin();

            toggle.setAttribute('aria-expanded', String(!min));
            toggle.setAttribute('aria-label', min ? 'Expand sidebar' : 'Collapse sidebar');
            toggle.title = min ? 'Expand sidebar' : 'Collapse sidebar';

            // The icon strip needs tooltips; the full sidebar already shows the text.
            links.forEach(a => {
                const label = a.querySelector('.lbl');
                if (!label) return;

                if (min) a.title = label.textContent.trim();
                else a.removeAttribute('title');
            });
        }

        // In the icon strip the list can be taller than the screen: keep the current page's icon in view.
        function revealCurrent() {
            const on = side.querySelector('a.on');
            if (!on) return;

            const a = on.getBoundingClientRect();
            const b = body.getBoundingClientRect();

            if (a.bottom > b.bottom) body.scrollTop += a.bottom - b.bottom + 8;
            else if (a.top < b.top) body.scrollTop -= b.top - a.top + 8;
        }

        function setMin(min) {
            root.classList.toggle('side-min', min);
            syncToggle();

            // Collapsed: wait for the groups to finish expanding, then scroll the strip. Expanded: back to rest.
            if (min) setTimeout(revealCurrent, 250);
            else rest();

            try {
                localStorage.setItem(STORE_KEY, min ? 'min' : 'open');
            } catch (e) {
                /* storage blocked: the choice just won't be remembered */
            }
        }

        toggle.addEventListener('click', () => setMin(!isMin()));

        /* ---- hover to open (mouse only, expanded sidebar only) --------- */
        if (canHover) {
            groups.forEach(group => {
                group.addEventListener('pointerenter', e => {
                    if (e.pointerType !== 'mouse' || isMin()) return;

                    clearTimeout(restTimer);
                    clearTimeout(openTimer);
                    openTimer = setTimeout(() => openOnly(group), OPEN_DELAY);
                });
            });

            side.addEventListener('pointerleave', e => {
                if (e.pointerType !== 'mouse' || isMin()) return;

                clearTimeout(openTimer);
                restTimer = setTimeout(rest, REST_DELAY);
            });
        }

        /* ---- click / tap / keyboard on a heading ----------------------- */
        groups.forEach(group => {
            group.querySelector('.grp-h').addEventListener('click', e => {
                clearTimeout(openTimer);

                const isOpen = group.classList.contains('open');
                const fromMouse = canHover && e.detail > 0; // detail is 0 for keyboard activation

                // A mouse click on a group that hover already opened should not slam it shut.
                if (fromMouse && isOpen) return;

                if (isOpen) setOpen(group, false);
                else openOnly(group);
            });
        });

        /* ---- init ------------------------------------------------------ */
        rest();
        syncToggle();
        if (isMin()) revealCurrent();
    }

    /* ====================================================================== */
    /* 2. ITEM LINES (purchase / LPO / issue forms)                           */
    /* ====================================================================== */
    function initItemLines() {
        const table = document.querySelector('table.lines');
        if (!table) return;

        const items = JSON.parse(document.getElementById('item-data')?.textContent || '[]');
        const bal = JSON.parse(document.getElementById('bal-data')?.textContent || '{}');
        const byId = Object.fromEntries(items.map(i => [String(i.id), i]));

        const tpl = document.getElementById('line-tpl');
        const body = table.tBodies[0];
        const useStock = table.dataset.unit === 'stock'; // issue page counts stock units; purchases use purchase units
        const from = document.querySelector('[name=from_location]');

        // Re-draw one row: unit label, suggested cost, "In stock" hint.
        function refresh(tr, fromItem) {
            const i = byId[tr.querySelector('.pick').value];
            const unit = tr.querySelector('.unit');
            const cost = tr.querySelector('.cost');
            const av = tr.querySelector('.avail');

            if (unit) unit.textContent = i ? (useStock ? i.sunit : i.punit) : '';

            if (cost && i && fromItem && !cost.value) cost.value = i.cost;

            if (av) {
                const have = i && from ? (bal[i.id] || {})[from.value] || '0' : '';
                const q = parseFloat(tr.querySelector('.qty').value || 0);

                av.textContent = i ? 'In stock: ' + have + ' ' + i.sunit : '';
                av.classList.toggle('bad', !!i && q > parseFloat(have));
            }
        }

        function add() {
            body.appendChild(tpl.content.cloneNode(true));
        }

        table.addEventListener('change', e => {
            const tr = e.target.closest('tr');

            if (tr && e.target.matches('.pick')) refresh(tr, true);
            else if (tr) refresh(tr);
        });

        table.addEventListener('input', e => {
            if (e.target.matches('.qty')) refresh(e.target.closest('tr'));
        });

        // Remove button: drop the row, or just clear it when it is the last one.
        table.addEventListener('click', e => {
            if (!e.target.matches('.x')) return;

            const tr = e.target.closest('tr');

            if (body.rows.length > 1) tr.remove();
            else tr.querySelectorAll('input,select').forEach(f => (f.value = ''));
        });

        document.getElementById('add-line')?.addEventListener('click', add);

        from?.addEventListener('change', () => [...body.rows].forEach(tr => refresh(tr)));

        if (!body.rows.length) add();
        [...body.rows].forEach(tr => refresh(tr));
    }

    /* ====================================================================== */
    /* 3. DASHBOARD (Overview): KPIs, SVG charts, insights, rooms, live data   */
    /* ====================================================================== */
    // Data arrives as JSON (embedded in the page for the first paint, then fetched from data-feed every
    // data-every seconds). Everything is drawn with DOM calls and textContent, never innerHTML, so a guest
    // name or item name can't inject markup.
    function initDashboard() {
        const root = document.getElementById('dx');
        const seed = document.getElementById('dash-data');
        if (!root || !seed) return;

        const $ = id => document.getElementById(id);
        const NS = 'http://www.w3.org/2000/svg';
        const FEED = root.dataset.feed;
        const EVERY = (parseInt(root.dataset.every, 10) || 30) * 1000;

        const state = { data: JSON.parse(seed.textContent), range: 14, tab: 'items', paused: false, failed: 0, loading: false, width: 0 };

        /* ---- tiny DOM helpers --------------------------------------------- */
        function append(node, kids) {
            for (const c of kids.flat(Infinity)) {
                if (c == null || c === false) continue;
                node.append(c.nodeType ? c : document.createTextNode(String(c)));
            }
        }

        function make(ns, tag, props, kids) {
            const n = ns ? document.createElementNS(ns, tag) : document.createElement(tag);

            for (const [k, v] of Object.entries(props || {})) {
                if (v == null || v === false) continue;
                if (k.startsWith('on') && typeof v === 'function') n.addEventListener(k.slice(2), v);
                else n.setAttribute(k, v === true ? '' : v);
            }

            append(n, kids);
            return n;
        }

        const h = (tag, props, ...kids) => make(null, tag, props, kids);
        const sv = (tag, props, ...kids) => make(NS, tag, props, kids);

        /* ---- formatting ---------------------------------------------------- */
        const nf = new Intl.NumberFormat(undefined, { maximumFractionDigits: 0 });
        const cur = () => state.data.currency || '';
        const money = n => `${cur()} ${nf.format(Math.round(n || 0))}`;
        const plural = (n, one, many) => `${nf.format(n)} ${n === 1 ? one : many}`;

        function compact(n) {
            const a = Math.abs(n);
            if (a >= 1e6) return `${+(n / 1e6).toFixed(1)}M`;
            if (a >= 1e3) return `${+(n / 1e3).toFixed(a >= 1e4 ? 0 : 1)}k`;
            return String(Math.round(n));
        }

        const parse = iso => new Date(`${iso}T00:00:00`);
        const dayLabel = iso => parse(iso).toLocaleDateString('en-GB', { weekday: 'short', day: 'numeric' });
        const shortDate = iso => parse(iso).toLocaleDateString('en-GB', { day: 'numeric', month: 'short' });
        const hr12 = x => `${x % 12 || 12}${x < 12 ? 'a' : 'p'}`;
        const hrLong = x => `${x % 12 || 12} ${x < 12 ? 'am' : 'pm'}`;

        function niceMax(v) {
            if (v <= 0) return 1;
            const p = Math.pow(10, Math.floor(Math.log10(v)));
            const f = v / p;
            return (f <= 1 ? 1 : f <= 2 ? 2 : f <= 2.5 ? 2.5 : f <= 5 ? 5 : 10) * p;
        }

        function age(min) {
            if (min == null) return '';
            return min < 60 ? `${min} min` : `${Math.floor(min / 60)} h ${min % 60} min`;
        }

        const emptyState = (title, text) => h('div', { class: 'dx-empty' }, h('b', null, title), h('span', null, text));
        const swap = (node, ...kids) => node.replaceChildren(...kids.flat().filter(Boolean));

        /* ---- tooltip (one shared element) ---------------------------------- */
        const tip = h('div', { class: 'dx-tip hidden', role: 'presentation' });
        document.body.append(tip);

        function showTip(e, title, rows) {
            swap(tip, h('b', null, title), rows.map(([c, label, value]) =>
                h('div', null, h('span', null, c ? h('i', { style: `--c:${c}` }) : null, label), h('span', null, value))));
            tip.classList.remove('hidden');

            let x = e.clientX + 14;
            const y = Math.min(e.clientY + 14, window.innerHeight - tip.offsetHeight - 8);
            if (x + tip.offsetWidth > window.innerWidth - 8) x = e.clientX - tip.offsetWidth - 14;

            tip.style.left = `${Math.max(8, x)}px`;
            tip.style.top = `${Math.max(8, y)}px`;
        }

        const hideTip = () => tip.classList.add('hidden');
        document.addEventListener('pointerdown', e => { if (!e.target.closest('.dx-chart')) hideTip(); });

        /* ---- small charts ---------------------------------------------------- */
        function sparkline(values) {
            const max = Math.max(...values, 1);
            const step = 120 / Math.max(values.length - 1, 1);
            const pts = values.map((v, i) => [i * step, 36 - (v / max) * 32]);
            const line = pts.map((p, i) => `${i ? 'L' : 'M'}${p[0].toFixed(1)} ${p[1].toFixed(1)}`).join(' ');

            return sv('svg', { class: 'dx-spark', viewBox: '0 0 120 40', preserveAspectRatio: 'none', 'aria-hidden': 'true' },
                sv('path', { d: `${line} L120 40 L0 40 Z`, fill: 'currentColor', opacity: '.14' }),
                sv('path', { d: line, fill: 'none', stroke: 'currentColor', 'stroke-width': '2', 'stroke-linecap': 'round', 'stroke-linejoin': 'round', 'vector-effect': 'non-scaling-stroke' }));
        }

        function ring(pct) {
            const C = 2 * Math.PI * 24;
            return sv('svg', { class: 'dx-ring', viewBox: '0 0 58 58', 'aria-hidden': 'true' },
                sv('circle', { class: 't', cx: 29, cy: 29, r: 24 }),
                sv('circle', { class: 'v', cx: 29, cy: 29, r: 24, transform: 'rotate(-90 29 29)', 'stroke-dasharray': `${(Math.min(pct, 100) / 100) * C} ${C}` }));
        }

        function delta(v, none) {
            if (v == null) return h('span', { class: 'dx-delta' }, none);
            const up = v >= 0;
            return h('span', { class: `dx-delta ${v === 0 ? '' : up ? 'up' : 'down'}` }, `${up ? '▲' : '▼'} ${Math.abs(v).toFixed(1)}%`);
        }

        /* ---- KPI cards -------------------------------------------------------- */
        function renderKpis() {
            const d = state.data, s = d.sales, r = d.rooms, l = d.live;
            const spark = d.trend.map(x => x.total);

            const sales = h('div', { class: 'dx-kpi hero' },
                h('span', { class: 'dx-kl' }, 'Sales today'),
                h('span', { class: 'dx-kv' }, h('small', null, cur()), nf.format(Math.round(s.today))),
                delta(s.delta, 'Nothing to compare yet'),
                h('span', { class: 'dx-ks' }, s.delta == null ? plural(s.bills, 'bill', 'bills') : `vs this time yesterday · ${plural(s.bills, 'bill', 'bills')}`),
                sparkline(spark));

            const mtdPct = s.prev_mtd ? Math.min(100, (s.mtd / s.prev_mtd) * 100) : 0;
            const month = h('div', { class: 'dx-kpi' },
                h('span', { class: 'dx-kl' }, `Since ${shortDate(d.month_start)}`),
                h('span', { class: 'dx-kv' }, h('small', null, cur()), nf.format(Math.round(s.mtd))),
                delta(s.mtd_delta, 'No earlier month to compare'),
                h('span', { class: 'dx-ks' }, `Rooms ${money(s.mtd_rooms)} · Food and bar ${money(s.mtd_food)}`),
                s.prev_mtd ? h('div', { class: 'dx-meter', title: `Last month, same days: ${money(s.prev_mtd)}` }, h('i', { style: `width:${mtdPct}%` })) : null);

            const rooms = h('div', { class: 'dx-kpi' },
                h('span', { class: 'dx-kl' }, 'Rooms in house'),
                h('span', { class: 'dx-kv' }, r.total ? `${r.occupied} of ${r.total}` : '0'),
                h('span', { class: 'dx-ks' }, r.total ? `${r.pct}% occupied · ${r.arrivals.length} arriving · ${r.departures.length} departing` : 'No rooms set up yet'),
                r.total ? ring(r.pct) : null);

            const owed = h('a', { class: 'dx-kpi', href: d.urls.bills },
                h('span', { class: 'dx-kl' }, 'Waiting to be paid'),
                h('span', { class: 'dx-kv' }, h('small', null, cur()), nf.format(Math.round(l.bills_owed))),
                h('span', { class: `dx-delta ${l.bills_stale ? 'down' : ''}` }, l.bills_stale ? `${l.bills_stale} over ${l.stale_hours} h` : 'None overdue'),
                h('span', { class: 'dx-ks' }, `${plural(l.bills_open, 'open bill', 'open bills')}`));

            swap($('dx-kpis'), sales, month, rooms, owed);
        }

        /* ---- sales trend (stacked bars) ---------------------------------------- */
        function renderTrend() {
            const box = $('dx-trend');
            const days = state.data.trend.slice(-state.range);
            const total = days.reduce((a, x) => a + x.total, 0);
            const rooms = days.reduce((a, x) => a + x.rooms, 0);
            const avg = total / days.length;

            swap($('dx-trend-legend'),
                h('span', { style: '--c:var(--c0)' }, h('i'), 'Restaurant and bar ', h('b', null, money(total - rooms))),
                h('span', { style: '--c:var(--c1)' }, h('i'), 'Rooms ', h('b', null, money(rooms))),
                h('span', null, 'Average a day ', h('b', null, money(avg))));

            if (!total) return swap(box, emptyState('No sales in this period', 'Paid restaurant, bar and room bills are charted here once they are settled.'));

            const W = Math.max(box.clientWidth || 640, 260), H = 250;
            const m = { l: 46, r: 8, t: 12, b: 26 };
            const iw = W - m.l - m.r, ih = H - m.t - m.b;
            const max = niceMax(Math.max(...days.map(x => x.total)));
            const n = days.length, band = iw / n, bw = Math.min(band * 0.64, 36);
            const y = v => m.t + ih - (v / max) * ih;
            const step = band < 46 ? 2 : 1;

            const svg = sv('svg', { viewBox: `0 0 ${W} ${H}`, width: W, height: H, role: 'img', 'aria-label': `Sales for the last ${n} days, ${money(total)} in total` });

            for (let i = 0; i <= 4; i++) {
                const gy = y((max * i) / 4);
                svg.append(sv('line', { class: 'dx-gl', x1: m.l, x2: W - m.r, y1: gy, y2: gy }),
                    sv('text', { class: 'dx-ax', x: m.l - 8, y: gy + 4, 'text-anchor': 'end' }, compact((max * i) / 4)));
            }

            days.forEach((d, i) => {
                const x = m.l + band * i + (band - bw) / 2;
                const hf = (d.food / max) * ih, hr = (d.rooms / max) * ih;
                const last = i === n - 1;

                svg.append(sv('rect', { class: 'dx-b', x, y: y(d.food), width: bw, height: Math.max(hf, 0), rx: 3, style: 'fill:var(--c0)', opacity: last ? 1 : 0.8 }));
                if (d.rooms > 0) svg.append(sv('rect', { class: 'dx-b', x, y: y(d.food) - hr - 1, width: bw, height: Math.max(hr, 0), rx: 3, style: 'fill:var(--c1)', opacity: last ? 1 : 0.8 }));
                if ((n - 1 - i) % step === 0) svg.append(sv('text', { class: 'dx-ax', x: x + bw / 2, y: H - 8, 'text-anchor': 'middle', 'font-weight': last ? 700 : 400 }, last ? 'Today' : dayLabel(d.date)));

                const bx = m.l + band * i;
                const hit = sv('rect', { class: 'dx-hit', x: bx, y: m.t, width: band, height: ih });
                const tipRows = () => [['var(--c0)', 'Restaurant and bar', money(d.food)], ['var(--c1)', 'Rooms', money(d.rooms)], [null, 'Total', money(d.total)], [null, 'Bills', nf.format(d.bills)]];
                hit.addEventListener('pointermove', e => showTip(e, dayLabel(d.date), tipRows()));
                hit.addEventListener('pointerdown', e => showTip(e, dayLabel(d.date), tipRows()));
                hit.addEventListener('pointerleave', hideTip);
                svg.append(hit, sv('rect', { class: 'dx-hover', x: bx, y: m.t, width: band, height: ih, rx: 6 }));
            });

            const ay = y(avg);
            svg.append(sv('line', { class: 'dx-avg', x1: m.l, x2: W - m.r, y1: ay, y2: ay }),
                sv('text', { class: 'dx-ax', x: W - m.r - 2, y: ay - 5, 'text-anchor': 'end', style: 'paint-order:stroke;stroke:var(--card);stroke-width:3px' }, `Average ${compact(avg)}`));

            swap(box, svg);
        }

        /* ---- today by hour ------------------------------------------------------ */
        function renderHours() {
            const box = $('dx-hours');
            const { today, typical } = state.data.hourly;
            const nowH = state.data.hour;

            swap($('dx-hours-legend'),
                h('span', { style: '--c:var(--c0)' }, h('i'), 'Today'),
                h('span', { style: '--c:var(--c2)' }, h('i', { class: 'line' }), 'Typical day'));

            if (!today.some(Boolean) && !typical.some(Boolean)) return swap(box, emptyState('No sales yet today', 'Sales by hour appear here as bills are paid.'));

            let first = 8, last = 22;
            for (let i = 0; i < 24; i++) if (today[i] > 0 || typical[i] > 0) { first = Math.min(first, i); last = Math.max(last, i); }

            const hrs = [];
            for (let i = first; i <= last; i++) hrs.push(i);

            const W = Math.max(box.clientWidth || 360, 240), H = 210;
            const m = { l: 40, r: 6, t: 10, b: 24 };
            const iw = W - m.l - m.r, ih = H - m.t - m.b;
            const max = niceMax(Math.max(...hrs.map(i => Math.max(today[i], typical[i]))));
            const band = iw / hrs.length, bw = Math.min(band * 0.7, 22);
            const y = v => m.t + ih - (v / max) * ih;
            const cx = i => m.l + band * (i - first) + band / 2;

            const svg = sv('svg', { viewBox: `0 0 ${W} ${H}`, width: W, height: H, role: 'img', 'aria-label': 'Sales by hour today against a typical day' });

            for (let i = 0; i <= 3; i++) {
                const gy = y((max * i) / 3);
                svg.append(sv('line', { class: 'dx-gl', x1: m.l, x2: W - m.r, y1: gy, y2: gy }),
                    sv('text', { class: 'dx-ax', x: m.l - 6, y: gy + 4, 'text-anchor': 'end' }, compact((max * i) / 3)));
            }

            hrs.forEach(i => {
                const bh = (today[i] / max) * ih;
                svg.append(sv('rect', { class: 'dx-b', x: cx(i) - bw / 2, y: y(today[i]), width: bw, height: bh, rx: 3, style: 'fill:var(--c0)', opacity: i === nowH ? 1 : 0.8 }));
                if (hrs.length <= 12 || i % 2 === 0) svg.append(sv('text', { class: 'dx-ax', x: cx(i), y: H - 6, 'text-anchor': 'middle' }, hr12(i)));
            });

            const pts = hrs.map(i => `${cx(i).toFixed(1)},${y(typical[i]).toFixed(1)}`);
            svg.append(sv('polyline', { points: pts.join(' '), fill: 'none', style: 'stroke:var(--c2)', 'stroke-width': 2, 'stroke-linejoin': 'round', 'stroke-linecap': 'round' }));

            if (nowH >= first && nowH <= last) {
                svg.append(sv('line', { class: 'dx-now', x1: cx(nowH), x2: cx(nowH), y1: m.t, y2: m.t + ih }),
                    sv('text', { class: 'dx-nowl', x: cx(nowH), y: m.t - 1, 'text-anchor': 'middle' }, 'Now'));
            }

            hrs.forEach(i => {
                const rows = () => [['var(--c0)', 'Today', money(today[i])], ['var(--c2)', 'Typical', money(typical[i])]];
                const bx = m.l + band * (i - first);
                const hit = sv('rect', { class: 'dx-hit', x: bx, y: m.t, width: band, height: ih });
                hit.addEventListener('pointermove', e => showTip(e, hrLong(i), rows()));
                hit.addEventListener('pointerdown', e => showTip(e, hrLong(i), rows()));
                hit.addEventListener('pointerleave', hideTip);
                svg.append(hit, sv('rect', { class: 'dx-hover', x: bx, y: m.t, width: band, height: ih, rx: 6 }));
            });

            swap(box, svg);
        }

        /* ---- payment methods (donut) ----------------------------------------------- */
        function renderMethods() {
            const list = state.data.methods;
            const sum = list.reduce((a, x) => a + x.amount, 0);

            if (!sum) return swap($('dx-methods'), emptyState('No payments yet today', 'Cash, card and mobile payments appear here as they are taken.'));

            const R = 60, C = 2 * Math.PI * R;
            const gap = list.length > 1 ? 2 : 0;
            let used = 0;

            const svg = sv('svg', { viewBox: '0 0 168 168', role: 'img', 'aria-label': `${money(sum)} collected today` },
                sv('circle', { class: 'bg', cx: 84, cy: 84, r: R }));

            list.forEach((x, i) => {
                const len = (x.amount / sum) * C;
                svg.append(sv('circle', { cx: 84, cy: 84, r: R, transform: 'rotate(-90 84 84)', style: `stroke:var(--c${i % 6})`,
                    'stroke-dasharray': `${Math.max(len - gap, 0.5)} ${C - Math.max(len - gap, 0.5)}`, 'stroke-dashoffset': -used }));
                used += len;
            });

            svg.append(sv('text', { class: 'big', x: 84, y: 86 }, compact(sum)), sv('text', { class: 'sm', x: 84, y: 103 }, 'Collected'));

            swap($('dx-methods'), h('div', { class: 'dx-donut' }, svg,
                h('ul', { class: 'dx-pays' }, list.map((x, i) =>
                    h('li', { style: `--c:var(--c${i % 6})` }, h('i'), h('span', null, x.label), h('b', null, money(x.amount)), h('em', null, `${Math.round((x.amount / sum) * 100)}%`))))));
        }

        /* ---- best sellers ------------------------------------------------------------ */
        function renderTop() {
            const s = state.data.sellers;
            const rows = state.tab === 'items' ? s.items : s.cats;

            if (!rows.length) return swap($('dx-top'), emptyState('No sales to rank yet', 'Items and categories are ranked here once bills are paid.'));

            const max = Math.max(...rows.map(x => x.amount), 1);

            swap($('dx-top'), h('ul', { class: 'dx-hbars' }, rows.map((x, i) =>
                h('li', { style: `--c:var(--c${state.tab === 'items' ? 0 : i % 6})` },
                    h('span', { title: x.name }, x.name), h('b', null, money(x.amount)),
                    h('div', { class: 'track' }, h('i', { style: `width:${state.tab === 'items' ? (x.amount / max) * 100 : x.share}%` })),
                    h('small', { class: 'muted' }, state.tab === 'items' ? `${nf.format(x.qty)} sold` : `${x.share}% of sales`)))));
        }

        /* ---- insights -------------------------------------------------------------------- */
        function renderInsights() {
            const list = state.data.insights;
            const n = list.filter(x => x.tone === 'bad' || x.tone === 'warn').length;

            const count = $('dx-insight-count');
            count.textContent = n ? `${n} to review` : 'All clear';
            count.className = `pill ${n ? 'warn' : 'ok'}`;

            if (!list.length) return swap($('dx-insights'), h('li', { class: 'none', style: 'display:block' }, emptyState('Nothing needs attention', 'Insights appear here when sales, stock or rooms change.')));

            swap($('dx-insights'), list.map(x =>
                h('li', { class: x.tone }, h('b', null, x.title), h('p', null, x.text), x.href ? h('a', { href: x.href }, `${x.cta || 'Open'} →`) : null)));
        }

        /* ---- right now ---------------------------------------------------------------------- */
        function renderLive() {
            const l = state.data.live, u = state.data.urls;
            const tp = l.tables_total ? Math.round((l.tables_busy / l.tables_total) * 100) : 0;

            swap($('dx-live-ops'), h('div', { class: 'dx-facts' },
                h('div', { class: 'dx-fact' }, h('span', null, 'Open orders'), h('b', null, nf.format(l.open_orders)),
                    h('small', { class: 'muted' }, l.oldest_minutes != null ? `Oldest has been open ${age(l.oldest_minutes)}` : 'No orders are open. New orders appear here.')),
                h('div', { class: 'dx-fact' }, h('span', null, 'Tables in use'), h('b', null, `${l.tables_busy} of ${l.tables_total}`),
                    l.tables_total ? h('div', { class: 'dx-meter' }, h('i', { style: `width:${tp}%` })) : null),
                h('a', { class: `dx-fact ${l.bills_stale ? 'warn' : ''}`, href: u.bills }, h('span', null, 'Bills waiting to be paid'), h('b', null, nf.format(l.bills_open)),
                    h('small', { class: 'muted' }, l.bills_open ? `${money(l.bills_owed)} to collect${l.bills_stale ? ` · ${l.bills_stale} older than ${l.stale_hours} h` : ''}` : 'Every bill is settled.')),
                h('div', { class: 'dx-fact' }, h('span', null, 'Staff on shift'), h('b', null, nf.format(l.crew_total)),
                    l.crew.length ? h('div', { class: 'dx-crew' }, l.crew.map(c => h('span', null, c.name, ' ', h('em', null, `· ${c.role}`)))) : h('small', { class: 'muted' }, 'Nobody is signed in to a shift.'))));
        }

        /* ---- stock and buying ------------------------------------------------------------------ */
        function renderStock() {
            const s = state.data.stock, u = state.data.urls;

            const stage = (n, href, label, go, goZero, alert) =>
                h('a', { class: `stage ${alert && n ? 'alert' : ''} ${n ? '' : 'zero'}`, href },
                    h('b', null, nf.format(n)), h('span', { class: 'lbl2' }, label), h('span', { class: 'go' }, n ? `${go} →` : goZero));

            const flow = h('div', { class: 'stages' },
                stage(s.low_count, u.low, s.out_count ? `items low · ${s.out_count} out of stock` : 'items at or below reorder level', 'Review low stock', 'All stocked', true),
                stage(s.draft_lpos, u.drafts, 'draft LPOs to send', 'Send LPOs', 'Nothing to send', false),
                stage(s.awaiting, u.receive, 'LPOs awaiting delivery', 'Receive stock', 'Nothing due', false));

            const table = s.low_items.length
                ? h('div', { class: 'tw' }, h('table', null,
                    h('thead', null, h('tr', null, h('th', null, 'Running low'), h('th', { class: 'n' }, 'On hand'), h('th', { class: 'n' }, 'Reorder at'), h('th', null, ''), h('th', null, ''))),
                    h('tbody', null, s.low_items.map(i => h('tr', null,
                        h('td', null, h('a', { href: i.url }, i.name), i.out ? h('span', { class: 'pill bad' }, 'Out') : null),
                        h('td', { class: 'n' }, `${nf.format(i.have)} ${i.unit}`),
                        h('td', { class: 'n' }, nf.format(i.level)),
                        h('td', null, h('span', { class: `gauge ${i.out ? 'out' : ''}`, style: `--p:${i.pct}` })),
                        h('td', { class: 'n' }, h('a', { href: u.lpo_new }, 'Order')))))))
                : h('div', { class: 'dx-empty' }, h('b', null, 'Everything is above its reorder level'), h('span', null, 'Items appear here when stock drops to their reorder level.'));

            const mini = h('div', { class: 'dx-mini' },
                h('span', null, 'Stock value ', h('b', null, money(s.value))),
                h('span', null, `Bought since ${shortDate(state.data.month_start)} `, h('b', null, money(s.spent_month))),
                h('a', { href: u.takes }, h('b', null, s.takes_open), ' stock takes in progress'),
                h('a', { href: u.voids }, h('b', null, s.voids_today), ' voids today'),
                h('a', { href: u.bills }, h('b', null, s.discounts_today), ' discounts today'));

            swap($('dx-stock'), h('div', { class: 'dx-stock-body' }, flow, table, mini));
        }

        /* ---- rooms ----------------------------------------------------------------------------------- */
        const STATES = { occupied: ['Occupied', 'var(--blue)'], arriving: ['Arriving', 'var(--vio)'], vacant: ['Ready', 'var(--ok)'], cleaning: ['Being cleaned', 'var(--warn)'], maintenance: ['Out of service', '#8a988f'] };
        const PAY = { PAID: ['ok', 'Paid'], PART: ['warn', 'Part-paid'], UNPAID: ['bad', 'Unpaid'], UNBILLED: ['off', 'Not billed'], REFUNDED: ['off', 'Refunded'] };

        const csrf = () => root.querySelector('#dx-csrf input')?.value || '';

        function postForm(url, fields, button, confirmText) {
            const f = h('form', { method: 'post', action: url },
                h('input', { type: 'hidden', name: 'csrfmiddlewaretoken', value: csrf() }),
                h('input', { type: 'hidden', name: 'next', value: location.pathname + location.search }),
                Object.entries(fields).map(([name, value]) => h('input', { type: 'hidden', name, value })),
                button);

            if (confirmText) f.addEventListener('submit', e => { if (!confirm(confirmText)) e.preventDefault(); });
            return f;
        }

        const dots = () => sv('svg', { viewBox: '0 0 24 24', width: 16, height: 16, fill: 'currentColor', 'aria-hidden': 'true' },
            sv('circle', { cx: 5, cy: 12, r: 2 }), sv('circle', { cx: 12, cy: 12, r: 2 }), sv('circle', { cx: 19, cy: 12, r: 2 }));

        function roomTile(r, statuses) {
            const today = state.data.today;
            const [label, color] = STATES[r.state] || STATES.vacant;
            let who = '', sub = '';

            if (r.state === 'occupied') { who = r.guest; sub = `Until ${shortDate(r.until)}`; }
            else if (r.state === 'arriving') { who = r.guest; sub = r.from < today ? `Was due ${shortDate(r.from)}` : 'Due today'; }
            else if (r.state === 'vacant') sub = r.next ? `Next booking ${shortDate(r.next)}` : 'No bookings this week';
            else if (r.state === 'cleaning') sub = 'Not ready for guests yet';
            else sub = "Can't be booked";

            const menu = r.state === 'occupied' ? null : h('details', { class: 'dx-menu' },
                h('summary', { 'aria-label': `Change status of ${r.name}`, title: 'Change status' }, dots()),
                h('div', { class: 'pop' }, h('small', { class: 'muted' }, 'Set room as'),
                    statuses.map(s => postForm(r.url, { status: s.value },
                        h('button', { type: 'submit', class: s.value === r.status ? 'on' : '', disabled: s.value === r.status }, s.label),
                        s.out ? `Take ${r.name} out of service? It can't be booked until you mark it ready again.` : ''))));

            return h('div', { class: `dx-room ${r.state}`, style: `--c:${color}` }, h('b', null, r.name), menu,
                h('small', { class: 'muted' }, [r.type, r.capacity ? `sleeps ${r.capacity}` : ''].filter(Boolean).join(' · ')),
                h('span', { class: 'what' }, label),
                who ? h('span', { class: 'who', title: who }, who) : null,
                h('small', { class: 'muted' }, sub),
                r.due_out ? h('span', { class: 'due' }, r.until < today ? 'Past check-out' : 'Due out today') : null);
        }

        function moveRow(b, kind) {
            const today = state.data.today;
            const pay = PAY[b.pay] || PAY.UNBILLED;
            const blocked = kind === 'in' ? b.not_ready.length > 0 : b.pay !== 'PAID';
            const reason = kind === 'in' ? `${b.not_ready.join(', ')} isn't ready yet` : `Bill must be paid in full first (balance ${money(b.balance)})`;
            const label = kind === 'in' ? 'Check in' : 'Check out';
            const when = kind === 'in'
                ? (b.check_in < today ? `Was due ${shortDate(b.check_in)}` : 'Due today')
                : (b.check_out < today ? `Past check-out since ${shortDate(b.check_out)}` : 'Leaving today');

            const confirmText = kind === 'out' ? `Check out ${b.guest}? ${b.rooms.join(', ')} will move to cleaning.` : '';
            const button = h('button', { type: 'submit', class: `btn sm ${blocked ? 'outline' : 'primary'}`, disabled: blocked, title: blocked ? reason : '' }, label);

            return h('li', null,
                h('div', null, h('b', null, b.guest), h('br'), h('small', { class: 'muted' }, `${b.rooms.join(', ')} · ${plural(b.nights, 'night', 'nights')} · ${plural(b.guests, 'guest', 'guests')}`)),
                h('div', { class: 'side' }, h('span', { class: `pill ${pay[0]}` }, pay[1]), b.url ? postForm(b.url, {}, button, confirmText) : null),
                h('small', { class: 'muted' }, when, b.balance > 0 ? [' · ', h('span', { class: kind === 'out' ? 'bad-t' : 'warn-t' }, `Balance ${money(b.balance)}`)] : null,
                    blocked ? [' · ', h('span', { class: 'warn-t' }, kind === 'in' ? `${b.not_ready.join(', ')} not ready` : 'Settle the bill first')] : null));
        }

        function renderRooms() {
            const r = state.data.rooms;
            const chips = $('dx-room-chips');

            if (!r.total) {
                swap(chips);
                swap($('dx-room-key'));
                swap($('dx-board'), emptyState('No rooms yet', 'Once rooms are added, occupancy, arrivals and departures show up here.'));
                swap($('dx-forecast'), emptyState('No forecast yet', 'Add rooms to see how booked the next 7 nights are.'));
                swap($('dx-arrivals'), emptyState('No arrivals today', 'Reservations due today appear here.'));
                swap($('dx-departures'), emptyState('No departures today', 'Guests leaving today appear here.'));
                $('dx-arr-count').textContent = '';
                $('dx-dep-count').textContent = '';
                return;
            }

            const chip = (label, n, tone) => h('span', { class: `dx-chip ${n && tone ? tone : ''}` }, h('b', null, n), label);
            swap(chips, chip('in house', `${r.occupied}/${r.total}`), chip(`% occupied`, r.pct),
                chip('arriving', r.arrivals.length), chip('departing', r.departures.length),
                chip('ready', r.vacant), chip('being cleaned', r.cleaning, 'warn'), chip('out of service', r.maintenance, 'bad'));

            swap($('dx-room-key'), Object.values(STATES).map(([l, c]) => h('span', { style: `--c:${c}` }, h('i'), l)));

            if (!busy($('dx-board'))) swap($('dx-board'), h('div', { class: 'dx-board' }, r.tiles.map(t => roomTile(t, r.statuses))));

            const max = r.forecast.length;
            const avg = Math.round(r.forecast.reduce((a, f) => a + f.pct, 0) / max);
            swap($('dx-forecast'), h('div', { class: 'dx-fc' }, r.forecast.map((f, i) =>
                h('div', { title: `${f.rooms} of ${r.total} rooms booked` },
                    h('span', { class: 'col' }, h('i', { class: f.pct >= 85 ? 'hot' : '', style: `height:${f.pct}%` })),
                    h('b', null, `${f.pct}%`), h('span', null, i === 0 ? 'Tonight' : i === 1 ? 'Tmrw' : dayLabel(f.date).split(' ')[0])))),
                h('p', { class: 'dx-note', style: 'margin:.8rem 0 0' }, `Average ${avg}% of ${plural(r.total, 'room', 'rooms')} booked. Counts reservations and guests staying.`));

            $('dx-arr-count').textContent = r.arrivals.length ? `${r.arrivals.length} today` : '';
            $('dx-dep-count').textContent = r.departures.length ? `${r.departures.length} today` : '';

            if (!busy($('dx-arrivals'))) {
                const next = r.upcoming[0];
                swap($('dx-arrivals'), r.arrivals.length
                    ? h('ul', { class: 'dx-moves' }, r.arrivals.map(b => moveRow(b, 'in')))
                    : emptyState('No arrivals today', next ? `Next: ${next.guest} on ${shortDate(next.check_in)}.` : 'Reservations due today appear here.'));
            }

            if (!busy($('dx-departures'))) {
                swap($('dx-departures'), r.departures.length
                    ? h('ul', { class: 'dx-moves' }, r.departures.map(b => moveRow(b, 'out')))
                    : emptyState('No departures today', r.in_house ? `${plural(r.in_house, 'booking is', 'bookings are')} in house.` : 'Guests leaving today appear here.'));
            }
        }

        // Don't redraw a panel while someone is using it (an open menu, or a focused control).
        function busy(node) {
            return !!node.querySelector('details[open]') || (node.contains(document.activeElement) && document.activeElement !== document.body);
        }

        /* ---- draw everything ------------------------------------------------------------------------------- */
        function stamp() {
            const t = new Date(state.data.generated);
            const day = parse(state.data.today).toLocaleDateString('en-GB', { weekday: 'long', day: 'numeric', month: 'long' });
            $('dx-sub').textContent = `${day} · Updated ${t.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })}`;
        }

        function renderCharts() {
            state.width = root.clientWidth;
            renderTrend();
            renderHours();
        }

        function renderAll() {
            renderKpis();
            renderInsights();
            renderMethods();
            renderTop();
            renderLive();
            renderStock();
            renderRooms();
            renderCharts();
            stamp();
        }

        /* ---- controls ----------------------------------------------------------------------------------------- */
        root.querySelectorAll('[data-range]').forEach(b => b.addEventListener('click', () => {
            state.range = parseInt(b.dataset.range, 10);
            root.querySelectorAll('[data-range]').forEach(x => x.classList.toggle('on', x === b));
            renderTrend();
        }));

        root.querySelectorAll('[data-tab]').forEach(b => b.addEventListener('click', () => {
            state.tab = b.dataset.tab;
            root.querySelectorAll('[data-tab]').forEach(x => x.classList.toggle('on', x === b));
            renderTop();
        }));

        let resizeTimer = null;
        if (window.ResizeObserver) {
            new ResizeObserver(() => {
                if (Math.abs(root.clientWidth - state.width) < 2) return; // height-only changes don't need a redraw
                clearTimeout(resizeTimer);
                resizeTimer = setTimeout(renderCharts, 150);
            }).observe(root);
        }

        /* ---- live updates --------------------------------------------------------------------------------------------- */
        const liveBtn = $('dx-live');
        const banner = $('dx-status');
        let timer = null;
        let lastOk = Date.now();

        function paintLive() {
            const off = state.paused, err = !off && state.failed > 0;
            liveBtn.classList.toggle('off', off);
            liveBtn.classList.toggle('err', err);
            liveBtn.querySelector('span').textContent = off ? 'Paused' : err ? 'Offline' : 'Live';
            liveBtn.setAttribute('aria-pressed', String(!off));
            liveBtn.title = off ? 'Resume live updates' : 'Pause live updates';
        }

        function say(text) {
            banner.textContent = text || '';
            banner.classList.toggle('hidden', !text);
        }

        async function refresh() {
            if (state.loading) return;
            state.loading = true;

            try {
                const r = await fetch(FEED, { credentials: 'same-origin', cache: 'no-store', headers: { Accept: 'application/json', 'X-Requested-With': 'XMLHttpRequest' } });
                const type = r.headers.get('content-type') || '';

                if (!r.ok || !type.includes('json')) throw new Error(r.redirected || r.status === 401 || r.status === 403 ? 'auth' : 'http');

                state.data = await r.json();
                state.failed = 0;
                lastOk = Date.now();
                say('');
                renderAll();
            } catch (e) {
                state.failed++;
                const at = new Date(lastOk).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
                say(e.message === 'auth'
                    ? 'You have been signed out. Reload the page to sign in again.'
                    : `Couldn't refresh. Showing data from ${at}. Trying again shortly.`);
            } finally {
                state.loading = false;
                paintLive();
            }
        }

        function schedule() {
            clearTimeout(timer);
            if (state.paused) return;

            timer = setTimeout(async () => {
                if (!document.hidden) await refresh();
                schedule();
            }, EVERY * Math.min(1 + state.failed, 4)); // back off while the server is unreachable
        }

        liveBtn.addEventListener('click', () => {
            state.paused = !state.paused;
            paintLive();

            if (state.paused) clearTimeout(timer);
            else refresh().then(schedule);
        });

        document.addEventListener('visibilitychange', () => {
            if (!document.hidden && !state.paused && Date.now() - lastOk > EVERY) refresh();
        });

        renderAll();
        paintLive();
        schedule();
    }

    /* ====================================================================== */
    /* START                                                                  */
    /* ====================================================================== */
    initSidebar();

    function onReady(fn) {
        if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', fn);
        else fn();
    }

    onReady(initItemLines);
    onReady(initDashboard);
})();