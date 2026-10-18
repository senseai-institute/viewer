// Copyright 2026 SenseAI Institute
// SPDX-License-Identifier: Apache-2.0
//
// Timeline: path risk (or global surprise) vs time for one or two pipelines, the reflex
// threshold, ground truth, clean segments and fires. Click or drag to scrub; click a fire
// marker to open its explanation. Drawn from the run index (never from windows).

import type { Fire, RunIndex } from "../api/client.ts";

export interface Series {
  pipeline: string;
  label: string;
  color: string;
  offset_s?: number; // shift applied to this series' times (compare across runs)
  index?: RunIndex; // defaults to the timeline's index
}

const M = { l: 46, r: 14, t: 18, b: 24 };

export class Timeline {
  readonly root: HTMLElement;
  private canvas: HTMLCanvasElement;
  private ctx: CanvasRenderingContext2D;
  private t = 0;
  private hoverX: number | null = null;
  private mode: "risk" | "global" = "risk";
  private dragging = false;
  private markers: { x: number; fire: Fire }[] = [];
  private duration: number;
  private readonly index: RunIndex;
  private series: Series[];
  onSeek: (t: number) => void = () => {};
  onFire: (f: Fire) => void = () => {};

  constructor(index: RunIndex, series: Series[]) {
    this.index = index;
    this.series = series;
    this.duration = index.duration_s;
    this.root = document.createElement("section");
    this.root.className = "panel timeline";
    const title = document.createElement("h2");
    title.className = "panel-title";
    title.append("Timeline");
    const legend = document.createElement("span");
    legend.className = "legend tl-legend";
    legend.style.margin = "0";
    for (const s of series) {
      const it = document.createElement("span");
      const sw = document.createElement("i");
      sw.style.background = s.color;
      it.append(sw, s.label);
      legend.append(it);
    }
    const thr = document.createElement("span");
    thr.innerHTML = `<i style="background:#F2B35C"></i>reflex threshold`;
    const gt = document.createElement("span");
    gt.innerHTML = `<i style="background:#FF5A7A"></i>in path (ground truth)`;
    legend.append(thr, gt);
    const spacer = document.createElement("span");
    spacer.className = "spacer";
    const seg = document.createElement("span");
    seg.className = "seg";
    for (const [m, lbl] of [["risk", "path risk"], ["global", "global surprise"]] as const) {
      const b = document.createElement("button");
      b.textContent = lbl;
      b.setAttribute("aria-pressed", String(this.mode === m));
      b.addEventListener("click", () => {
        this.mode = m;
        seg.querySelectorAll("button").forEach((x) => x.setAttribute("aria-pressed", String(x === b)));
        this.draw();
      });
      seg.append(b);
    }
    title.append(legend, spacer, seg);
    this.canvas = document.createElement("canvas");
    this.canvas.tabIndex = 0;
    this.canvas.setAttribute("role", "slider");
    this.canvas.setAttribute("aria-label", "Playback position");
    this.canvas.setAttribute("aria-valuemin", "0");
    this.canvas.setAttribute("aria-valuemax", this.duration.toFixed(2));
    this.root.append(title, this.canvas);
    this.ctx = this.canvas.getContext("2d")!;
    const pos = (e: PointerEvent) => {
      const r = this.canvas.getBoundingClientRect();
      return e.clientX - r.left;
    };
    this.canvas.addEventListener("pointerdown", (e) => {
      const x = pos(e);
      const hit = this.markers.find((m) => Math.abs(m.x - x) < 8 && e.offsetY < M.t + 16);
      if (hit) {
        this.onFire(hit.fire);
        return;
      }
      this.dragging = true;
      this.canvas.setPointerCapture(e.pointerId);
      this.onSeek(this.xToT(x));
    });
    this.canvas.addEventListener("pointermove", (e) => {
      this.hoverX = pos(e);
      if (this.dragging) this.onSeek(this.xToT(this.hoverX));
      else this.draw();
    });
    this.canvas.addEventListener("pointerup", () => (this.dragging = false));
    this.canvas.addEventListener("pointerleave", () => {
      this.hoverX = null;
      this.draw();
    });
    new ResizeObserver(() => this.draw()).observe(this.canvas);
  }

  setT(t: number): void {
    this.t = t;
    this.canvas.setAttribute("aria-valuenow", t.toFixed(2));
    this.canvas.setAttribute("aria-valuetext", `${t.toFixed(2)} seconds`);
    this.draw();
  }

  private width(): number {
    return this.canvas.clientWidth || 800;
  }

  private xToT(x: number): number {
    const w = this.width() - M.l - M.r;
    return Math.min(this.duration, Math.max(0, ((x - M.l) / w) * this.duration));
  }

  private tToX(t: number): number {
    return M.l + (t / this.duration) * (this.width() - M.l - M.r);
  }

