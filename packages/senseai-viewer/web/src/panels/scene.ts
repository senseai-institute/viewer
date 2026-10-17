// Copyright 2026 SenseAI Institute
// SPDX-License-Identifier: Apache-2.0
//
// The scene: a camera view (frame underlay, event points, surprise heat projected onto the
// floor, path cells, tracks) and a top-down body-frame map (32×32 grid over ±5 m, bearing
// ring, path cells, robot, tracks, ground truth). Canvas 2D; cheap enough for 60 fps.

import type { RunIndex, Window } from "../api/client.ts";
import { css, heat } from "../colors.ts";
import type { LayerId } from "../state/player.ts";
import type { Sample } from "../sample.ts";

export interface SceneInput {
  t: number;
  win: Window | null;
  sample: Sample | null;
  layers: Set<LayerId>;
  colorblind: boolean;
  pathCells: number[] | null; // cells of the fire in force
  diff?: Float32Array | null; // other side's grid: outline cells lit here but not there
  profileLabel: string;
}

const EVENT_TRAIL_S = 0.03;
const FRAME_GAIN = 3;
const LIT = 0.05;
const SIMPLE_MIN = 0.12; // simple view: draw only strong signals, not background specks
const BEARING_FULL = 1.5;
const TEAL = "#5EEAD4";
const WARN = "#F2B35C";
const ALERT = "#FF5A7A";

function el<K extends keyof HTMLElementTagNameMap>(tag: K, cls?: string, text?: string): HTMLElementTagNameMap[K] {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text) e.textContent = text;
  return e;
}

/** Index of the first value >= x in a sorted Float32Array. */
function lowerBound(a: Float32Array, x: number): number {
  let lo = 0;
  let hi = a.length;
  while (lo < hi) {
    const m = (lo + hi) >> 1;
    if (a[m] < x) lo = m + 1;
    else hi = m;
  }
  return lo;
}

export class Scene {
  readonly root: HTMLElement;
  private cam: HTMLCanvasElement;
  private map: HTMLCanvasElement;
  private camCtx: CanvasRenderingContext2D;
  private mapCtx: CanvasRenderingContext2D;
  private evBuf: HTMLCanvasElement;
  private frBuf: HTMLCanvasElement;
  private tooltip: HTMLElement;
  private hud: HTMLElement;
  private last: SceneInput | null = null;
  private hover: { x: number; y: number } | null = null;
  private index: RunIndex;
  private camW: number;
  private camH: number;
  private mapPx = 560;
  private simple = false;

  constructor(index: RunIndex, opts: { compact?: boolean; stacked?: boolean; simple?: boolean } = {}) {
    this.index = index;
    this.camW = index.camera?.width || 346;
    this.camH = index.camera?.height || 260;
    this.root = el("div");
    const scene = el("div", "scene");
    const camView = el("div", "view");
    const mapView = el("div", "view");
    this.cam = el("canvas");
    this.map = el("canvas");
    const S = 2;
    this.cam.width = this.camW * S;
    this.cam.height = this.camH * S;
    this.map.width = this.mapPx;
    this.map.height = this.mapPx;
    this.cam.setAttribute("role", "img");
    this.cam.setAttribute("aria-label", "Camera view: night frame, event points and projected surprise");
    this.map.setAttribute("role", "img");
    this.map.setAttribute("aria-label", "Top-down map: surprise grid around the robot, bearing ring, tracks");
    camView.append(this.cam, el("span", "view-label", "camera · events · heat"));
    mapView.append(this.map, el("span", "view-label", "body frame · ±5 m"));
    this.tooltip = el("div", "tooltip");
    mapView.append(this.tooltip);
    scene.append(camView, mapView);
    this.simple = !!opts.simple;
    if (opts.simple) {
      mapView.remove();
      scene.style.gridTemplateColumns = "minmax(0,1fr)";
      camView.querySelector(".view-label")?.remove();
    }
    if (opts.compact) scene.style.gridTemplateColumns = "minmax(0,1fr) minmax(0,0.75fr)";
    if (opts.stacked) {
      scene.style.gridTemplateColumns = "minmax(0,1fr)";
      mapView.style.width = "72%";
      mapView.style.justifySelf = "center";
    }
    this.hud = el("div", "hud");
    this.root.append(scene, this.hud);
    if (opts.simple) this.hud.style.display = "none";
    this.camCtx = this.cam.getContext("2d")!;
    this.mapCtx = this.map.getContext("2d")!;
    this.evBuf = document.createElement("canvas");
    this.evBuf.width = this.camW;
    this.evBuf.height = this.camH;
    this.frBuf = document.createElement("canvas");
    this.map.addEventListener("mousemove", (e) => {
      const r = this.map.getBoundingClientRect();
      this.hover = { x: ((e.clientX - r.left) / r.width) * this.mapPx, y: ((e.clientY - r.top) / r.height) * this.mapPx };
      this.drawTooltip(e.clientX - r.left, e.clientY - r.top);
    });
    this.map.addEventListener("mouseleave", () => {
      this.hover = null;
      this.tooltip.style.display = "none";
    });
  }

