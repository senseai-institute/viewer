// Copyright 2026 SenseAI Institute
// SPDX-License-Identifier: Apache-2.0
//
// The playback clock: t, play/pause, speed. One per page; panels redraw on change.

export const SPEEDS = [0.25, 0.5, 1, 2, 4] as const;

export class Player {
  t = 0;
  playing = false;
  speed = 1;
  readonly duration: number;
  /** Return false to hold the clock (e.g. the chunk under the playhead is still loading). */
  canAdvance: (t: number) => boolean = () => true;
  private listeners: (() => void)[] = [];
  private last = 0;
  private raf = 0;
  private dirty = true;

  constructor(duration: number, t = 0) {
    this.duration = duration;
    this.t = this.clamp(t);
    this.raf = requestAnimationFrame(this.loop);
  }

  on(fn: () => void): void {
    this.listeners.push(fn);
  }

  clamp(t: number): number {
    return Math.min(this.duration, Math.max(0, t));
  }

  seek(t: number): void {
    this.t = this.clamp(t);
    this.invalidate();
  }

  invalidate(): void {
    this.dirty = true;
  }

  toggle(): void {
    if (!this.playing && this.t >= this.duration - 1e-3) this.t = 0;
    this.playing = !this.playing;
    this.last = performance.now();
    this.invalidate();
  }

  setSpeed(s: number): void {
    this.speed = s;
    this.invalidate();
  }

  destroy(): void {
    cancelAnimationFrame(this.raf);
  }

  private loop = (now: number) => {
    if (this.playing) {
      const dt = Math.min(0.1, (now - this.last) / 1000);
      if (this.canAdvance(this.t)) {
        this.t = this.clamp(this.t + dt * this.speed);
        if (this.t >= this.duration) this.playing = false;
      }
      this.dirty = true;
    }
    this.last = now;
    if (this.dirty) {
      this.dirty = false;
      for (const fn of this.listeners) fn();
    }
    this.raf = requestAnimationFrame(this.loop);
  };
}

/** Layers the scene can toggle, in keyboard order 1–7. */
export const LAYERS = [
  { id: "events", label: "events" },
  { id: "frames", label: "frames" },
  { id: "grid", label: "surprise" },
  { id: "bearing", label: "bearing" },
  { id: "path", label: "path cells" },
  { id: "tracks", label: "tracks" },
  { id: "gt", label: "ground truth" },
] as const;
export type LayerId = (typeof LAYERS)[number]["id"];

export function parseLayers(v: string | null): Set<LayerId> {
  const all = new Set<LayerId>(LAYERS.map((l) => l.id));
  if (v === null) return all;
  return new Set(v.split(",").filter((x): x is LayerId => all.has(x as LayerId)));
}

/** Read and write page state in the query string (replaceState, throttled). */
export class UrlState {
  private timer = 0;
  get(key: string): string | null {
    return new URLSearchParams(location.search).get(key);
  }
  num(key: string, dflt: number): number {
    const v = Number(this.get(key));
    return this.get(key) !== null && Number.isFinite(v) ? v : dflt;
  }
  set(values: Record<string, string | null>, immediate = false): void {
    const apply = () => {
      const q = new URLSearchParams(location.search);
      for (const [k, v] of Object.entries(values)) {
        if (v === null) q.delete(k);
        else q.set(k, v);
      }
      const s = q.toString();
      history.replaceState(null, "", `${location.pathname}${s ? `?${s}` : ""}`);
    };
    clearTimeout(this.timer);
    if (immediate) apply();
    else this.timer = window.setTimeout(apply, 250);
  }
}
