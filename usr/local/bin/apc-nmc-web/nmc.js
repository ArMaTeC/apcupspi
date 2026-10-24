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
    document.querySelectorAll('table.ev,table.pgtbl')
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
        // Update buttons (About > System): lock while a run is queued or
        // live, release on ok/failed/interrupted — no page reload needed.
        var busy = (d['sys.updstate'] === 'queued' ||
                    d['sys.updstate'] === 'running');
        document.querySelectorAll('button.updbtn').forEach(function (b) {
            b.disabled = busy;
            if (busy) b.title = 'an update is in progress';
            else b.removeAttribute('title');
        });
        // Persistent on-battery banner — the alarm pill alone is easy
        // to miss during an outage (real NMCs show a full-width alert).
        var bb = document.getElementById('obbanner');
        if (bb) {
            var ob = (d['ups.status'] || '').indexOf('OB') >= 0;
            bb.hidden = !ob;
            if (ob) {
                var inf = document.getElementById('obb'),
                    t = [];
                if (d['battery.charge'])
                    t.push(d['battery.charge'] + '%');
                var s = parseInt(d['battery.runtime'], 10);
                if (s > 0) t.push(Math.floor(s / 60) + ' min left');
                inf.textContent = t.length ? ' — ' + t.join(' · ') : '';
            }
        }
    }).catch(function () {
        var ld = document.getElementById('liveDot');
        if (ld) ld.classList.add('bad');
    });
}, 5000);


/* ------------------------------------------------------------------ *
 * 4. nmcCopy(b) - clipboard helper for the About > API Token "Copy"   *
 *    button (data-tok carries the secret so it never sits in JS).     *
 * ------------------------------------------------------------------ */
function nmcCopy(b) {
    var t = b.getAttribute('data-tok') || '',
        done = function () {
            b.textContent = 'Copied!';
            setTimeout(function () { b.textContent = 'Copy'; }, 1500);
        };
    if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(t).then(done);
    } else {                                // fallback for old engines
        var i = document.createElement('textarea');
        i.value = t;
        i.style.position = 'fixed';
        i.style.opacity = '0';
        document.body.appendChild(i);
        i.select();
        try { document.execCommand('copy'); } catch (e) {}
        document.body.removeChild(i);
        done();
    }
}


/* ------------------------------------------------------------------ *
 * 5. Dirty-form guard - any edit inside a POST form flags the page;   *
 *    submitting clears it. beforeunload then warns only when an edit  *
 *    would actually be lost (nav, close, reload).                     *
 * ------------------------------------------------------------------ */
var _nmcDirty = false;
document.addEventListener('change', function (e) {
    var f = e.target.closest && e.target.closest('form');
    if (f && f.method === 'post') _nmcDirty = true;
});
document.addEventListener('submit', function () { _nmcDirty = false; });
window.addEventListener('beforeunload', function (e) {
    if (_nmcDirty) {
        e.preventDefault();
        e.returnValue = '';
    }
});


/* ------------------------------------------------------------------ *
 * 6. Event-log auto-refresh - persisted in localStorage so the        *
 *    choice survives the reload it triggers.                          *
 * ------------------------------------------------------------------ */
(function () {
    var cb = document.getElementById('evrefresh');
    if (!cb) return;
    cb.checked = localStorage.getItem('nmc-evrefresh') === '1';
    if (cb.checked) {
        setTimeout(function () { location.reload(); }, 60000);
    }
    cb.addEventListener('change', function () {
        localStorage.setItem('nmc-evrefresh', cb.checked ? '1' : '0');
        if (cb.checked) {
            setTimeout(function () { location.reload(); }, 60000);
        }
    });
})();


/* ------------------------------------------------------------------ *
 * 7. Password strength meter - heuristic bar under the new-password   *
 *    field (length + char classes; not a dictionary check).           *
 * ------------------------------------------------------------------ */