  render(inp: SceneInput): void {
    this.last = inp;
    this.drawCamera(inp);
    if (!this.simple) {
      this.drawMap(inp);
      this.drawHud(inp);
    }
    if (this.hover) this.drawTooltip(null, null);
  }

  // ------------------------------------------------------------------ camera

  private project(x: number, y: number, z = 0): [number, number] | null {
    const c = this.index.camera;
    if (!c || x < 0.25) return null;
    const S = this.cam.width / this.camW;
    return [(c.cx - (c.fx * y) / x) * S, (c.cy + (c.fx * (c.mount_height_m - z)) / x) * S];
  }

  private drawCamera(inp: SceneInput): void {
    const ctx = this.camCtx;
    const W = this.cam.width;
    const H = this.cam.height;
    ctx.fillStyle = "#04060b";
    ctx.fillRect(0, 0, W, H);
    const win = inp.win;
    const s = inp.sample;
    if (!win) {
      this.loading(ctx, W, H);
      return;
    }
    const rec = win.recording;
    // Frame underlay.
    if (inp.layers.has("frames") && rec.frames && rec.frames.data.length) {
      const fr = rec.frames;
      let j = 0;
      for (let i = 0; i < fr.t_s.data.length; i++) if (fr.t_s.data[i] <= inp.t + 1e-6) j = i;
      const img = fr.data[j];
      if (this.frBuf.width !== fr.width || this.frBuf.height !== fr.height) {
        this.frBuf.width = fr.width;
        this.frBuf.height = fr.height;
      }
      const fctx = this.frBuf.getContext("2d")!;
      const id = fctx.createImageData(fr.width, fr.height);
      for (let i = 0, o = 0; i < img.length; i++, o += 4) {
        const v = Math.min(255, img[i] * FRAME_GAIN);
        id.data[o] = v;
        id.data[o + 1] = v;
        id.data[o + 2] = v + 4;
        id.data[o + 3] = 255;
      }
      fctx.putImageData(id, 0, 0);
      ctx.imageSmoothingEnabled = true;
      ctx.drawImage(this.frBuf, 0, 0, W, H);
    }
    // Surprise projected onto the floor.
    if (inp.layers.has("grid") && s?.grid && this.index.camera) {
      const G = s.gridCells;
      const E = s.extent;
      const c = (2 * E) / G;
      for (let ix = 0; ix < G; ix++) {
        const x0 = -E + ix * c;
        if (x0 < 0.3) continue;
        for (let iy = 0; iy < G; iy++) {
          const v = s.grid[ix * G + iy];
          if (this.simple && v < SIMPLE_MIN) continue;
          const col = heat(v, inp.colorblind);
          if (col[3] === 0) continue;
          const y0 = -E + iy * c;
          const pts = [this.project(x0, y0), this.project(x0 + c, y0), this.project(x0 + c, y0 + c), this.project(x0, y0 + c)];
          if (pts.some((p) => p === null)) continue;
          ctx.fillStyle = css([col[0], col[1], col[2], col[3] * 0.85]);
          ctx.beginPath();
          pts.forEach((p, k) => (k ? ctx.lineTo(p![0], p![1]) : ctx.moveTo(p![0], p![1])));
          ctx.closePath();
          ctx.fill();
        }
      }
    }
    if (inp.layers.has("path") && inp.pathCells && s && this.index.camera) {
      ctx.strokeStyle = WARN;
      ctx.lineWidth = 2;
      const G = s.gridCells;
      const E = s.extent;
      const c = (2 * E) / G;
      for (const cell of inp.pathCells) {
        const ix = Math.floor(cell / G);
        const iy = cell % G;
        const x0 = -E + ix * c;
        const y0 = -E + iy * c;
        const pts = [this.project(x0, y0), this.project(x0 + c, y0), this.project(x0 + c, y0 + c), this.project(x0, y0 + c)];
        if (pts.some((p) => p === null)) continue;
        ctx.beginPath();
        pts.forEach((p, k) => (k ? ctx.lineTo(p![0], p![1]) : ctx.moveTo(p![0], p![1])));
        ctx.closePath();
        ctx.stroke();
      }
    }
    // Event points: a 30 ms trail, ON teal, OFF slate.
    if (inp.layers.has("events") && rec.events) {
      const ev = rec.events;
      const tr = inp.t - win.t0_s;
      const a = lowerBound(ev.t_s.data, tr - EVENT_TRAIL_S);
      const b = lowerBound(ev.t_s.data, tr);
      const ectx = this.evBuf.getContext("2d")!;
      const id = ectx.createImageData(this.camW, this.camH);
      const d = id.data;
      for (let i = a; i < b; i++) {
        const x = ev.x.data[i];
        const y = ev.y.data[i];
        if (x >= this.camW || y >= this.camH) continue;
        const o = (y * this.camW + x) * 4;
        if (ev.p.data[i] > 0) {
          d[o] = 0x5e;
          d[o + 1] = 0xea;
          d[o + 2] = 0xd4;
        } else {
          d[o] = 0xa9;
          d[o + 1] = 0xb4;
          d[o + 2] = 0xca;
        }
        d[o + 3] = 255;
      }
      ectx.putImageData(id, 0, 0);
      ctx.imageSmoothingEnabled = false;
      ctx.drawImage(this.evBuf, 0, 0, W, H);
    }
    // Ground truth and tracks as boxes.
    if (s && inp.layers.has("gt")) {
      for (const a of s.actors) this.personBox(ctx, a.x, a.y, ALERT, "ground truth", true);
    }
    if (s && inp.layers.has("tracks")) {
      for (const tr of s.tracks) this.personBox(ctx, tr.x, tr.y, TEAL, this.simple ? tr.label : `${tr.label} ${tr.confidence.toFixed(2)}`, false);
    }
    if (s?.warming) this.banner(ctx, "world model warming up");
  }

