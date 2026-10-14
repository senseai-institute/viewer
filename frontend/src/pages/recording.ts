// Copyright 2026 SenseAI Institute
// SPDX-License-Identifier: Apache-2.0
// Recording view: stats card, stacked per-source timeline, and a scrubber bound to it.

import uPlot from "uplot";
import { api, nums, xnums, type Columnar, type Manifest, type SourceEntry, type ThumbEntry, type ThumbIndex } from "../api";
import { badge, DASH, fmtDate, fmtDuration, fmtInt, fmtMs, fmtRate, h, na } from "../format";
import { axis, barSeries, cssVar, dotSeries, lineSeries, PALETTE, spacerAxis, stepSeries, xAxis } from "../plot";

type Cleanup = () => void;

export async function renderRecording(root: HTMLElement, id: string, initialTick: number | null): Promise<Cleanup> {
  root.replaceChildren(h("p", { class: "loading" }, "Loading recording…"));
  const m = await api.manifest(id);
  if (!m) {
    root.replaceChildren(backLink(), h("div", { class: "card" }, h("h1", {}, "Recording not found"), h("p", { class: "sub" }, id)));
    return () => {};
  }
  const head = h(
    "div",
    { class: "page-head" },
    backLink(),
    h("h1", {}, m.recording_id),
    h(
      "p",
      { class: "sub" },
      [m.dataset, fmtDuration(m.duration_s), `${m.sources.length} sources`, m.source_file, `exported ${fmtDate(m.created_at)}`]
        .filter(Boolean)
        .join(" · "),
    ),
  );
  if (!m.supported) {
    root.replaceChildren(head, h("div", { class: "card warn-card" }, `Summary version ${m.version} not supported: re-export this recording.`));
    return () => {};
  }

  const [timeline, thumbs, traces] = await Promise.all([
    api.timeline(id).catch(() => null),
    api.thumbs(id).catch(() => null),
    Promise.all(m.sources.map((s) => (s.trace ? api.trace(id, s.source_id).catch(() => null) : Promise.resolve(null)))),
  ]);
  const traceBy = new Map(m.sources.map((s, i) => [s.source_id, traces[i] ?? null]));

  const timelineCard = h("section", { class: "card", id: "timeline" }, h("h2", {}, "Timeline"));
  const scrubCard = h("section", { class: "card", id: "scrubber" }, h("h2", {}, "Scrubber"));
  root.replaceChildren(head, statsCard(m), timelineCard, scrubCard);

  if (!timeline || timeline.n_rows === 0) {
    timelineCard.append(na("Timeline not available"));
    scrubCard.append(na("Scrubber not available"));
    return () => {};
  }
  const view = new RecordingView(m, timeline, thumbs, traceBy, timelineCard, scrubCard);
  view.select(initialTick ?? 0, false);
  return () => view.destroy();
}

function backLink(): HTMLElement {
  return h("a", { href: "#/", class: "back" }, "← Recordings");
}

// --------------------------------------------------------------------------- stats card

function statsCard(m: Manifest): HTMLElement {
  const st = m.stats ?? {};
  const p99 = st.sync_jitter_p99_ns ?? null;
  const tiles: [string, HTMLElement | string][] = [
    ["Ticks", fmtInt(st.ticks ?? m.n_ticks)],
    ["Tick length", fmtMs(m.tick_ns)],
    ["Jitter p50", fmtMs(st.sync_jitter_p50_ns)],
    ["Jitter p99", p99 === null ? DASH : badge(fmtMs(p99), p99 < 1e6)],
    ["Staleness p50", fmtMs(st.staleness_p50_ns)],
    ["Staleness p99", fmtMs(st.staleness_p99_ns)],
  ];
  const per = st.sources ?? {};
  const rows = m.sources.map((s) => {
    const ss = per[s.source_id] ?? {};
    const rate = ss.samples_per_s ?? (m.duration_s > 0 ? s.n_samples / m.duration_s : null);
    const drops = ss.dropped;
    const late = ss.late;
    return h(
      "tr",
      {},
      h("th", { scope: "row" }, s.source_id),
      h("td", {}, h("span", { class: `mod mod-${s.modality}` }, s.modality)),
      h("td", { class: "num" }, fmtRate(rate)),
      h("td", { class: "num" }, s.modality === "event" ? fmtInt(s.n_events) : fmtInt(s.n_samples)),
      h("td", { class: "num" }, drops === undefined ? DASH : badge(fmtInt(drops), drops === 0)),
      h("td", { class: "num" }, late === undefined ? DASH : badge(fmtInt(late), late === 0)),
    );
  });
  return h(
    "section",
    { class: "card", id: "stats" },
    h("h2", {}, "Stats"),
    m.stats ? null : h("p", { class: "sub" }, "This tick file has no stats; values marked — are not available."),
    h("div", { class: "tiles" }, ...tiles.map(([k, v]) => h("div", { class: "tile" }, h("div", { class: "k" }, k), h("div", { class: "v" }, v)))),
    h(
      "div",
      { class: "table-scroll" },
      h(
        "table",
        { class: "src-table" },
        h(
          "thead",
          {},
          h(
            "tr",
            {},
            h("th", { scope: "col" }, "Source"),
            h("th", { scope: "col" }, "Modality"),
            ...["Rate", "Samples / events", "Drops", "Late"].map((c) => h("th", { scope: "col", class: "num" }, c)),
          ),
        ),
        h("tbody", {}, ...rows),
      ),
    ),
  );
}

