// Copyright 2026 SenseAI Institute
// SPDX-License-Identifier: Apache-2.0
//
// The surprise colour scale: the deck's teal → amber → red, and a colour-blind-safe
// alternate (viridis stops). Values are per_cell surprise sums; see SCALE_MAX.

export type RGBA = [number, number, number, number];

/** per_cell value drawn at full intensity. Cells below MIN_VISIBLE are transparent. */
export const SCALE_MAX = 0.6;
export const MIN_VISIBLE = 0.04;

const DECK: [number, number, number, number][] = [
  // stop, r, g, b
  [0.0, 0x1b, 0x6f, 0x68],
  [0.3, 0x5e, 0xea, 0xd4],
  [0.65, 0xf2, 0xb3, 0x5c],
  [1.0, 0xff, 0x5a, 0x7a],
];
const VIRIDIS: [number, number, number, number][] = [
  [0.0, 0x44, 0x01, 0x54],
  [0.25, 0x3b, 0x52, 0x8b],
  [0.5, 0x21, 0x91, 0x8c],
  [0.75, 0x5e, 0xc9, 0x62],
  [1.0, 0xfd, 0xe7, 0x25],
];

function lerpStops(stops: [number, number, number, number][], x: number): [number, number, number] {
  for (let i = 1; i < stops.length; i++) {
    const [s1, r1, g1, b1] = stops[i];
    const [s0, r0, g0, b0] = stops[i - 1];
    if (x <= s1) {
      const f = (x - s0) / (s1 - s0);
      return [r0 + (r1 - r0) * f, g0 + (g1 - g0) * f, b0 + (b1 - b0) * f];
    }
  }
  const last = stops[stops.length - 1];
  return [last[1], last[2], last[3]];
}

/** Colour of one cell value. Alpha rises with the value so quiet cells stay see-through. */
export function heat(v: number, colorblind = false): RGBA {
  if (!(v > MIN_VISIBLE)) return [0, 0, 0, 0];
  const x = Math.min(1, Math.sqrt((v - MIN_VISIBLE) / (SCALE_MAX - MIN_VISIBLE)));
  const [r, g, b] = lerpStops(colorblind ? VIRIDIS : DECK, x);
  return [Math.round(r), Math.round(g), Math.round(b), 0.25 + 0.6 * x];
}

export function css(c: RGBA): string {
  return `rgba(${c[0]},${c[1]},${c[2]},${c[3].toFixed(3)})`;
}

/** CSS gradient for the legend. */
export function legendGradient(colorblind = false): string {
  const stops = colorblind ? VIRIDIS : DECK;
  return `linear-gradient(90deg, ${stops.map(([s, r, g, b]) => `rgb(${r},${g},${b}) ${s * 100}%`).join(", ")})`;
}
