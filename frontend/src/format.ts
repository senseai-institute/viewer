// Copyright 2026 SenseAI Institute
// SPDX-License-Identifier: Apache-2.0
// Number/date formatting and small DOM helpers.

export const DASH = "—";

export function fmtMs(ns: number | null | undefined): string {
  if (ns === null || ns === undefined || !Number.isFinite(ns)) return DASH;
  const ms = ns / 1e6;
  return `${ms < 10 ? ms.toFixed(3) : ms.toFixed(1)} ms`;
}

export function fmtDuration(s: number | null | undefined): string {
  if (s === null || s === undefined || !Number.isFinite(s)) return DASH;
  if (s < 60) return `${s.toFixed(s < 10 ? 2 : 1)} s`;
  const m = Math.floor(s / 60);
  return `${m} min ${Math.round(s - m * 60)
    .toString()
    .padStart(2, "0")} s`;
}

export function fmtInt(n: number | null | undefined): string {
  if (n === null || n === undefined || !Number.isFinite(n)) return DASH;
  return Math.round(n).toLocaleString("en-US");
}

export function fmtRate(hz: number | null | undefined): string {
  if (hz === null || hz === undefined || !Number.isFinite(hz)) return DASH;
  return `${hz >= 100 ? Math.round(hz).toLocaleString("en-US") : hz.toFixed(1)} /s`;
}

export function fmtDate(iso: string | null | undefined): string {
  if (!iso) return DASH;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toISOString().slice(0, 16).replace("T", " ");
}

type Attrs = Record<string, string | number | boolean | null | undefined>;
type Child = Node | string | null | undefined | false;

/** Tiny hyperscript: h("div", {class: "x"}, "text", child). */
export function h<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  attrs: Attrs = {},
  ...children: (Child | Child[])[]
): HTMLElementTagNameMap[K] {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === null || v === undefined || v === false) continue;
    el.setAttribute(k, v === true ? "" : String(v));
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    el.append(typeof c === "string" ? document.createTextNode(c) : c);
  }
  return el;
}

/** A "not available" placeholder. */
export function na(what = "not available"): HTMLElement {
  return h("div", { class: "na" }, what);
}

/** Status badge: good=true → green, false → red, null → neutral. */
export function badge(text: string, good: boolean | null): HTMLElement {
  const cls = good === null ? "badge" : good ? "badge ok" : "badge bad";
  return h("span", { class: cls }, text);
}