  private personBox(ctx: CanvasRenderingContext2D, x: number, y: number, color: string, label: string, dashed: boolean): void {
    const foot = this.project(x, y, 0);
    const head = this.project(x, y, 1.8);
    const side = this.project(x, y + 0.3, 0);
    if (!foot || !head || !side) return;
    const hw = Math.abs(side[0] - foot[0]);
    ctx.save();
    ctx.strokeStyle = color;
    ctx.lineWidth = 2;
    if (dashed) ctx.setLineDash([6, 5]);
    ctx.strokeRect(foot[0] - hw, head[1], 2 * hw, foot[1] - head[1]);
    ctx.setLineDash([]);
    ctx.fillStyle = color;
    ctx.font = "500 20px 'IBM Plex Mono', monospace";
    ctx.fillText(label, foot[0] + hw + 6, dashed ? foot[1] - 6 : head[1] + 18);
    ctx.restore();
  }

  private banner(ctx: CanvasRenderingContext2D, text: string): void {
    ctx.save();
    ctx.font = "500 20px 'IBM Plex Mono', monospace";
    ctx.fillStyle = WARN;
    ctx.fillText(text, 20, this.cam.height - 20);
    ctx.restore();
  }

  private loading(ctx: CanvasRenderingContext2D, W: number, H: number): void {
    ctx.fillStyle = "#9AA4B8";
    ctx.font = "20px 'IBM Plex Sans', sans-serif";
    ctx.textAlign = "center";
    ctx.fillText("loading…", W / 2, H / 2);
    ctx.textAlign = "start";
  }

  // ------------------------------------------------------------------ map

  private scale(): number {
    return this.mapPx / (2 * 5.9);
  }

  /** Body frame (x forward, y left) → map pixels; forward is up. */
  private toMap(x: number, y: number): [number, number] {
    const k = this.scale();
    return [this.mapPx / 2 - y * k, this.mapPx / 2 - x * k];
  }