  draw(): void {
    const dpr = window.devicePixelRatio || 1;
    const W = this.width();
    const H = this.canvas.clientHeight || 150;
    if (this.canvas.width !== Math.round(W * dpr) || this.canvas.height !== Math.round(H * dpr)) {
      this.canvas.width = Math.round(W * dpr);
      this.canvas.height = Math.round(H * dpr);
    }
    const ctx = this.ctx;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, W, H);
    const ph = H - M.t - M.b;
    const key = this.mode === "risk" ? "risk_max" : "global_max";
    let ymax = 0;
    let thr: number | null = null;
    for (const s of this.series) {
      const idx = s.index ?? this.index;
      for (const v of idx.timeline[key][s.pipeline] ?? []) ymax = Math.max(ymax, v);
      const th = idx.timeline.threshold[s.pipeline];
      // One threshold line: the first series' (a detector's score threshold is on another scale).
      const det = idx.pipelines.find((p) => p.name === s.pipeline)?.kind === "detector";
      if (this.mode === "risk" && th != null && thr === null && !det) thr = th;
    }
    if (thr !== null) ymax = Math.max(ymax, thr * 1.6);
    ymax = ymax > 0 ? ymax * 1.08 : 1;
    const yToPx = (v: number) => M.t + ph - (v / ymax) * ph;
    const gt = this.index.ground_truth;
    // Clean segments.
    for (const [a, b] of gt.clean_segments_s ?? []) {
      ctx.fillStyle = "rgba(94,234,212,0.05)";
      ctx.fillRect(this.tToX(a), M.t, this.tToX(b) - this.tToX(a), ph);
      ctx.fillStyle = "#6b7690";
      ctx.font = "11px 'IBM Plex Mono', monospace";
      ctx.fillText("clean segment: every fire here is false", this.tToX(a) + 6, M.t + 12);
    }
    // Axes.
    ctx.strokeStyle = "#243049";
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(M.l, M.t + ph + 0.5);
    ctx.lineTo(W - M.r, M.t + ph + 0.5);
    ctx.stroke();
    ctx.fillStyle = "#6b7690";
    ctx.font = "11px 'IBM Plex Mono', monospace";
    const pxPerS = (W - M.l - M.r) / this.duration;
    const step = [1, 2, 5, 10, 30, 60, 120, 300].find((s) => s * pxPerS >= 44) ?? 600;
    for (let s = 0; s <= this.duration + 1e-6; s += step) {
      const x = this.tToX(s);
      ctx.fillRect(x, M.t + ph, 1, 4);
      ctx.fillText(`${s}s`, x - 6, H - 6);
    }
    ctx.fillText(ymax.toFixed(1), 6, M.t + 8);
    ctx.fillText("0", 6, M.t + ph);
    // Threshold.
    if (thr !== null) {
      ctx.strokeStyle = "#F2B35C";
      ctx.setLineDash([6, 5]);
      ctx.beginPath();
      ctx.moveTo(M.l, yToPx(thr));
      ctx.lineTo(W - M.r, yToPx(thr));
      ctx.stroke();
      ctx.setLineDash([]);
      ctx.fillStyle = "#F2B35C";
      ctx.fillText(`threshold ${thr.toFixed(1)}`, W - M.r - 100, yToPx(thr) - 5);
    }
    // Ground truth.
    if (gt.t_visible_s !== undefined) {
      const x = this.tToX(gt.t_visible_s);
      ctx.strokeStyle = "rgba(255,90,122,0.45)";
      ctx.setLineDash([2, 4]);
      ctx.beginPath();
      ctx.moveTo(x, M.t);
      ctx.lineTo(x, M.t + ph);
      ctx.stroke();
      ctx.setLineDash([]);
    }
    if (gt.t_gt_s !== undefined) {
      const x = this.tToX(gt.t_gt_s);
      ctx.strokeStyle = "#FF5A7A";
      ctx.lineWidth = 2;
      ctx.beginPath();
      ctx.moveTo(x, M.t);
      ctx.lineTo(x, M.t + ph);
      ctx.stroke();
      ctx.lineWidth = 1;
      ctx.fillStyle = "#FF5A7A";
      ctx.fillText(`in path ${gt.t_gt_s.toFixed(2)}s`, x + 5, M.t + 26);
    }
    // Series.
    for (const s of this.series) {
      const idx = s.index ?? this.index;
      const ys = idx.timeline[key][s.pipeline];
      if (!ys) continue;
      const off = s.offset_s ?? 0;
      ctx.strokeStyle = s.color;
      ctx.lineWidth = 1.75;
      ctx.beginPath();
      ys.forEach((v, i) => {
        const x = this.tToX(idx.timeline.t_s[i] - off);
        const y = yToPx(v);
        if (i) ctx.lineTo(x, y);
        else ctx.moveTo(x, y);
      });
      ctx.stroke();
    }
    // Fires.
    this.markers = [];
    for (const s of this.series) {
      const idx = s.index ?? this.index;
      for (const f of idx.fires) {
        if (f.pipeline !== s.pipeline) continue;
        const x = this.tToX(f.t_s - (s.offset_s ?? 0));
        this.markers.push({ x, fire: f });
        ctx.fillStyle = s.color;
        ctx.beginPath();
        ctx.moveTo(x - 6, M.t - 10);
        ctx.lineTo(x + 6, M.t - 10);
        ctx.lineTo(x, M.t + 1);
        ctx.closePath();
        ctx.fill();
        ctx.strokeStyle = s.color;
        ctx.globalAlpha = 0.35;
        ctx.beginPath();
        ctx.moveTo(x, M.t);
        ctx.lineTo(x, M.t + ph);
        ctx.stroke();
        ctx.globalAlpha = 1;
      }
    }
    // Hover guide.
    if (this.hoverX !== null && this.hoverX > M.l) {
      ctx.strokeStyle = "rgba(230,234,242,0.25)";
      ctx.beginPath();
      ctx.moveTo(this.hoverX, M.t);
      ctx.lineTo(this.hoverX, M.t + ph);
      ctx.stroke();
      ctx.fillStyle = "#9AA4B8";
      ctx.fillText(`${this.xToT(this.hoverX).toFixed(2)}s`, this.hoverX + 4, M.t + ph - 18);
    }
    // Playhead.
    const px = this.tToX(this.t);
    ctx.fillStyle = "#E6EAF2";
    ctx.fillRect(px - 1, M.t - 4, 2, ph + 8);
  }
}