// --------------------------------------------------------------------------- timeline + scrubber

interface Lane {
  src: SourceEntry;
  el: HTMLElement;
  plot: uPlot | null;
}

class RecordingView {
  private lanes: Lane[] = [];
  private tickS: number;
  private sel = 0;
  private range: HTMLInputElement;
  private readout: HTMLElement;
  private thumbsEl: HTMLElement;
  private countsBody: HTMLElement;
  private thumbSlots = new Map<string, { img: HTMLImageElement; cap: HTMLElement; entries: ThumbEntry[]; k: number }>();
  private ro: ResizeObserver;
  private syncing = false;
  private lanesEl: HTMLElement;

  constructor(
    private m: Manifest,
    private tl: Columnar,
    private thumbs: ThumbIndex | null,
    traces: Map<string, Columnar | null>,
    timelineCard: HTMLElement,
    scrubCard: HTMLElement,
  ) {
    this.tickS = m.tick_ns / 1e9;
    this.lanesEl = h("div", { class: "lanes" });
    timelineCard.append(
      h("p", { class: "hint" }, "Click a lane to move the scrubber. Drag to zoom, double-click to reset."),
      this.lanesEl,
    );
    const width = this.laneWidth();
    m.sources.forEach((src, i) => {
      const lane = this.buildLane(src, traces.get(src.source_id) ?? null, width, i === m.sources.length - 1);
      this.lanes.push(lane);
      this.lanesEl.append(lane.el);
    });

    // scrubber
    const n = tl.n_rows;
    this.range = h("input", { type: "range", min: 0, max: n - 1, step: 1, value: 0, "aria-label": "Tick", id: "scrub-range" });
    this.range.addEventListener("input", () => this.select(Number(this.range.value)));
    const step = (d: number) => () => this.select(this.sel + d);
    this.readout = h("output", { class: "readout", id: "scrub-readout" });
    this.thumbsEl = h("div", { class: "thumbs" });
    this.countsBody = h("tbody", {});
    scrubCard.append(
      h(
        "div",
        { class: "scrub-row" },
        h("button", { type: "button", class: "step", "aria-label": "Previous tick" }, "‹"),
        this.range,
        h("button", { type: "button", class: "step", "aria-label": "Next tick" }, "›"),
      ),
      this.readout,
      this.thumbsEl,
      h(
        "div",
        { class: "table-scroll" },
        h(
          "table",
          { class: "counts" },
          h(
            "thead",
            {},
            h("tr", {}, h("th", { scope: "col" }, "Source"), h("th", { scope: "col", class: "num" }, "Samples"), h("th", { scope: "col", class: "num" }, "Events")),
          ),
          this.countsBody,
        ),
      ),
    );
    const [prev, next] = scrubCard.querySelectorAll<HTMLButtonElement>("button.step");
    prev?.addEventListener("click", step(-1));
    next?.addEventListener("click", step(1));
    this.buildThumbs();

    this.ro = new ResizeObserver(() => {
      const w = this.laneWidth();
      for (const l of this.lanes) l.plot?.setSize({ width: w, height: l.plot.height });
    });
    this.ro.observe(this.lanesEl);
  }

  destroy(): void {
    this.ro.disconnect();
    for (const l of this.lanes) l.plot?.destroy();
  }

  private laneWidth(): number {
    return Math.max(240, Math.floor(this.lanesEl.clientWidth || this.lanesEl.parentElement?.clientWidth || 600));
  }

  // ---- lanes