  private drawMap(inp: SceneInput): void {
    const ctx = this.mapCtx;
    const P = this.mapPx;
    const k = this.scale();
    ctx.fillStyle = "#070b13";
    ctx.fillRect(0, 0, P, P);
    const s = inp.sample;
    const E = s?.extent || this.index.pipelines[0]?.grid_half_extent_m || 5;
    // Range rings and axes.
    ctx.strokeStyle = "#1a2336";
    ctx.lineWidth = 1;
    for (let r = 1; r <= Math.floor(E); r++) {
      ctx.beginPath();
      ctx.arc(P / 2, P / 2, r * k, 0, 2 * Math.PI);
      ctx.stroke();
    }
    ctx.beginPath();
    ctx.moveTo(P / 2, P / 2 - E * k);
    ctx.lineTo(P / 2, P / 2 + E * k);
    ctx.moveTo(P / 2 - E * k, P / 2);
    ctx.lineTo(P / 2 + E * k, P / 2);
    ctx.stroke();
    ctx.fillStyle = "#4d5874";
    ctx.font = "12px 'IBM Plex Mono', monospace";
    for (let r = 1; r <= Math.floor(E); r++) ctx.fillText(`${r} m`, P / 2 + 4, P / 2 - r * k - 3);
    ctx.strokeStyle = "#243049";
    const [gx0, gy0] = this.toMap(E, E);
    ctx.strokeRect(gx0, gy0, 2 * E * k, 2 * E * k);
    if (!inp.win) {
      this.loading(ctx, P, P);
      return;
    }
    if (s?.grid) {
      const G = s.gridCells;
      const c = (2 * E) / G;
      const px = c * k;
      if (inp.layers.has("grid")) {
        for (let ix = 0; ix < G; ix++) {
          for (let iy = 0; iy < G; iy++) {
            const col = heat(s.grid[ix * G + iy], inp.colorblind);
            if (col[3] === 0) continue;
            const [sx, sy] = this.toMap(-E + (ix + 1) * c, -E + (iy + 1) * c);
            ctx.fillStyle = css(col);
            ctx.fillRect(sx + 0.5, sy + 0.5, px - 1, px - 1);
          }
        }
      }
      if (inp.diff) {
        ctx.strokeStyle = "#ffffff";
        ctx.lineWidth = 1.5;
        for (let i = 0; i < G * G; i++) {
          if (!(s.grid[i] > LIT && !(inp.diff[i] > LIT))) continue;
          const ix = Math.floor(i / G);
          const iy = i % G;
          const [sx, sy] = this.toMap(-E + (ix + 1) * c, -E + (iy + 1) * c);
          ctx.strokeRect(sx + 1.5, sy + 1.5, px - 3, px - 3);
        }
      }
      if (inp.layers.has("path") && inp.pathCells) {
        ctx.strokeStyle = WARN;
        ctx.lineWidth = 2;
        for (const cell of inp.pathCells) {
          const ix = Math.floor(cell / G);
          const iy = cell % G;
          const [sx, sy] = this.toMap(-E + (ix + 1) * c, -E + (iy + 1) * c);
          ctx.strokeRect(sx + 1, sy + 1, px - 2, px - 2);
        }
      }
    }
    // Bearing ring: bin b centred on b·360/B degrees CCW from +x (straight ahead = up).
    if (inp.layers.has("bearing") && s?.bearing) {
      const B = s.bearing.length;
      const r0 = (E + 0.25) * k;
      ctx.strokeStyle = "#1f2a40";
      ctx.lineWidth = 6;
      ctx.beginPath();
      ctx.arc(P / 2, P / 2, r0, 0, 2 * Math.PI);
      ctx.stroke();
      for (let b = 0; b < B; b++) {
        const v = s.bearing[b];
        const f = Math.min(1, v / BEARING_FULL);
        if (f < 0.12) continue;
        const ang = (b * 2 * Math.PI) / B;
        const half = Math.PI / B;
        const col = heat(0.05 + f * 0.6, inp.colorblind);
        ctx.strokeStyle = css([col[0], col[1], col[2], 0.95]);
        ctx.lineWidth = 6 + f * 18;
        ctx.beginPath();
        // canvas angle: forward (up) is −π/2; CCW in the body frame is CCW on screen.
        ctx.arc(P / 2, P / 2, r0 + (f * 18) / 2, -Math.PI / 2 - ang - half, -Math.PI / 2 - ang + half);
        ctx.stroke();
      }
    }
    // Robot footprint.
    ctx.fillStyle = "#e6eaf2";
    ctx.strokeStyle = "#e6eaf2";
    const [rx, ry] = this.toMap(0.35, 0.3);
    ctx.globalAlpha = 0.9;
    ctx.strokeRect(rx, ry, 0.6 * k, 0.7 * k);
    ctx.beginPath();
    const [tx, ty] = this.toMap(0.55, 0);
    ctx.moveTo(tx, ty - 6);
    ctx.lineTo(tx - 6, ty + 4);
    ctx.lineTo(tx + 6, ty + 4);
    ctx.fill();
    ctx.globalAlpha = 1;
    if (s && inp.layers.has("gt")) {
      ctx.setLineDash([5, 4]);
      ctx.strokeStyle = ALERT;
      ctx.lineWidth = 2;
      for (const a of s.actors) {
        const [ax, ay] = this.toMap(a.x, a.y);
        ctx.beginPath();
        ctx.arc(ax, ay, 0.25 * k, 0, 2 * Math.PI);
        ctx.stroke();
      }
      ctx.setLineDash([]);
    }
    if (s && inp.layers.has("tracks")) {
      for (const tr of s.tracks) {
        const [ax, ay] = this.toMap(tr.x, tr.y);
        const [bx, by] = this.toMap(tr.x + tr.vx * 0.5, tr.y + tr.vy * 0.5);
        ctx.strokeStyle = TEAL;
        ctx.lineWidth = 2;
        ctx.beginPath();
        ctx.moveTo(ax, ay);
        ctx.lineTo(bx, by);
        ctx.stroke();
        ctx.fillStyle = TEAL;
        ctx.beginPath();
        ctx.arc(ax, ay, 6, 0, 2 * Math.PI);
        ctx.fill();
        ctx.font = "500 13px 'IBM Plex Mono', monospace";
        ctx.fillText(`${tr.label} #${tr.id}`, ax + 10, ay - 8);
      }
    }
  }

