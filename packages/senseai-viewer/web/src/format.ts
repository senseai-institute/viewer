// Copyright 2026 SenseAI Institute
// SPDX-License-Identifier: Apache-2.0
//
// Formatting of the numbers on screen. The numbers come from the server (stats.py); this
// file only decides how they read.

const MINUS = "−";

export function seconds(s: number | null | undefined, digits = 2): string {
  if (s === null || s === undefined || !Number.isFinite(s)) return "—";
  return `${s.toFixed(digits)} s`;
}

export function signed(s: number | null | undefined, digits = 2, unit = " s"): string {
  if (s === null || s === undefined || !Number.isFinite(s)) return "—";
  const v = Math.abs(s) < 0.5 * 10 ** -digits ? 0 : s;
  return `${v > 0 ? "+" : v < 0 ? MINUS : "±"}${Math.abs(v).toFixed(digits)}${unit}`;
}

export function ms(v: number | null | undefined, digits = 1): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return "—";
  return `${v.toFixed(digits)} ms`;
}

export function pct(v: number | null | undefined): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return "—";
  return `${Math.round(v * 100)}%`;
}

/** Playback clock: m:ss.cc */
export function clock(s: number): string {
  const t = Math.max(0, s);
  const m = Math.floor(t / 60);
  const r = t - m * 60;
  return `${m}:${r < 10 ? "0" : ""}${r.toFixed(2)}`;
}

/** Reaction delay d = t_fire − t_gt, as the tile shows it. */
export function delay(d: number | "miss" | null): string {
  if (d === "miss") return "miss";
  if (d === null) return "—";
  return signed(d);
}

/** Bench's lead(A over B), possibly ±inf as strings from the server. */
export function lead(v: number | string | null): string {
  if (v === null) return "—";
  if (v === "+inf") return "B missed";
  if (v === "-inf") return "A missed";
  if (typeof v === "string") return v;
  return signed(v);
}

/** One plain sentence for the lead tile. */
export function leadSentence(a: string, b: string, v: number | string | null): string {
  if (typeof v !== "number") {
    if (v === "+inf") return `${b} never fired on the event`;
    if (v === "-inf") return `${a} never fired on the event`;
    return "needs ground truth";
  }
  const ms_ = Math.round(Math.abs(v) * 1000);
  if (ms_ === 0) return `${a} and ${b} fire together`;
  return v > 0 ? `${a} reacts ${ms_} ms earlier than ${b}` : `${a} reacts ${ms_} ms later than ${b}`;
}

/** Which side is better for a metric ("lower" or "higher" is better); null on a tie. */
export function better(a: number | null, b: number | null, prefer: "lower" | "higher"): "a" | "b" | null {
  if (a === null || b === null || !Number.isFinite(a) || !Number.isFinite(b) || a === b) return null;
  return (prefer === "lower" ? a < b : a > b) ? "a" : "b";
}

export function profileLabel(profile: string, version: number): string {
  if (profile === "null") return "default";
  return `${profile[0].toUpperCase()}${profile.slice(1)} v${version}`;
}