  private buildLane(src: SourceEntry, trace: Columnar | null, width: number, last: boolean): Lane {
    const spec = this.laneSpec(src, trace);
    const legend = spec
      ? spec.series.map((s, i) => h("span", { class: "chip" }, h("i", { style: `background:${s.stroke as string}` }), spec.labels[i] ?? ""))
      : [];
    const el = h(
      "div",
      { class: "lane", "data-source": src.source_id },
      h(
        "div",
        { class: "lane-head" },
        h("strong", {}, src.source_id),
        h("span", { class: `mod mod-${src.modality}` }, src.modality),
        h("span", { class: "unit" }, spec?.unit ?? ""),
        h("span", { class: "legend" }, ...legend),
      ),
    );
    if (!spec) {
      el.append(na());
      return { src, el, plot: null };
    }
    const dur = this.m.duration_s;
    const opts: uPlot.Options = {
      width,
      height: spec.height + (last ? 28 : 0),
      legend: { show: false },
      padding: [6, 8, 0, 0],
      cursor: { sync: { key: "timeline" }, drag: { x: true, y: false, setScale: true }, points: { show: false } },
      scales: {
        x: { time: false, auto: false, range: [0, dur] },
        y: spec.yRange ? { range: spec.yRange } : { range: (_u, _min, max) => [Math.min(0, _min), max > 0 ? max * 1.05 : 1] },
        y2: { range: (_u, min, max) => uPlot.rangeNum(min, max, 0.1, true) },
      },
      axes: [
        xAxis(last),
        spec.yRange ? { ...axis(undefined, 3, 52), values: () => [] } : axis(undefined, 3, 52),
        spec.y2 ? axis(undefined, 1, 44) : spacerAxis(44),
      ],
      series: [{}, ...spec.series],
      hooks: {
        draw: [(u) => this.drawMarker(u)],
        setScale: [(u, key) => key === "x" && this.syncZoom(u)],
      },
    };
    const plot = new uPlot(opts, spec.data, el);
    this.bindClicks(plot);
    return { src, el, plot };
  }

  private laneSpec(
    src: SourceEntry,
    trace: Columnar | null,
  ): { data: uPlot.AlignedData; series: uPlot.Series[]; labels: string[]; unit: string; height: number; y2?: boolean; yRange?: [number, number] } | null {
    const sid = src.source_id;
    const tx = xnums(trace);
    const lx = xnums(this.tl);
    const color = PALETTE[0] as string;
    const valueCols = trace ? Object.keys(trace.columns).filter((c) => !["tick", "t_rel_s", "n_ticks"].includes(c)) : [];
    const lines = (cols: string[], unit: string, height = 72) => {
      if (!tx || cols.length === 0) return null;
      const series = cols.map((c, i) => (c === "mask" ? stepSeries(c, PALETTE[i % PALETTE.length] as string) : lineSeries(c, PALETTE[i % PALETTE.length] as string)));
      return { data: [tx, ...cols.map((c) => nums(trace, c) ?? [])] as uPlot.AlignedData, series, labels: cols, unit, height };
    };
    switch (src.modality) {
      case "event": {
        const y = nums(trace, "events_per_tick");
        if (tx && y) return { data: [tx, y], series: [barSeries("events / tick", color)], labels: [], unit: "events / tick", height: 72 };
        const ly = nums(this.tl, `events:${sid}`);
        if (lx && ly) return { data: [lx, ly], series: [barSeries("events / tick", color)], labels: [], unit: "events / tick", height: 72 };
        return null;
      }
      case "frame": {
        const counts = nums(this.tl, `samples:${sid}`);
        if (!lx || !counts) return null;
        const xs: number[] = [];
        counts.forEach((c, i) => {
          if (c && lx[i] !== undefined) xs.push(lx[i] as number);
        });
        return {
          data: [xs, xs.map(() => 1)],
          series: [dotSeries("frame", PALETTE[2] as string)],
          labels: [],
          unit: `${xs.length} frames`,
          height: 40,
          yRange: [0, 2],
        };
      }
      case "audio":
        return lines(valueCols, "RMS (full scale = 1)");
      case "imu": {
        if (!tx) return null;
        const acc = ["ax", "ay", "az"], gyr = ["gx", "gy", "gz"];
        const series = [
          ...acc.map((c, i) => lineSeries(c, PALETTE[i] as string)),
          ...gyr.map((c, i) => lineSeries(c, PALETTE[i + 3] as string, "y2")),
        ];
        return {
          data: [tx, ...[...acc, ...gyr].map((c) => nums(trace, c) ?? [])] as uPlot.AlignedData,
          series,
          labels: [...acc, ...gyr],
          unit: "m/s² (left) · rad/s (right)",
          height: 90,
          y2: true,
        };
      }
      case "current":
        return lines(valueCols, "A");
      case "contact":
        return lines(valueCols, valueCols[0] === "mask" ? "bumper mask" : "N");
      case "gas":
        return lines(valueCols, "");
      default: {
        const ly = nums(this.tl, `samples:${sid}`);
        if (!lx || !ly) return null;
        return { data: [lx, ly], series: [barSeries("samples / tick", PALETTE[5] as string)], labels: [], unit: "samples / tick", height: 56 };
      }
    }
  }

