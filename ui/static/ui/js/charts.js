/* Chart wrappers. Data comes from <script type="application/json"> blocks
   (Django json_script), colours from CSS tokens so light/dark stay in sync. */
(function () {
  "use strict";
  if (!window.Chart) return;

  const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  const eur0 = new Intl.NumberFormat("en-GB", { style: "currency", currency: "EUR", maximumFractionDigits: 0 });
  const eur2 = new Intl.NumberFormat("en-GB", { style: "currency", currency: "EUR" });
  const compact = new Intl.NumberFormat("en-GB", { notation: "compact", maximumFractionDigits: 1 });
  const dayFmt = new Intl.DateTimeFormat("en-GB", { day: "numeric", month: "short", year: "numeric" });
  const monFmt = new Intl.DateTimeFormat("en-GB", { month: "short", year: "2-digit" });
  const yearFmt = new Intl.DateTimeFormat("en-GB", { year: "numeric" });
  const DAY = 86400000;

  // Ticks on 1 January every 1/2/5/10 years, for long ranges.
  const yearTicks = (lo, hi) => {
    const first = new Date(lo).getFullYear() + 1;
    const last = new Date(hi).getFullYear();
    const step = [1, 2, 5, 10, 20].find((s) => (last - first + 1) / s <= 6) || 50;
    const ticks = [];
    for (let y = Math.ceil(first / step) * step; y <= last; y += step) {
      ticks.push({ value: new Date(y, 0, 1).getTime() });
    }
    return ticks;
  };
  const axisTick = (v) => (Math.abs(v) >= 10000 ? "€" + compact.format(v) : eur0.format(v));
  const withAlpha = (hex, a) => {
    const h = hex.replace("#", "");
    const n = parseInt(h.length === 3 ? h.replace(/./g, "$&$&") : h, 16);
    return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${a})`;
  };

  // ------------------------------------------------------------ tooltip
  function tooltipHandler(titleFmt) {
    return (ctx) => {
      const { chart, tooltip } = ctx;
      let el = chart.canvas.parentNode.querySelector(".tip");
      if (!el) {
        el = document.createElement("div");
        el.className = "tip";
        chart.canvas.parentNode.appendChild(el);
      }
      if (tooltip.opacity === 0) { el.style.opacity = 0; return; }
      el.replaceChildren();
      const title = document.createElement("div");
      title.className = "t-title";
      title.textContent = titleFmt(tooltip.dataPoints[0]);
      el.appendChild(title);
      for (const dp of tooltip.dataPoints) {
        if (dp.parsed.y == null || Number.isNaN(dp.parsed.y)) continue;
        const row = document.createElement("div");
        row.className = "t-row";
        const name = document.createElement("span");
        const key = document.createElement("i");
        key.style.background = dp.dataset.borderColor || dp.dataset.backgroundColor;
        name.append(key, document.createTextNode(dp.dataset.label));
        const val = document.createElement("b");
        val.textContent = eur2.format(dp.parsed.y);
        row.append(name, val);
        el.appendChild(row);
      }
      const { offsetWidth: w } = chart.canvas.parentNode;
      let left = tooltip.caretX + 14;
      if (left + el.offsetWidth > w) left = tooltip.caretX - el.offsetWidth - 14;
      el.style.left = Math.max(0, left) + "px";
      el.style.top = Math.max(0, tooltip.caretY - el.offsetHeight / 2) + "px";
      el.style.opacity = 1;
    };
  }

  // Vertical hairline at the hovered X.
  const crosshair = {
    id: "crosshair",
    afterDatasetsDraw(chart) {
      const active = chart.tooltip && chart.tooltip.getActiveElements();
      if (!active || !active.length) return;
      const x = active[0].element.x;
      const { top, bottom } = chart.chartArea;
      const c = chart.ctx;
      c.save();
      c.strokeStyle = css("--ink-3");
      c.lineWidth = 1;
      c.beginPath();
      c.moveTo(Math.round(x) + 0.5, top);
      c.lineTo(Math.round(x) + 0.5, bottom);
      c.stroke();
      c.restore();
    },
  };

  function baseOptions() {
    const grid = css("--hair");
    const ink3 = css("--ink-3");
    Chart.defaults.font.family = css("--sans");
    Chart.defaults.font.size = 11;
    Chart.defaults.color = ink3;
    return {
      responsive: true,
      maintainAspectRatio: false,
      animation: { duration: 300 },
      interaction: { mode: "index", intersect: false },
      plugins: { legend: { display: false } },
      scales: {
        y: {
          border: { display: false },
          grid: { color: grid, lineWidth: 1, drawTicks: false },
          ticks: { padding: 10, maxTicksLimit: 5, callback: axisTick },
        },
      },
    };
  }

  // Dashed vertical line + label at spec.marker (e.g. "today" on a projection).
  const marker = {
    id: "marker",
    afterDraw(chart, _args, opts) {
      if (opts.x == null) return;
      const x = chart.scales.x.getPixelForValue(opts.x);
      const { top, bottom, left, right } = chart.chartArea;
      if (x < left || x > right) return;
      const c = chart.ctx;
      c.save();
      c.strokeStyle = css("--ink-3");
      c.setLineDash([2, 3]);
      c.beginPath();
      c.moveTo(Math.round(x) + 0.5, top);
      c.lineTo(Math.round(x) + 0.5, bottom);
      c.stroke();
      c.fillStyle = css("--ink-3");
      c.font = `11px ${css("--sans")}`;
      c.textAlign = x > right - 40 ? "right" : "left";
      c.fillText(opts.label || "", x + (c.textAlign === "left" ? 6 : -6), top + 10);
      c.restore();
    },
  };

  const registry = [];
  function register(fn) { registry.push(fn); fn(); }
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => registry.forEach((f) => f()));

  // ------------------------------------------------------------ line chart
  // spec: {series: [{label, color: "--s1", points: [[iso, value|null], ...], fill, dash, thin}],
  //        ranges, marker: iso}
  // Range buttons: data-days = look back from now (0 = from the start),
  // data-ahead = look ahead from now (absent = to the end).
  function lineChart(canvas, spec) {
    let chart;
    let minX = null;
    let maxX = null;
    const render = () => {
      if (chart) chart.destroy();
      const surface = css("--paper");
      const datasets = spec.series.map((s) => {
        const color = css(s.color);
        return {
          label: s.label,
          data: s.points.map(([d, v]) => ({ x: Date.parse(d), y: v })),
          borderColor: color,
          backgroundColor: s.fill ? withAlpha(color, 0.1) : color,
          fill: s.fill ? "origin" : false,
          borderWidth: s.thin ? 1 : 2,
          borderDash: s.dash ? [4, 4] : [],
          borderJoinStyle: "round",
          borderCapStyle: "round",
          // Short series (a few syncs so far) get visible markers.
          pointRadius: s.points.length <= 12 ? 4 : 0,
          pointBorderWidth: 2,
          pointBorderColor: surface,
          pointBackgroundColor: color,
          pointHoverRadius: 4,
          pointHoverBorderWidth: 2,
          pointHoverBorderColor: surface,
          pointHoverBackgroundColor: color,
          stepped: s.stepped ? "before" : false,
          tension: 0,
        };
      });
      const xs = datasets.flatMap((d) => d.data.map((p) => p.x));
      const lo = Math.max(minX ?? -Infinity, Math.min(...xs));
      const hi = Math.min(maxX ?? Infinity, Math.max(...xs));
      const tickFmt = hi - lo > 4 * 365 * DAY ? yearFmt : monFmt;
      const opts = baseOptions();
      opts.scales.x = {
        type: "linear",
        min: lo,
        max: hi,
        border: { color: css("--hair") },
        grid: { display: false },
        ticks: { maxTicksLimit: 6, padding: 8, callback: (v) => tickFmt.format(new Date(v)) },
      };
      if (tickFmt === yearFmt) opts.scales.x.afterBuildTicks = (scale) => { scale.ticks = yearTicks(lo, hi); };
      opts.plugins.marker = { x: spec.marker ? Date.parse(spec.marker) : null, label: "Today" };
      opts.plugins.tooltip = {
        enabled: false,
        external: tooltipHandler((dp) => dayFmt.format(new Date(dp.parsed.x))),
      };
      chart = new Chart(canvas, { type: "line", data: { datasets }, options: opts, plugins: [crosshair, marker] });
    };
    register(render);

    const ranges = spec.ranges && document.querySelector(spec.ranges);
    if (ranges) {
      ranges.addEventListener("click", (e) => {
        const b = e.target.closest("button[data-days]");
        if (!b) return;
        ranges.querySelectorAll("button").forEach((x) => x.setAttribute("aria-pressed", x === b));
        const days = Number(b.dataset.days);
        minX = days ? Date.now() - days * DAY : null;
        maxX = b.dataset.ahead == null ? null : Date.now() + Number(b.dataset.ahead) * DAY;
        render();
      });
    }
  }

  // ------------------------------------------------------------ bar chart
  // spec: {labels: ["2026-01", ...], series: [{label, color, data: [...]}], stacked}
  function barChart(canvas, spec) {
    let chart;
    register(() => {
      if (chart) chart.destroy();
      const datasets = spec.series.map((s) => ({
        label: s.label,
        data: s.data,
        backgroundColor: css(s.color),
        borderColor: css(s.color),
        borderWidth: 0,
        borderSkipped: "start",
        borderRadius: { topLeft: 4, topRight: 4 },
        maxBarThickness: 24,
        // 2px surface gap between stacked segments
        ...(spec.stacked ? { borderWidth: { top: 2 }, borderColor: css("--paper"), borderRadius: 4 } : {}),
      }));
      const opts = baseOptions();
      opts.scales.x = {
        stacked: !!spec.stacked,
        border: { color: css("--hair") },
        grid: { display: false },
        ticks: {
          maxTicksLimit: 12,
          autoSkip: true,
          maxRotation: 0,
          callback(v) { return monFmt.format(new Date(this.getLabelForValue(v) + "-01")); },
        },
      };
      opts.scales.y.stacked = !!spec.stacked;
      opts.plugins.tooltip = {
        enabled: false,
        external: tooltipHandler((dp) =>
          new Intl.DateTimeFormat("en-GB", { month: "long", year: "numeric" }).format(new Date(dp.label + "-01"))),
      };
      chart = new Chart(canvas, { type: "bar", data: { labels: spec.labels, datasets }, options: opts });
    });
  }

  // ------------------------------------------------------------ boot
  document.querySelectorAll("canvas[data-chart]").forEach((canvas) => {
    const src = document.getElementById(canvas.dataset.source);
    if (!src) return;
    const spec = JSON.parse(src.textContent);
    (canvas.dataset.chart === "bar" ? barChart : lineChart)(canvas, spec);
  });
})();
