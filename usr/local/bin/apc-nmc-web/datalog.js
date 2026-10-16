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
    fetch('/datalog.json').then(function (r) { return r.json(); })
        .then(function (arr) {
            var cx = CV.getContext('2d'), W = CV.width, H = CV.height;
            if (!arr || !arr.length) {
                document.getElementById('dllbl').textContent =
                    'No samples yet.';
                return;
            }
            var series = [['battery.charge', '#3dcd58', 'Charge %'],
                          ['ups.load', '#409fff', 'Load %'],
                          ['ups.realpower', '#d00000', 'Watts /10'],
                          ['output.voltage', '#b7791f', 'Out V-200']];
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
})();
