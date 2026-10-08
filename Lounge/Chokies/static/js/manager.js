/* manager.js - back-office scripts. Loaded once, on every page, by base.html.
 *
 *   1. initSidebar()    hamburger collapse / expand, hover-open menu groups, remembered state
 *   2. initItemLines()  add / remove rows on item-line tables, label units, suggest cost, show stock available
 *
 * Load order matters:
 *   - base.html places this script straight after </nav>, so initSidebar() runs before first paint
 *     and the current menu group is already open when the page appears.
 *   - initItemLines() waits for DOMContentLoaded because the table and its JSON data live further down the page.
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
    /* START                                                                  */
    /* ====================================================================== */
    initSidebar();

    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', initItemLines);
    else initItemLines();
})();