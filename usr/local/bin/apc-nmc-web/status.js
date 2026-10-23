/*
 * status.js - live refresh for the UPS Status page (/status/ups),
 * inlined by pg_status() in apc-nmc-web.py.
 *
 * Every 5 s it fetches /status.json?frag=1 and swaps in the "frag"
 * fragments by element id (stTop/stInp/stOut/stTimers/stEnv - the
 * server-rendered <tbody>/card contents from _status_frags()).
 *
 * After a swap inside a table, tbl._apply() (installed by nmcTable in
 * nmc.js) re-runs filtering/pagination so the new rows don't bypass an
 * active filter.  Fetch failures are ignored - the next poll retries.
 */
setInterval(function () {
    fetch('/status.json?frag=1').then(r => r.json())
        .then(function (d) {
            var f = d.frag || {};
            for (var k in f) {
                var el = document.getElementById(k);
                // compare against the last raw fragment, not innerHTML —
                // the browser normalizes markup on write (entities,
                // attribute quoting) so innerHTML !== server string even
                // when nothing changed, causing a swap + repaint flicker
                // every poll.
                if (el && el._lastFrag !== f[k]) {
                    el._lastFrag = f[k];
                    el.innerHTML = f[k];
                    var t = el.closest('table');
                    if (t && t._apply) t._apply();
                }
            }
        }).catch(function () { });
}, 5000);
