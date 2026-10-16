/*
 * nmc.js - shared behaviour inlined at the end of <body> on every
 * authenticated page by page() in apc-nmc-web.py.
 *
 *   1. nmcTheme()    dark/light toggle, wired to the top-bar button
 *                    (onclick="nmcTheme()" in the page template)
 *   2. nmcTable()    enhances table.ev / table.kv / table.pgtbl with
 *                    per-column filter inputs and client-side pagination
 *   3. status poll   every 5 s refreshes the top-bar alarm pill and the
 *                    "live data" dot from /status.json
 */


/* ------------------------------------------------------------------ *
 * 1. Theme toggle.  Flips the data-theme attribute that nmc.css keys   *
 *    off and remembers the choice in localStorage (read by theme.js    *
 *    on the next page load).                                           *
 * ------------------------------------------------------------------ */
function nmcTheme() {
    var h = document.documentElement,
        t = h.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
    h.setAttribute('data-theme', t);
    localStorage.nmcTheme = t;
}


/* ------------------------------------------------------------------ *
 * 2. Generic table enhancer: per-column filter inputs + client-side    *
 *    pagination.                                                       *
 *                                                                    *
 *    Auto-applies to table.ev / table.kv; tables with fewer than 5     *
 *    data rows or containing form controls are left alone unless       *
 *    class="pgtbl" forces it.  Optional per-table hooks:               *
 *      data-pg-global="#id" - external "match any cell" filter input   *
 *      data-pg-sev          - AND-filter rows against the checked      *
 *                             .evf severity checkboxes (event log)     *
 * ------------------------------------------------------------------ */
