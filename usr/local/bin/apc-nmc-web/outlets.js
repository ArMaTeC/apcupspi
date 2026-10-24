/*
 * outlets.js - live refresh for the Outlet Groups page
 * (/control/outlets), inlined by pg_outlets() in apc-nmc-web.py.
 *
 * Every 3 s it polls /status.json and updates each
 * <tr data-gidx="N"> row from the outlet.group.N.status var:
 *   .dot    - green/grey status dot
 *   .gstat  - raw status text ("on"/"off"/...)
 *   .swin   - the toggle-switch checkbox position
 *   .swact  - hidden form field holding the inverse action the switch
 *             will submit (so flipping it requests off when on)
 *   .gsw    - the "On"/"Off" label text beside the switch
 *
 * Rows whose group index has no reported status are left untouched
 * (e.g. a UPS without switchable groups).
 */
setInterval(function () {
    fetch('/status.json').then(r => r.json()).then(d => {
        document.querySelectorAll('tr[data-gidx]').forEach(function (tr) {
            var st = d['outlet.group.' + tr.dataset.gidx + '.status'];
            if (st === undefined) return;
            var on = (st === 'on');
            tr.querySelector('.dot').className = 'dot ' + (on ? 'on' : 'off');
            tr.querySelector('.gstat').textContent =
                (st === 'on') ? 'On' : (st === 'off') ? 'Off' : st;
            tr.querySelector('.swin').checked = on;
            tr.querySelector('.swact').value = on ? 'off' : 'on';
            tr.querySelector('.gsw').textContent = on ? 'On' : 'Off';
        });
    }).catch(function () { });
}, 3000);
