// Copyright 2026 SenseAI Institute
// SPDX-License-Identifier: Apache-2.0
//
// Typed access to the viewer API (README §"The window API").

import { decode, type NDArray } from "./msgpack.ts";

export interface PipelineMeta {
  name: string;
  pipeline: string;
  profile: string;
  profile_version: number;
  heads: string[];
  layers: Record<string, boolean>;
  grid_cells?: number;
  grid_half_extent_m?: number;
  bearing_bins?: number;
}

export interface Fire {
  pipeline: string;
  tick: number;
  t_s: number;
  action: string;
  explain: string;
  cells: number[];
  risk: number | null;
  threshold: number | null;
}

export interface FeedEvent {
  pipeline: string;
  tick: number;
  t_s: number;
  kind: string;
  text: string;
}

export interface Side {
  run_id: string;
  pipeline: string;
  profile: string;
  profile_version: number;
  first_fire_s: number | null;
  attributed_fire_s: number | null;
  delay_s: number | "miss" | null;
  cells_lit: number | null;
  cells_lit_at_s: number | null;
  false_fires: number;
  latency_p50_ms: number | null;
  latency_p99_ms: number | null;
  throughput: number | null;
}

export interface HeadToHead {
  a: Side;
  b: Side;
  lead_s: number | string | null;
  cells_lit_at_s: number | null;
}

export interface RunIndex {
  run_id: string;
  title: string;
  mode: string;
  scenario: string;
  synthetic: boolean;
  synthetic_note: string;
  tick_ns: number;
  t0_ns: string;
  n_ticks: number;
  duration_s: number;
  lux: { min: number | null; max: number | null };
  camera: { fx: number; cx: number; cy: number; mount_height_m: number; width: number; height: number } | null;
  pipelines: PipelineMeta[];
  layers: Record<string, boolean>;
  ground_truth: {
    t_gt_s?: number;
    t_visible_s?: number;
    clean_segments_s?: [number, number][];
    window_pre_s?: number;
    window_post_s?: number;
  };
  timeline: {
    t_s: number[];
    global_max: Record<string, number[]>;
    risk_max: Record<string, number[]>;
    threshold: Record<string, number | null>;
  };
  fires: Fire[];
  events: FeedEvent[];
  default_pair: [string, string] | null;
  head_to_head: HeadToHead | null;
}

export interface RunSummary {
  run_id: string;
  title: string;
  mode: string;
  scenario: string;
  synthetic: boolean;
  duration_s: number;
  default_pair: [string, string] | null;
  pipelines: string[];
  fires: number;
}

export interface CortexEvent {
  tick: number;
  t_s: number;
  kind: string;
  track_id: number;
  label: string;
  x: number | null;
  y: number | null;
  vx: number | null;
  vy: number | null;
  confidence: number;
  text: string;
}

export interface PipelineWindow {
  surprise?: {
    t_s: NDArray<Float32Array>;
    global: NDArray<Float32Array>;
    warming_up?: NDArray<Uint8Array>;
    num_tokens?: NDArray<Uint32Array>;
  };
  grid?: {
    grid_cells: number;
    grid_half_extent_m: number;
    bearing_bins: number;
    step_ticks: number;
    t_s: NDArray<Float32Array>;
    per_cell: NDArray<Float32Array>;
  };
  bearing?: { bins: number; step_ticks: number; t_s: NDArray<Float32Array>; per_bearing: NDArray<Float32Array> };
  heads?: Record<string, NDArray<Float32Array>>;
  reflex?: {
    actions: string[];
    action: NDArray<Uint8Array>;
    speed_scale: NDArray<Float32Array>;
    risk: NDArray<Float32Array>;
    threshold: NDArray<Float32Array>;
    fires: Fire[];
  };
  cortex?: CortexEvent[];
  latency?: { e2e_ms: NDArray<Float32Array> };
}