  private drawTooltip(px: number | null, py: number | null): void {
    const inp = this.last;
    const s = inp?.sample;
    if (!this.hover || !s?.grid) {
      this.tooltip.style.display = "none";
      return;
    }
    const k = this.scale();
    const x = (this.mapPx / 2 - this.hover.y) / k;
    const y = (this.mapPx / 2 - this.hover.x) / k;
    const E = s.extent;
    const G = s.gridCells;
    const c = (2 * E) / G;
    const ix = Math.floor((x + E) / c);
    const iy = Math.floor((y + E) / c);
    if (ix < 0 || iy < 0 || ix >= G || iy >= G) {
      this.tooltip.style.display = "none";
      return;
    }
    const v = s.grid[ix * G + iy];
    const heads = Object.entries(s.heads)
      .map(([h, val]) => `${h} ${val.toFixed(1)}`)
      .join(" · ");
    this.tooltip.innerHTML = "";
    const lines = [
      `cell [${ix}, ${iy}]  x ${(-E + (ix + 0.5) * c).toFixed(1)} m, y ${(-E + (iy + 0.5) * c).toFixed(1)} m`,
      `surprise ${v.toFixed(3)}${v > LIT ? "  (lit)" : ""}`,
      `heads this tick: ${heads || "—"}`,
    ];
    for (const l of lines) this.tooltip.append(el("div", undefined, l));
    this.tooltip.style.display = "block";
    if (px !== null && py !== null) {
      this.tooltip.style.left = `${Math.min(px + 14, (this.map.clientWidth || 300) - 260)}px`;
      this.tooltip.style.top = `${py + 14}px`;
    }
  }

  private drawHud(inp: SceneInput): void {
    const s = inp.sample;
    const item = (k: string, v: string, cls = "") => {
      const span = el("span");
      span.append(`${k} `);
      const b = el("b", cls, v);
      span.append(b);
      return span;
    };
    const nodes = [
      item("t", `${inp.t.toFixed(2)} s`),
      item("profile", inp.profileLabel),
      item("tokens/tick", s?.tokens != null ? String(s.tokens) : "—"),
      item("lux", s?.lux != null ? s.lux.toFixed(1) : "—"),
      item("reflex", s ? `${s.action}${s.speedScale != null && s.action !== "none" ? ` ${Math.round(s.speedScale * 100)}%` : ""}` : "—", `action-${s?.action ?? "none"}`),
      item("e2e", s?.latencyMs != null ? `${s.latencyMs.toFixed(1)} ms` : "—"),
    ];
    if (inp.win?.recording.events) {
      const e = inp.win.recording.events;
      if (e.kept < e.total) nodes.push(item("events", `${Math.round((100 * e.kept) / e.total)}% shown`));
    }
    this.hud.replaceChildren(...nodes);
  }
}
