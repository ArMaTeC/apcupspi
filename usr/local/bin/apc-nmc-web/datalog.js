/*
 * datalog.js - trend chart for the Data Log page (/log/data), inlined
 * by pg_data() in apc-nmc-web.py.
 *
 * Draws four series from /datalog.json onto <canvas id="dlc"> and
 * writes a summary line into #dllbl.  Everything is scaled into the
 * 0-100 range so one Y axis serves all four series:
 *
 *   series            colour    transform        label
 *   battery.charge    green     none             Charge %
 *   ups.load          blue      none             Load %
 *   ups.realpower     red       watts / 10       Watts /10
 *   output.voltage    amber     volts - 200      Out V-200
 *
 * The chart is deliberately minimal (no axes/grid) to mimic the NMC2
 * look; if you change the series list keep the /10 and -200 labels in
 * sync with the transforms.
 */
(function () {
    var CV = document.getElementById('dlc');
    if (!CV) return;                              // canvas not on page
    var series = [['battery.charge', '#3dcd58', 'Charge %'],
                  ['ups.load', '#409fff', 'Load %'],
                  ['ups.realpower', '#d00000', 'Watts /10'],
                  ['output.voltage', '#b7791f', 'Out V-200']];

    // (Re)fetch + redraw for a range: hours=0 -> the whole log. The
    // ?hours= filter is server-side; the unfiltered endpoint stays the
    // default so other consumers keep the full array.
    function draw(h) {
        var url = '/datalog.json' + (h ? '?hours=' + h : '');
        fetch(url).then(function (r) { return r.json(); })
        .then(function (arr) {
            var cx = CV.getContext('2d'), W = CV.width, H = CV.height;
            cx.clearRect(0, 0, W, H);
            if (!arr || !arr.length) {
                document.getElementById('dllbl').textContent =
                    'No samples in this range.';
                return;
            }
            var N = arr.length, t0 = arr[0].ts, t1 = arr[N - 1].ts,
                span = Math.max(1, t1 - t0);

            // sample index + value -> canvas pixel coords, with margins
            // for the legend strip (bottom) and y-axis label (left).
            function XY(i, v) {
                return [20 + i / (N - 1) * (W - 30),
                        H - 22 - (v / 100) * (H - 34)];
            }
            cx.font = '11px sans-serif';
            series.forEach(function (s, si) {
                var vals = arr.map(function (r) {
                    return parseFloat(r[s[0]]);
                });
                if (si === 2) vals = vals.map(function (v) { return v / 10; });
                if (si === 3) vals = vals.map(function (v) { return v - 200; });
                cx.strokeStyle = s[1];
                cx.lineWidth = 1.6;
                cx.beginPath();
                var started = false;
                vals.forEach(function (v, i) {
                    if (isNaN(v)) return;         // gap in the samples
                    var p = XY(i, Math.max(0, Math.min(100, v)));
                    started ? cx.lineTo(p[0], p[1]) : cx.moveTo(p[0], p[1]);
                    started = true;
                });
                cx.stroke();
                // legend swatch: coloured label along the bottom edge
                cx.fillStyle = s[1];
                cx.fillText(s[2], 26 + si * 110, H - 6);
                cx.fillStyle = '#888';
            });
            var d0 = new Date(t0 * 1000), d1 = new Date(t1 * 1000);
            document.getElementById('dllbl').textContent =
                N + ' samples, ' + d0.toLocaleString() +
                ' to ' + d1.toLocaleString();
        }).catch(function () {
            document.getElementById('dllbl').textContent =
                'Chart unavailable.';
        });
    }

    // Range buttons: 24 h is the default view — a full log redraw is
    // only a click away and doesn't need to load on page entry.
    document.querySelectorAll('.dlrange').forEach(function (b) {
        b.onclick = function () {
            document.querySelectorAll('.dlrange').forEach(function (x) {
                x.classList.remove('act');
            });
            b.classList.add('act');
            draw(parseFloat(b.getAttribute('data-h')) || 0);
        };
    });
    draw(24);
    setInterval(function () {               // live: new samples each minute
        var act = document.querySelector('.dlrange.act');
        if (act) draw(parseFloat(act.getAttribute('data-h')) || 0);
    }, 60000);

    // ---- paged table, lazy loaded ------------------------------------
    // Rows come from /datalog.json?off=&lim= (newest first) a page at a
    // time; an IntersectionObserver on #dlsent fetches the next page as
    // the bottom of the table scrolls into view. #dlmore is the manual
    // fallback for browsers without IntersectionObserver.
    var TB = document.getElementById('dlrows');
    if (!TB) return;
    var PAGE = 150, off = 0, total = Infinity, busy = false, done = false;
    var sent = document.getElementById('dlsent');
    var btn = document.getElementById('dlmore');
    var note = document.getElementById('dlnote');
    var KEYS = ['battery.charge', 'ups.load', 'ups.realpower',
                'input.voltage', 'output.voltage',
                'battery.temperature', 'st'];

    function pad2(n) { return (n < 10 ? '0' : '') + n; }

    function td(t) {
        var d = document.createElement('td');
        d.textContent = t;
        return d;
    }

    function mkrow(r) {
        var tr = document.createElement('tr');
        var d = new Date(r.ts * 1000);
        tr.appendChild(td(pad2(d.getMonth() + 1) + '/' + pad2(d.getDate()) +
                          '/' + d.getFullYear()));
        tr.appendChild(td(pad2(d.getHours()) + ':' + pad2(d.getMinutes())));
        KEYS.forEach(function (k) {
            var v = r[k];
            tr.appendChild(td(v === undefined || v === null || v === '' ?
                              '-' : String(v)));
        });
        return tr;
    }

    function updateNote() {
        if (note && total !== Infinity)
            note.textContent = 'Sampled every 60 s, newest first \u2014 ' +
                off + ' of ' + total + ' samples shown.';
    }

    function finish() {
        done = true;
        if (btn) btn.style.display = 'none';
        if (sent && sent.parentNode) sent.parentNode.removeChild(sent);
        if (off === 0)
            TB.innerHTML = '<tr><td colspan="9" class="note">No samples ' +
                'yet &mdash; the data log starts filling within a minute ' +
                'of service start.</td></tr>';
    }

    function loadMore() {
        if (busy || done) return;
        busy = true;
        fetch('/datalog.json?off=' + off + '&lim=' + PAGE)
            .then(function (r) { return r.json(); })
            .then(function (p) {
                total = p.total;
                if (off === 0) TB.textContent = '';
                p.rows.forEach(function (r) { TB.appendChild(mkrow(r)); });
                off += p.rows.length;
                updateNote();
                if (!p.rows.length || off >= total) finish();
                busy = false;
            })
            .catch(function () {
                busy = false;
                if (off === 0)
                    TB.innerHTML = '<tr><td colspan="9" class="note">' +
                        'Failed to load samples.</td></tr>';
            });
    }

    if (btn) {
        btn.addEventListener('click', loadMore);
        if (!('IntersectionObserver' in window))
            btn.style.display = '';
    }
    if ('IntersectionObserver' in window && sent) {
        var io = new IntersectionObserver(function (entries) {
            entries.forEach(function (e) {
                if (e.isIntersecting) loadMore();
            });
        });
        io.observe(sent);
    }
    loadMore();                                   // first page right away
})();
