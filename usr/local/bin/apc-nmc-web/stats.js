/* stats.js - Statistics page: per-metric trend charts.
 *
 * Anchored by /stats.json?win=H (server-decimated pts + min/max/avg).
 * Then every 5 s /status.json appends a live point per metric — the
 * tail of each chart moves in real time between 60 s log samples.
 * stats.json is re-anchored every 60 s so rolling windows stay clean. */
(function () {
    var MAP = {
        c_charge: 'battery.charge', c_load: 'ups.load',
        c_watts: 'ups.realpower', c_inv: 'input.voltage',
        c_outv: 'output.voltage', c_temp: 'battery.temperature'
    };
    var ST = {};                    // id -> {pts,min,max,avg,n,last}

    function draw(id) {
        var cv = document.getElementById(id), s = ST[id];
        if (!cv || !s || !s.pts.length) return;
        var ctx = cv.getContext('2d'), W = cv.width, H = cv.height;
        ctx.clearRect(0, 0, W, H);
        var a = s.pts, lo = s.min, hi = s.max, sp = (hi - lo) || 1;
        ctx.strokeStyle = '#4ea1ff'; ctx.lineWidth = 1.4;
        ctx.beginPath();
        a.forEach(function (p, i) {
            var x = 4 + i * ((W - 8) / Math.max(1, a.length - 1));
            var y = (H - 18) - ((p[1] - lo) / sp) * (H - 30);
            if (i) ctx.lineTo(x, y); else ctx.moveTo(x, y);
        });
        ctx.stroke();
        var ay = (H - 18) - ((s.avg - lo) / sp) * (H - 30);
        ctx.strokeStyle = '#ffb84d'; ctx.setLineDash([4, 4]);
        ctx.beginPath(); ctx.moveTo(4, ay); ctx.lineTo(W - 4, ay);
        ctx.stroke(); ctx.setLineDash([]);
        ctx.fillStyle = '#888'; ctx.font = '10px sans-serif';
        ctx.fillText(hi.toFixed(1), 6, 11);
        ctx.fillText(lo.toFixed(1), 6, H - 6);
        ctx.fillStyle = '#ffb84d';
        ctx.fillText('avg ' + s.avg.toFixed(1), W - 66, ay - 4);
        var t0 = new Date(a[0][0] * 1000), t1 = new Date(a[a.length - 1][0] * 1000);
        ctx.fillStyle = '#888';
        ctx.fillText(t0.toLocaleDateString() + ' ' + t0.toLocaleTimeString(),
                     6, 22);
        var t1s = t1.toLocaleDateString() + ' ' + t1.toLocaleTimeString();
        ctx.fillText(t1s, W - 6 - ctx.measureText(t1s).width, 22);
    }

    function label(id) {
        var s = ST[id], lb = document.getElementById(id + '_lbl');
        if (lb && s) {
            lb.textContent = s.n + ' samples — min ' + s.min.toFixed(1) +
                ' / avg ' + s.avg.toFixed(1) + ' / max ' +
                s.max.toFixed(1) + ' / now ' + s.last.toFixed(1);
        }
    }

    function anchor(win) {          // full re-anchor from the log
        fetch('/stats.json?win=' + win).then(function (r) {
            return r.json();
        }).then(function (d) {
            var tot = document.getElementById('st_total');
            if (tot && d.total !== undefined) tot.textContent = d.total;
            var sv = d.series || {};
            Object.keys(MAP).forEach(function (id) {
                var s = sv[MAP[id]];
                if (s) ST[id] = s;
                var lb = document.getElementById(id + '_lbl');
                if (!s && lb) lb.textContent = 'no data in this window';
            });
            Object.keys(MAP).forEach(function (id) {
                draw(id); label(id);
            });
        }).catch(function () {});
    }

    function live() {               // append the current reading (5 s)
        fetch('/status.json').then(function (r) {
            return r.json();
        }).then(function (d) {
            var now = Math.floor(Date.now() / 1000);
            Object.keys(MAP).forEach(function (id) {
                var s = ST[id]; if (!s) return;
                var v = parseFloat(d[MAP[id]]);
                if (isNaN(v)) return;
                s.pts.push([now, v]);
                if (s.pts.length > 900) s.pts.shift();
                s.last = v;
                if (v < s.min) s.min = v;
                if (v > s.max) s.max = v;
                draw(id); label(id);
            });
        }).catch(function () {});
    }

    document.querySelectorAll('.strng').forEach(function (b) {
        b.onclick = function () {
            document.querySelectorAll('.strng').forEach(function (x) {
                x.classList.remove('act');
            });
            b.classList.add('act');
            anchor(parseInt(b.getAttribute('data-h'), 10) || 0);
        };
    });
    anchor(24);
    setInterval(live, 5000);
    setInterval(function () {
        var act = document.querySelector('.strng.act');
        if (act) anchor(parseInt(act.getAttribute('data-h'), 10) || 0);
    }, 60000);
})();
