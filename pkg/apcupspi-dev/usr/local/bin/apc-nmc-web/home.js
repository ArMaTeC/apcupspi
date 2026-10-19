/*
 * home.js - live refresh for the Home page (/home), inlined at the end
 * of the page by pg_home() in apc-nmc-web.py.
 *
 * Every 10 s it fetches /status.json?frag=1, which returns all UPS vars
 * plus a "frag" object of server-rendered HTML fragments keyed by the
 * element id they replace (see _home_frags()/_status_frags() in the
 * Python - ids hs/hp/ht/he are the home page's live regions).
 *
 * Two update mechanisms:
 *   - [data-var] elements: raw var values dropped into span-like slots
 *     (the tile values), with unit formatting done here.
 *   - frag swaps: whole innerHTML replacement for composite regions.
 *
 * Changed values flash via the .uv class (keyframes "uv" in nmc.css);
 * "void el.offsetWidth" forces a reflow so the animation restarts on
 * every update.  After a frag swap inside a table, tbl._apply() (set by
 * nmcTable in nmc.js) re-applies filters/pagination to the new rows.
 */
setInterval(function () {
    fetch('/status.json?frag=1').then(r => r.json())
        .then(function (d) {

            // --- scalar tile values: <... data-var="ups.load"> etc. ---
            document.querySelectorAll('[data-var]').forEach(function (el) {
                var v = d[el.dataset.var];
                if (v === undefined) return;      // UPS didn't report it
                if (el.dataset.var === 'battery.runtime') {
                    // seconds -> "Xh Ym"
                    v = Math.floor(v / 3600) + 'h ' +
                        Math.floor(v % 3600 / 60) + 'm';
                }
                else if (el.dataset.var === 'battery.charge' ||
                         el.dataset.var === 'ups.load') v = v + ' %';
                else if (el.dataset.var === 'output.voltage' ||
                         el.dataset.var === 'input.voltage') v = v + ' V';
                else if (el.dataset.var === 'ups.realpower') v = v + ' W';
                else if (el.dataset.var === 'battery.temperature')
                    v = v + ' °C';
                if (el.textContent != v) {
                    el.textContent = v;
                    el.classList.remove('uv');
                    void el.offsetWidth;          // restart the flash
                    el.classList.add('uv');
                }
            });

            // --- composite fragments by element id ---
            var f = d.frag || {};
            for (var k in f) {
                var el = document.getElementById(k);
                if (el && el.innerHTML !== f[k]) {
                    el.innerHTML = f[k];
                    // flash only the text-y fragments, not whole tables
                    if (k === 'hs' || k === 'ht') {
                        el.classList.remove('uv');
                        void el.offsetWidth;
                        el.classList.add('uv');
                    }
                    var t = el.closest('table');
                    if (t && t._apply) t._apply();   // re-filter new rows
                }
            }
        }).catch(function () { });   // stay silent on transient failures
}, 10000);
