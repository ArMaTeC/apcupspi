/*
 * config.js - behaviour for the UPS Configuration page (/config/ups),
 * inlined by pg_config() in apc-nmc-web.py.
 *
 * NMC_INIT.csrf is the session CSRF token (a JSON string emitted by the
 * Python template just before this script).
 */

/* bkRestore() - wired to the "Restore" button (onclick="bkRestore()")
 * in the Backup / Restore card.
 *
 * The restore endpoint takes the raw .tgz body via POST rather than a
 * form upload, so the CSRF token travels in the query string instead of
 * a hidden _csrf field (the handler checks it the same way).  On
 * success the server restarts apc-nmc-web itself ~1.5 s later, so the
 * "OK" message is as much as the user gets before the page dies.
 */
function bkRestore() {
    var f = document.getElementById('bkfile').files[0],
        m = document.getElementById('bkmsg');
    if (!f) { m.textContent = 'Choose a backup file first.'; return; }
    if (!confirm('Restore configuration from "' + f.name +
                 '"? The interface will restart.')) return;
    m.textContent = 'Uploading\u2026';
    fetch('/config/restore?_csrf=' + encodeURIComponent(NMC_INIT.csrf),
          { method: 'POST', body: f })
        .then(function (r) {
            // Always read the body - it carries the server's reason text
            // on failure as well as the success message.
            return r.text().then(function (t) {
                m.textContent =
                    (r.ok ? ('OK \u2014 ') : ('Failed \u2014 ')) + t;
            });
        })
        .catch(function (e) { m.textContent = 'Upload failed: ' + e; });
}
