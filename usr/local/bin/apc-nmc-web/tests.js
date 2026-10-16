/*
 * tests.js - live refresh for the Diagnostics page (/test/diag),
 * inlined by pg_tests() in apc-nmc-web.py.
 *
 * Why this page needs a different mechanism: starting a self test or
 * runtime calibration changes button/field state (e.g. "Initiate UPS
 * Self Test" becomes "Self Test In Progress...", calibration forms get
 * disabled), so on any change of ups.status we re-render the dynamic
 * region rather than just updating fields.
 *
 * NMC_INIT.status is the ups.status at page-render time (a JSON string
 * emitted by the Python template just before this script).  Every 4 s
 * we poll /status.json; when ups.status differs, we refetch the current
 * URL, parse the response, and swap #pagebody's innerHTML - a partial
 * reload that keeps the URL, scroll position and nav state intact.
 */
(function () {
    var s = NMC_INIT.status;
    setInterval(function () {
        fetch('/status.json').then(function (r) { return r.json(); })
            .then(function (d) {
                if (d['ups.status'] !== s) {
                    s = d['ups.status'];           // track the new state
                    fetch(location.pathname)
                        .then(function (r) { return r.text(); })
                        .then(function (h) {
                            // Extract #pagebody from the freshly rendered
                            // page and graft it onto this document.
                            var neo = new DOMParser()
                                .parseFromString(h, 'text/html')
                                .getElementById('pagebody');
                            var el = document.getElementById('pagebody');
                            if (el && neo) el.innerHTML = neo.innerHTML;
                        }).catch(function () { });
                }
            }).catch(function () { });
    }, 4000);
})();