function nmcTable(tbl) {
    if (tbl._pgt) return;                 // already enhanced
    tbl._pgt = 1;

    // Snapshot rows; a leading row containing a <th> counts as the header.
    var rows0 = [].slice.call(tbl.rows || []);
    if (!rows0.length) return;
    var head = null;
    if (rows0[0].querySelector('th')) head = rows0[0];
    var rows = rows0.slice(head ? 1 : 0);

    // Bail out on small/interactive tables unless explicitly forced on.
    var forced = tbl.classList.contains('pgtbl');
    if (!forced &&
        (rows.length < 5 || tbl.querySelector('input,select,textarea,form')))
        return;

    var ncols = head ? head.children.length : rows[0].children.length;
    if (!ncols) return;

    // --- filter row: one .cf text input under each non-empty header cell ---
    var pg = 0, i, fr = document.createElement('tr');
    fr.className = 'flt';
    for (i = 0; i < ncols; i++) {
        var th = document.createElement('th');
        if (!head || head.children[i].textContent.trim() !== '') {
            var inp = document.createElement('input');
            inp.type = 'text';
            inp.className = 'cf';
            inp.setAttribute('data-c', i);   // column index this box filters
            inp.placeholder = 'Filter\u2026';
            inp.addEventListener('input', function () { pg = 0; apply(); });
            th.appendChild(inp);
        }
        fr.appendChild(th);
    }
    if (head) head.parentNode.insertBefore(fr, head.nextSibling);
    else rows0[0].parentNode.insertBefore(fr, rows0[0]);

    // --- pagination bar: info text, prev/next buttons, page-size select ---
    var bar = document.createElement('div');
    bar.className = 'pgbar';
    var info = document.createElement('span');
    info.className = 'pginfo';
    var prev = document.createElement('button');
    prev.type = 'button';
    prev.className = 'pgbtn';
    prev.innerHTML = '&lsaquo; Prev';
    var next = document.createElement('button');
    next.type = 'button';
    next.className = 'pgbtn';
    next.innerHTML = 'Next &rsaquo;';
    var sel = document.createElement('select');
    [10, 25, 50, 100].forEach(function (n) {
        var o = document.createElement('option');
        o.value = n;
        o.textContent = n + ' / page';
        sel.appendChild(o);
    });
    var oa = document.createElement('option');
    oa.value = '0';                          // '0' means "no pagination"
    oa.textContent = 'All';
    sel.appendChild(oa);
    sel.value = '25';                        // default page size
    bar.appendChild(info);
    bar.appendChild(prev);
    bar.appendChild(next);
    bar.appendChild(sel);
    tbl.parentNode.insertBefore(bar, tbl.nextSibling);

    // Optional external "match any cell" box (data-pg-global).
    var gi = null, gls = tbl.getAttribute('data-pg-global');
    if (gls) gi = document.querySelector(gls);
    if (gi) gi.addEventListener('input', function () { pg = 0; apply(); });

    // Optional severity checkboxes (data-pg-sev): the event log's
    // crit/warn/info filters; a row is hidden when its data-sev box is
    // unchecked.
    var sevboxes = tbl.hasAttribute('data-pg-sev')
        ? [].slice.call(document.querySelectorAll('.evf')) : [];
    sevboxes.forEach(function (c) {
        c.addEventListener('change', function () { pg = 0; apply(); });
    });

    // Recompute visibility + current page, then show/hide rows.
    function apply() {
        var rows = [].slice.call(tbl.rows || [])
            .filter(function (tr) { return tr !== head && tr !== fr; });

        // Per-column queries keyed by column index.
        var qs = [];
        [].forEach.call(fr.querySelectorAll('.cf'), function (x) {
            qs[parseInt(x.getAttribute('data-c'), 10)] =
                x.value.toLowerCase();
        });
        var g = (gi ? gi.value : '').toLowerCase(), sv = {};
        sevboxes.forEach(function (c) { sv[c.value] = c.checked; });

        // A row is visible iff: its severity box is checked, every
        // non-empty column filter substring-matches its cell, and the
        // global query (if any) matches at least one cell.
        var vis = [];
        rows.forEach(function (tr) {
            var ok = true, cells = tr.children, i;
            if (sevboxes.length) {
                var s = tr.getAttribute('data-sev');
                if (s && sv[s] === false) ok = false;
            }
            if (ok) for (i = 0; i < ncols && i < cells.length; i++) {
                var q = qs[i];
                if (!q) continue;
                if (cells[i].textContent.toLowerCase().indexOf(q) < 0) {
                    ok = false;
                    break;
                }
            }
            if (ok && g) {
                for (i = 0, ok = false; i < cells.length; i++)
                    if (cells[i].textContent.toLowerCase().indexOf(g) >= 0) {
                        ok = true;
                        break;
                    }
            }
            if (ok) vis.push(tr);
        });

        // Clamp the current page into range, then render the slice.
        var per = sel.value === '0'
            ? (vis.length || 1)
            : (parseInt(sel.value, 10) || 25);
        var pages = Math.max(1, Math.ceil(vis.length / per));
        if (pg >= pages) pg = pages - 1;
        if (pg < 0) pg = 0;
        rows.forEach(function (tr) { tr.style.display = 'none'; });
        vis.slice(pg * per, pg * per + per)
            .forEach(function (tr) { tr.style.display = ''; });

        // Status line: "N rows [of M] — page x/y".
        info.textContent = vis.length + ' row' + (vis.length === 1 ? '' : 's') +
            (vis.length < rows.length ? ' of ' + rows.length : '') +
            (pages > 1 ? ' \u2014 page ' + (pg + 1) + '/' + pages : '');
        prev.disabled = pg <= 0;
        next.disabled = pg >= pages - 1;

        // Hide the bar entirely for trivially small, unfiltered tables.
        bar.style.display =
            (rows.length > 10 || vis.length < rows.length) ? '' : 'none';
    }
    prev.addEventListener('click', function () { pg--; apply(); });
    next.addEventListener('click', function () { pg++; apply(); });
    sel.addEventListener('change', function () { pg = 0; apply(); });

    // Frag swaps (home.js / status.js) replace innerHTML on table bodies;
    // they call tbl._apply() afterwards so filters/pagination re-run on
    // the new rows.
    tbl._apply = apply;
    apply();
}

// Enhance every matching table once the DOM is ready.
document.addEventListener('DOMContentLoaded', function () {
    document.querySelectorAll('table.ev,table.kv,table.pgtbl')
        .forEach(nmcTable);
});


/* ------------------------------------------------------------------ *
 * 3. Top-bar live poll (every 5 s).  /status.json carries two derived  *
 *    fields set by _vars_for_js():                                     *
 *      sys.pill  - CSS class suffix for the alarm pill                 *
 *                ("pill-ok" / "pill-warn" / "pill-crit")               *
 *      sys.alarm - text shown inside the pill                          *
 *    The #liveDot pulse goes grey ("bad") whenever the fetch fails,    *
 *    so the bar shows at a glance whether data is still flowing.       *
 * ------------------------------------------------------------------ */
setInterval(function () {
    fetch('/status.json').then(function (r) {
        if (!r.ok) return null;
        return r.json();
    }).then(function (d) {
        var ld = document.getElementById('liveDot');
        if (ld) ld.classList.remove('bad');
        if (!d) return;
        var p = document.getElementById('alarmPill');
        if (!p || !d['sys.pill']) return;
        var nc = 'pill ' + d['sys.pill'];
        // Touch the DOM only when something actually changed.
        if (p.className !== nc || p.textContent !== d['sys.alarm']) {
            p.className = nc;
            p.textContent = d['sys.alarm'];
        }
    }).catch(function () {
        var ld = document.getElementById('liveDot');
        if (ld) ld.classList.add('bad');
    });
}, 5000);
