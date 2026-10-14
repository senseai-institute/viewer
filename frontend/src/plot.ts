// Copyright 2026 SenseAI Institute
// SPDX-License-Identifier: Apache-2.0
// uPlot helpers shared by the timeline lanes and the compare chart.

import uPlot from "uplot";

export const PALETTE = ["#2f6fde", "#e0663a", "#1a9e77", "#b04fc4", "#c9a227", "#5b6b7f"];

export function cssVar(name: string): string {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

export function axis(label?: string, side: 1 | 3 = 3, size = 46): uPlot.Axis {
  const grid = cssVar("--grid");
  const ink = cssVar("--muted");
  return {
    side,
    scale: side === 1 ? "y2" : "y",
    label,
    labelSize: label ? 14 : 0,
    size,
    stroke: ink,
    grid: { stroke: grid, width: 1, show: side === 3 },
    ticks: { stroke: grid, width: 1 },
    font: "11px system-ui, sans-serif",
    labelFont: "11px system-ui, sans-serif",
    values: (_u, vals) => vals.map((v) => compact(v)),
  };
}

/** Right-hand axis space with nothing in it, so every lane has the same plot width. */
export function spacerAxis(size = 44): uPlot.Axis {
  return { side: 1, scale: "y", size, values: () => [], grid: { show: false }, ticks: { show: false }, stroke: "transparent" };
}

export function xAxis(show: boolean): uPlot.Axis {
  const grid = cssVar("--grid");
  return {
    show,
    stroke: cssVar("--muted"),
    grid: { stroke: grid, width: 1 },
    ticks: { stroke: grid, width: 1 },
    font: "11px system-ui, sans-serif",
    size: show ? 28 : 0,
    values: (_u, vals) => vals.map((v) => `${+v.toFixed(3)} s`),
  };
}

export function compact(v: number): string {
  const a = Math.abs(v);
  if (a >= 1e6) return `${+(v / 1e6).toFixed(1)}M`;
  if (a >= 1e3) return `${+(v / 1e3).toFixed(a < 1e4 ? 2 : 1)}k`;
  if (a === 0) return "0";
  if (a < 0.01) return v.toExponential(0);
  return `${+v.toFixed(2)}`;
}

const barsPath = uPlot.paths.bars?.({ size: [0.9, Infinity], gap: 0 });

export function lineSeries(label: string, color: string, scale = "y"): uPlot.Series {
  return { label, stroke: color, width: 1.25, scale, points: { show: false } };
}

export function barSeries(label: string, color: string): uPlot.Series {
  return { label, stroke: color, fill: `${color}99`, width: 0, paths: barsPath, points: { show: false } };
}

export function dotSeries(label: string, color: string): uPlot.Series {
  return {
    label,
    stroke: color,
    fill: color,
    width: 0,
    paths: () => null,
    points: { show: true, size: 5, stroke: color, fill: color, width: 1 },
  };
}

export function stepSeries(label: string, color: string): uPlot.Series {
  return { label, stroke: color, width: 1.25, paths: uPlot.paths.stepped?.({ align: 1 }), points: { show: false } };
}