function nmcPwScore(p) {
    var s = 0;
    if (p.length >= 8) s++;
    if (p.length >= 12) s++;
    if (/[a-z]/.test(p) && /[A-Z]/.test(p)) s++;
    if (/\d/.test(p)) s++;
    if (/[^a-zA-Z0-9]/.test(p)) s++;
    return s;   /* 0..5 */
}
(function () {
    var f = document.querySelector('input[name="newp"]');
    if (!f) return;
    var bar = document.createElement('div');
    bar.style.cssText = 'height:5px;border-radius:3px;margin-top:6px;' +
        'background:var(--brd,#444);width:160px;display:none';
    var fill = document.createElement('div');
    fill.style.cssText = 'height:100%;border-radius:3px;width:0;' +
        'transition:width .2s,background .2s';
    bar.appendChild(fill);
    var lab = document.createElement('span');
    lab.className = 'note';
    lab.style.marginLeft = '8px';
    f.parentNode.appendChild(bar);
    f.parentNode.appendChild(lab);
    f.addEventListener('input', function () {
        var s = nmcPwScore(f.value),
            cols = ['#b00020', '#b00020', '#e0a800', '#e0a800',
                    '#3dcd58', '#3dcd58'],
            lbls = ['very weak', 'weak', 'fair', 'fair', 'good',
                    'strong'];
        bar.style.display = f.value ? '' : 'none';
        fill.style.width = (f.value ? 20 + s * 16 : 0) + '%';
        fill.style.background = cols[s];
        lab.textContent = f.value ? lbls[s] : '';
    });
})();


/* ------------------------------------------------------------------ *
 * 8. Fragment swap - replace a live region with the server's new      *
 *    fragment, but diff table rows first so only changed cells        *
 *    repaint. Whole-innerHTML swaps steal focus/selection and jiggle  *
 *    layout every poll; unchanged content is never touched.           *
 * ------------------------------------------------------------------ */
function nmcSwap(el, html) {
    if (el._lastFrag === html) return false;
    el._lastFrag = html;
    var tmp = el.cloneNode(false);
    tmp.innerHTML = html;
    var o = el.querySelectorAll('tr'), n = tmp.querySelectorAll('tr');
    if (o.length && o.length === n.length) {
        for (var i = 0; i < n.length; i++) {
            if (o[i].textContent !== n[i].textContent) {
                o[i].innerHTML = n[i].innerHTML;
            }
        }
    } else if (o.length !== n.length ||
               el.textContent !== tmp.textContent) {
        el.innerHTML = html;
    }
    return true;
}

var _nlc = {};
/* Live multi-series line chart on a canvas. nmcLivePush(id, {name: v})
 * appends a sample (max 120 kept) and redraws; each series is normalized
 * to its own min/max so differing units co-exist; current values render
 * into #<id>lbl. */
function nmcLivePush(id, vals) {
    var st = _nlc[id];
    if (!st) {
        st = _nlc[id] = {buf: {}, cv: document.getElementById(id),
                         lbl: document.getElementById(id + 'lbl')};
    }
    if (!st.cv) { delete _nlc[id]; return; }
    var k, v, a;
    for (k in vals) {
        v = parseFloat(vals[k]);
        if (isNaN(v)) continue;
        a = st.buf[k] = st.buf[k] || [];
        a.push(v);
        if (a.length > 120) a.shift();
    }
    var cv = st.cv, ctx = cv.getContext('2d'), W = cv.width, H = cv.height;
    ctx.clearRect(0, 0, W, H);
    var keys = Object.keys(st.buf), i, j;
    var cols = ['#4ea1ff', '#7dd87d', '#ffb84d', '#ff6b6b', '#c084fc',
                '#4dd0e1'];
    for (i = 0; i < keys.length; i++) {
        a = st.buf[keys[i]];
        var lo = Math.min.apply(null, a), hi = Math.max.apply(null, a);
        var sp = (hi - lo) || 1;
        ctx.strokeStyle = cols[i % cols.length];
        ctx.lineWidth = 1.5;
        ctx.beginPath();
        for (j = 0; j < a.length; j++) {
            var x = 4 + j * ((W - 8) / 119);
            var y = (H - 6) - ((a[j] - lo) / sp) * (H - 30);
            if (j) ctx.lineTo(x, y); else ctx.moveTo(x, y);
        }
        ctx.stroke();
    }
    if (st.lbl) {
        st.lbl.textContent = keys.map(function (kk) {
            var b = st.buf[kk];
            return kk + ': ' + b[b.length - 1];
        }).join('   ');
    }
}