export interface Window {
  window_version: number;
  run_id: string;
  t0_s: number;
  t1_s: number;
  tick0: number;
  tick1: number;
  tick_s: number;
  missing: string[];
  recording: {
    events?: {
      source: string;
      width: number;
      height: number;
      total: number;
      kept: number;
      x: NDArray<Uint16Array>;
      y: NDArray<Uint16Array>;
      t_s: NDArray<Float32Array>;
      p: NDArray<Int8Array>;
    };
    frames?: {
      source: string;
      encoding: string;
      width: number;
      height: number;
      tick: NDArray<Int32Array>;
      t_s: NDArray<Float32Array>;
      lux: NDArray<Float32Array>;
      data: Uint8Array[];
    };
    gt?: { t_s: NDArray<Float32Array>; actors: NDArray<Float32Array> };
    lux?: NDArray<Float32Array>;
    robot_speed?: NDArray<Float32Array>;
  };
  pipelines: Record<string, PipelineWindow>;
}

async function getJSON<T>(url: string): Promise<T> {
  const r = await fetch(url);
  if (!r.ok) throw new Error(`${r.status} ${(await r.text()).slice(0, 200)}`);
  return (await r.json()) as T;
}

export const api = {
  runs: () => getJSON<RunSummary[]>("/api/runs"),
  index: (run: string) => getJSON<RunIndex>(`/api/runs/${encodeURIComponent(run)}/index`),
  compare: (a: string, b: string) =>
    getJSON<{ a: { run_id: string; pipeline: string }; b: { run_id: string; pipeline: string }; same_run: boolean; clock: { offset_s: number }; head_to_head: HeadToHead }>(
      `/api/compare?a=${encodeURIComponent(a)}&b=${encodeURIComponent(b)}`,
    ),
  async window(run: string, params: Record<string, string | number>): Promise<Window> {
    const q = new URLSearchParams(Object.entries(params).map(([k, v]) => [k, String(v)]));
    const r = await fetch(`/api/runs/${encodeURIComponent(run)}/window?${q}`);
    if (!r.ok) throw new Error(`${r.status} ${(await r.text()).slice(0, 200)}`);
    return decode(await r.arrayBuffer()) as Window;
  },
  exportUrl: (run: string, t: number, pipeline?: string) =>
    `/api/runs/${encodeURIComponent(run)}/incidents/export?t=${t.toFixed(3)}${pipeline ? `&pipeline=${encodeURIComponent(pipeline)}` : ""}`,
};

export const CHUNK_S = 2;
const WINDOW_PARAMS = {
  layers: "events,frames,surprise,grid,bearing,heads,reflex,cortex,gt,latency",
  max_events: 25000,
  frame_w: 173,
  max_frames: 10,
  grid_hz: 25,
};

/**
 * Two-second windows of one run, fetched on demand and kept in a small LRU. Playback asks
 * for the chunk under the playhead and prefetches the next one.
 */
export class Chunks {
  private cache = new Map<number, Promise<Window>>();
  private done = new Map<number, Window>();
  readonly run: string;
  readonly duration: number;
  onLoad: () => void = () => {};

  constructor(run: string, duration: number) {
    this.run = run;
    this.duration = duration;
  }

  key(t: number): number {
    return Math.max(0, Math.floor(Math.min(t, this.duration - 1e-6) / CHUNK_S));
  }

  /** The loaded chunk covering ``t``, or null (and start loading it). */
  at(t: number): Window | null {
    const k = this.key(t);
    const w = this.done.get(k);
    if (w) return w;
    this.load(k);
    return null;
  }

  load(k: number): Promise<Window> | null {
    if (k < 0 || k * CHUNK_S >= this.duration) return null;
    let p = this.cache.get(k);
    if (!p) {
      const t0 = k * CHUNK_S;
      const t1 = Math.min(this.duration, t0 + CHUNK_S);
      p = api.window(this.run, { ...WINDOW_PARAMS, t0, t1 });
      this.cache.set(k, p);
      p.then(
        (w) => {
          this.done.set(k, w);
          this.trim(k);
          this.onLoad();
        },
        () => this.cache.delete(k),
      );
    }
    return p;
  }

  prefetch(t: number): void {
    const k = this.key(t);
    this.load(k);
    this.load(k + 1);
  }

  private trim(keep: number): void {
    if (this.done.size <= 10) return;
    const far = [...this.done.keys()].sort((a, b) => Math.abs(b - keep) - Math.abs(a - keep));
    for (const k of far.slice(0, this.done.size - 10)) {
      this.done.delete(k);
      this.cache.delete(k);
    }
  }
}