  private drawMarker(u: uPlot): void {
    const x = u.valToPos((this.sel + 1) * this.tickS, "x", true);
    const { top, height, left, width } = u.bbox;
    if (x < left || x > left + width) return;
    const ctx = u.ctx;
    ctx.save();
    ctx.strokeStyle = cssVar("--accent");
    ctx.lineWidth = 2 * devicePixelRatio;
    ctx.beginPath();
    ctx.moveTo(x, top);
    ctx.lineTo(x, top + height);
    ctx.stroke();
    ctx.restore();
  }

  private syncZoom(src: uPlot): void {
    if (this.syncing) return;
    this.syncing = true;
    const { min, max } = src.scales.x ?? {};
    for (const l of this.lanes) {
      if (l.plot && l.plot !== src && min !== undefined && max !== undefined) l.plot.setScale("x", { min, max });
    }
    this.syncing = false;
  }

  private bindClicks(u: uPlot): void {
    let downX = 0;
    u.over.addEventListener("mousedown", (e) => {
      downX = e.clientX;
    });
    u.over.addEventListener("click", (e) => {
      if (Math.abs(e.clientX - downX) > 4) return; // that was a drag-zoom
      const t = u.posToVal(e.offsetX, "x");
      this.select(Math.round(t / this.tickS) - 1);
    });
    u.over.addEventListener("dblclick", () => {
      for (const l of this.lanes) l.plot?.setScale("x", { min: 0, max: this.m.duration_s });
    });
  }

  // ---- scrubber

  private buildThumbs(): void {
    const visual = this.m.sources.filter((s) => s.modality === "frame" || s.modality === "event");
    if (visual.length === 0) {
      this.thumbsEl.append(na("No camera or event sources"));
      return;
    }
    for (const s of visual) {
      const entries = this.thumbs?.[s.source_id] ?? [];
      const cap = h("figcaption", {});
      const fig = h("figure", { class: "thumb", "data-source": s.source_id }, h("div", { class: "thumb-title" }, s.source_id));
      if (entries.length === 0) {
        fig.append(na());
        this.thumbsEl.append(fig);
        continue;
      }
      const img = h("img", { alt: `${s.source_id} near the selected tick`, decoding: "async" });
      img.addEventListener("error", () => img.replaceWith(na()), { once: true });
      fig.append(h("div", { class: "frame" }, img), cap);
      this.thumbsEl.append(fig);
      this.thumbSlots.set(s.source_id, { img, cap, entries, k: -1 });
    }
  }

  select(tick: number, updateHash = true): void {
    const n = this.tl.n_rows;
    const k = Math.min(n - 1, Math.max(0, Math.round(Number.isFinite(tick) ? tick : 0)));
    this.sel = k;
    this.range.value = String(k);
    const t = (k + 1) * this.tickS;
    this.readout.textContent = `tick ${k.toLocaleString("en-US")} of ${n.toLocaleString("en-US")} · t = ${t.toFixed(3)} s`;
    for (const l of this.lanes) l.plot?.redraw(false, false);

    for (const [sid, slot] of this.thumbSlots) {
      const e = nearest(slot.entries, k);
      if (e.k !== slot.k) {
        slot.k = e.k;
        slot.img.src = api.thumbUrl(this.m.recording_id, sid, e.k);
        slot.img.dataset.k = String(e.k);
      }
      const d = e.tick - k;
      slot.cap.textContent = `tick ${e.tick}${d === 0 ? " (exact)" : ` (${d > 0 ? "+" : "−"}${Math.abs(d)} ticks, ${(Math.abs(d) * this.tickS * 1e3).toFixed(0)} ms)`}`;
    }

    this.countsBody.replaceChildren(
      ...this.m.sources.map((s) => {
        const sc = this.tl.columns[`samples:${s.source_id}`]?.[k];
        const ec = this.tl.columns[`events:${s.source_id}`]?.[k];
        return h(
          "tr",
          { "data-source": s.source_id },
          h("th", { scope: "row" }, s.source_id),
          h("td", { class: "num" }, typeof sc === "number" ? fmtInt(sc) : DASH),
          h("td", { class: "num" }, typeof ec === "number" ? fmtInt(ec) : DASH),
        );
      }),
    );
    if (updateHash) {
      history.replaceState(null, "", `#/r/${encodeURIComponent(this.m.recording_id)}?t=${k}`);
    }
  }
}

/** Entry whose tick is closest to `tick` (entries sorted by tick, non-empty). */
export function nearest(entries: ThumbEntry[], tick: number): ThumbEntry {
  let lo = 0;
  let hi = entries.length - 1;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if ((entries[mid] as ThumbEntry).tick < tick) lo = mid + 1;
    else hi = mid;
  }
  const a = entries[lo] as ThumbEntry;
  const b = entries[lo - 1];
  return b && Math.abs(b.tick - tick) <= Math.abs(a.tick - tick) ? b : a;
}
