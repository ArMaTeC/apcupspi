/* stats.js - Statistics page.
 *
 * Crisp rendering: every canvas is sized to its CSS box ×
 * devicePixelRatio, so lines/text stay sharp on any screen.
 * Anchored by /stats.json?win=H; /status.json appends a live point
 * every 5 s so chart tails move in real time; 60 s re-anchor keeps
 * rolling windows accurate. Donuts seeded once by the server
 * (window.ST_SEED) then updated from the same status.json feed. */
(function () {
    var MAP = {
        c_charge: 'battery.charge', c_load: 'ups.load',
        c_watts: 'ups.realpower', c_inv: 'input.voltage',
        c_outv: 'output.voltage', c_temp: 'battery.temperature',
        c_va: 'ups.power', c_freq: 'output.frequency'
    };
    var ST = {};                    // id -> {pts,min,max,avg,n,last}
    var DPR = window.devicePixelRatio || 1;

    function fit(cv, w, h) {        // HiDPI-aware sizing
        var cw = cv.clientWidth || w;
        cv.width = Math.round(cw * DPR);
        cv.height = Math.round((w === 300 ? 300 : cw * 200 / 620) * DPR);
    }

    function draw(id) {
        var cv = document.getElementById(id), s = ST[id];
        if (!cv || !s || !s.pts.length) return;
        fit(cv, 620, 200);
        var W = cv.width, H = cv.height;
        var ctx = cv.getContext('2d');
        ctx.clearRect(0, 0, W, H);
        var a = s.pts, lo = s.min, hi = s.max, sp = (hi - lo) || 1;
        ctx.strokeStyle = '#4ea1ff'; ctx.lineWidth = 1.4 * DPR;
        ctx.beginPath();
        a.forEach(function (p, i) {
            var x = 4 * DPR + i * ((W - 8 * DPR) / Math.max(1, a.length - 1));
            var y = (H - 18 * DPR) - ((p[1] - lo) / sp) * (H - 30 * DPR);
            if (i) ctx.lineTo(x, y); else ctx.moveTo(x, y);
        });
        ctx.stroke();
        var ay = (H - 18 * DPR) - ((s.avg - lo) / sp) * (H - 30 * DPR);
        ctx.strokeStyle = '#ffb84d'; ctx.setLineDash([4, 4]);
        ctx.lineWidth = DPR;
        ctx.beginPath(); ctx.moveTo(4 * DPR, ay); ctx.lineTo(W - 4 * DPR, ay);
        ctx.stroke(); ctx.setLineDash([]);
        ctx.fillStyle = '#888';
        ctx.font = (10 * DPR) + 'px sans-serif';
        ctx.fillText(hi.toFixed(1), 6 * DPR, 11 * DPR);
        ctx.fillText(lo.toFixed(1), 6 * DPR, H - 6 * DPR);
        ctx.fillStyle = '#ffb84d';
        ctx.fillText('avg ' + s.avg.toFixed(1), W - 66 * DPR, ay - 4 * DPR);
        var t0 = new Date(a[0][0] * 1000), t1 = new Date(a[a.length - 1][0] * 1000);
        ctx.fillStyle = '#888';
        ctx.fillText(t0.toLocaleDateString() + ' ' + t0.toLocaleTimeString(),
                     6 * DPR, 22 * DPR);
        var t1s = t1.toLocaleDateString() + ' ' + t1.toLocaleTimeString();
        ctx.fillText(t1s, W - 6 * DPR - ctx.measureText(t1s).width, 22 * DPR);
    }

    function donut(id, parts, colors, center) {
        var cv = document.getElementById(id); if (!cv) return;
        cv.width = 300 * DPR; cv.height = 300 * DPR;
        var ctx = cv.getContext('2d'), W = cv.width, H = cv.height,
            cx = W / 2, cy = H / 2, r = Math.min(cx, cy) - 8 * DPR,
            r2 = r * 0.62;
        ctx.clearRect(0, 0, W, H);
        var tot = parts.reduce(function (x, v) { return x + v; }, 0) || 1,
            a0 = -Math.PI / 2;
        parts.forEach(function (v, i) {
            var a1 = a0 + (v / tot) * Math.PI * 2;
            ctx.beginPath(); ctx.moveTo(cx, cy);
            ctx.arc(cx, cy, r, a0, a1); ctx.closePath();
            ctx.fillStyle = colors[i]; ctx.fill();
            a0 = a1;
        });
        ctx.globalCompositeOperation = 'destination-out';
        ctx.beginPath(); ctx.arc(cx, cy, r2, 0, Math.PI * 2); ctx.fill();
        ctx.globalCompositeOperation = 'source-over';
        if (center) {                 // big value centred in the hole
            ctx.fillStyle = '#555';
            ctx.font = '700 ' + (34 * DPR) + 'px sans-serif';
            ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
            ctx.fillText(center, cx, cy + 2 * DPR);
        }
    }

    function label(id) {
        var s = ST[id], lb = document.getElementById(id + '_lbl');
        if (lb && s) {
            lb.textContent = s.n + ' samples — min ' + s.min.toFixed(1) +
                ' / avg ' + s.avg.toFixed(1) + ' / max ' +
                s.max.toFixed(1) + ' / now ' + s.last.toFixed(1);
        }
    }

    var seed = window.ST_SEED || {};
    function drawDonuts() {
        var chg = seed.chg || 0, ld = seed.ld || 0;
        var sev = seed.sev || {crit: 0, warn: 0, info: 0};
        donut('dn_chg', [chg, Math.max(0, 100 - chg)],
              ['#3dcd58', 'rgba(120,120,120,.25)'],
              chg.toFixed(0) + '%');
        donut('dn_load', [ld, Math.max(0, 100 - ld)],
              ['#ffb84d', 'rgba(120,120,120,.25)'],
              ld.toFixed(0) + '%');
        donut('dn_sev', [sev.crit || 0, sev.warn || 0, sev.info || 0],
              ['#e05252', '#ffb84d', '#3dcd58'],
              String((sev.crit || 0) + (sev.warn || 0) + (sev.info || 0)));
    }

    function anchor(win) {
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

    function live() {
        fetch('/status.json').then(function (r) {
            return r.json();
        }).then(function (d) {
            var now = Math.floor(Date.now() / 1000);
            var chg = parseFloat(d['battery.charge']),
                ld = parseFloat(d['ups.load']);
            if (!isNaN(chg)) seed.chg = chg;
            if (!isNaN(ld)) seed.ld = ld;
            drawDonuts();
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
    drawDonuts();
    setInterval(live, 5000);
    setInterval(function () {
        var act = document.querySelector('.strng.act');
        if (act) anchor(parseInt(act.getAttribute('data-h'), 10) || 0);
    }, 60000);
    window.addEventListener('resize', function () {
        Object.keys(MAP).forEach(draw);
    });
})();
