// Copyright 2026 SenseAI Institute
// SPDX-License-Identifier: Apache-2.0
//
// What is true at time t: pull the per-tick values out of a loaded window.

import type { CortexEvent, Fire, RunIndex, Window } from "./api/client.ts";

export interface Track {
  id: number;
  label: string;
  x: number;
  y: number;
  vx: number;
  vy: number;
  confidence: number;
  text: string;
}

export interface Sample {
  t: number;
  tick: number;
  global: number | null;
  heads: Record<string, number>;
  action: string;
  speedScale: number | null;
  risk: number | null;
  threshold: number | null;
  tokens: number | null;
  warming: boolean;
  lux: number | null;
  grid: Float32Array | null;
  gridCells: number;
  extent: number;
  bearing: Float32Array | null;
  tracks: Track[];
  actors: { x: number; y: number }[];
  latencyMs: number | null;
}

/** Tick whose window has ended by t (ticks are stamped with their end). */
export function tickAt(t: number, tickS: number, n: number): number {
  return Math.min(n - 1, Math.max(0, Math.floor(t / tickS + 1e-6) - 1));
}

export function sample(win: Window | null, pipeline: string, t: number, nTicks: number): Sample | null {
  if (!win) return null;
  const p = win.pipelines[pipeline];
  if (!p) return null;
  const k = Math.min(win.tick1 - 1, Math.max(win.tick0, tickAt(t, win.tick_s, nTicks)));
  const i = k - win.tick0;
  const heads: Record<string, number> = {};
  for (const [h, arr] of Object.entries(p.heads ?? {})) heads[h] = arr.data[i];
  let grid: Float32Array | null = null;
  let gridCells = 0;
  let extent = 0;
  if (p.grid) {
    gridCells = p.grid.grid_cells;
    extent = p.grid.grid_half_extent_m;
    const j = Math.min(p.grid.per_cell.shape[0] - 1, Math.floor(i / p.grid.step_ticks));
    const sz = gridCells * gridCells;
    grid = p.grid.per_cell.data.subarray(j * sz, (j + 1) * sz);
  }
  let bearing: Float32Array | null = null;
  if (p.bearing) {
    const j = Math.min(p.bearing.per_bearing.shape[0] - 1, Math.floor(i / p.bearing.step_ticks));
    const b = p.bearing.bins;
    bearing = p.bearing.per_bearing.data.subarray(j * b, (j + 1) * b);
  }
  const actors: { x: number; y: number }[] = [];
  const gt = win.recording.gt;
  if (gt) {
    const a = gt.actors.shape[1];
    for (let j = 0; j < a; j++) {
      const o = (i * a + j) * 4;
      actors.push({ x: gt.actors.data[o], y: gt.actors.data[o + 1] });
    }
  }
  const r = p.reflex;
  return {
    t,
    tick: k,
    global: p.surprise ? p.surprise.global.data[i] : null,
    heads,
    action: r ? (r.actions[r.action.data[i]] ?? "none") : "none",
    speedScale: r ? r.speed_scale.data[i] : null,
    risk: r ? r.risk.data[i] : null,
    threshold: r ? r.threshold.data[i] : null,
    tokens: p.surprise?.num_tokens ? p.surprise.num_tokens.data[i] : null,
    warming: p.surprise?.warming_up ? p.surprise.warming_up.data[i] === 1 : false,
    lux: win.recording.lux ? win.recording.lux.data[i] : null,
    grid,
    gridCells,
    extent,
    bearing,
    tracks: activeTracks(p.cortex ?? [], k),
    actors,
    latencyMs: p.latency ? p.latency.e2e_ms.data[i] : null,
  };
}

/** Latest state of each track updated in the last 0.5 s (50 ticks) and not lost. */
export function activeTracks(events: CortexEvent[], tick: number): Track[] {
  const by = new Map<number, CortexEvent>();
  for (const e of events) {
    if (e.tick > tick || e.track_id < 0) continue;
    by.set(e.track_id, e);
  }
  const out: Track[] = [];
  for (const e of by.values()) {
    if (e.kind === "track.lost" || tick - e.tick > 50 || e.x === null || e.y === null) continue;
    out.push({ id: e.track_id, label: e.label, x: e.x, y: e.y, vx: e.vx ?? 0, vy: e.vy ?? 0, confidence: e.confidence, text: e.text });
  }
  return out;
}

/** The fire in force at t: the last fire of the pipeline at or before t, if still latched. */
export function fireInForce(index: RunIndex, pipeline: string, t: number, action: string): Fire | null {
  if (action === "none") return null;
  let best: Fire | null = null;
  for (const f of index.fires) if (f.pipeline === pipeline && f.t_s <= t + 1e-6) best = f;
  return best;
}

export function fireNear(index: RunIndex, pipeline: string, t: number, dir: 1 | -1): Fire | null {
  const fs = index.fires.filter((f) => f.pipeline === pipeline);
  if (dir > 0) return fs.find((f) => f.t_s > t + 0.005) ?? null;
  return [...fs].reverse().find((f) => f.t_s < t - 0.005) ?? null;
}
