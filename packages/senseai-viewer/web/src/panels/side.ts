// Copyright 2026 SenseAI Institute
// SPDX-License-Identifier: Apache-2.0
//
// The side panels: per-head surprise bars, head-to-head tiles, the explain card and the
// event feed. Each builds its DOM once and updates values in place.

import type { FeedEvent, Fire, HeadToHead, RunIndex } from "../api/client.ts";
import * as fmt from "../format.ts";
import type { Sample } from "../sample.ts";

function h<K extends keyof HTMLElementTagNameMap>(tag: K, cls?: string, text?: string): HTMLElementTagNameMap[K] {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}

function panel(title: string): { root: HTMLElement; head: HTMLElement } {
  const root = h("section", "panel");
  const head = h("h2", "panel-title", title);
  root.append(head);
  return { root, head };
}

// ---------------------------------------------------------------- senses

export class Senses {
  readonly root: HTMLElement;
  private rows = new Map<string, { bar: HTMLElement; ghost: HTMLElement; val: HTMLElement }>();
  private risk: { bar: HTMLElement; thr: HTMLElement; val: HTMLElement };
  private note: HTMLElement;

  constructor(heads: string[], otherLabel: string | null) {
    const p = panel("Senses");
    this.root = p.root;
    const bars = h("div", "bars");
    const mk = (label: string) => {
      const row = h("div", "bar-row");
      const bar = h("div", "bar");
      const fill = h("i");
      const ghost = h("em");
      bar.append(fill, ghost);
      const val = h("span", "val", "—");
      row.append(h("span", "label", label), bar, val);
      bars.append(row);
      return { bar: fill, ghost, val, wrap: bar };
    };
    for (const hd of [...heads, "global"]) {
      const r = mk(hd);
      this.rows.set(hd, r);
    }
    const r = mk("path risk");
    r.wrap.classList.add("risk");
    const thr = h("s");
    r.wrap.append(thr);
    r.ghost.remove();
    this.risk = { bar: r.bar, thr, val: r.val };
    this.note = h("div", "muted");
    this.note.style.fontSize = "12px";
    this.note.style.marginTop = "8px";
    this.note.textContent = otherLabel ? `Bars: per-head surprise, 0–10. The grey tick is ${otherLabel} at the same moment.` : "Bars: per-head surprise, 0–10.";
    this.root.append(bars, this.note);
  }

  update(s: Sample | null, other: Sample | null): void {
    for (const [hd, r] of this.rows) {
      const v = hd === "global" ? s?.global : s?.heads[hd];
      const o = hd === "global" ? other?.global : other?.heads[hd];
      r.bar.style.width = `${Math.min(100, ((v ?? 0) / 10) * 100)}%`;
      r.val.textContent = v == null ? "—" : v.toFixed(1);
      r.ghost.style.display = o == null ? "none" : "block";
      r.ghost.style.left = `${Math.min(100, ((o ?? 0) / 10) * 100)}%`;
    }
    const thr = s?.threshold ?? null;
    const full = thr ? thr * 2 : 5;
    const risk = s?.risk ?? 0;
    this.risk.bar.style.width = `${Math.min(100, (risk / full) * 100)}%`;
    this.risk.bar.style.background = thr !== null && risk > thr ? "var(--warn)" : "var(--teal)";
    this.risk.thr.style.left = thr ? "50%" : "-10px";
    this.risk.val.textContent = s?.risk == null ? "—" : s.risk.toFixed(1);
  }
}

// ---------------------------------------------------------------- head to head

export function headToHead(h2h: HeadToHead | null, labelA: string, labelB: string, colors = ["var(--teal)", "var(--side-b)"]): HTMLElement {
  const p = panel("Head to head");
  if (!h2h) {
    p.root.append(h("p", "muted", "One pipeline only. Open Compare to pit it against another run."));
    return p.root;
  }
  const { a, b } = h2h;
  const legend = h("div", "legend");
  for (const [lbl, c] of [[labelA, colors[0]], [labelB, colors[1]]]) {
    const it = h("span");
    const sw = h("i");
    sw.style.background = c;
    it.append(sw, lbl);
    legend.append(it);
  }
  const tiles = h("div", "tiles");
  const hero = h("div", "tile wide hero");
  hero.append(h("div", "k", "Lead time"), h("div", "v", fmt.lead(h2h.lead_s)), h("div", "sub", fmt.leadSentence(labelA, labelB, h2h.lead_s)));
  tiles.append(hero);
  const num = (v: number | string | null): number | null => (typeof v === "number" ? v : null);
  const pair = (k: string, va: string, vb: string, sub: string, best: "a" | "b" | null, wide = false) => {
    const t = h("div", `tile${wide ? " wide" : ""}`);
    const v = h("div", "v");
    const ea = h("span", `a${best === "a" ? " better" : ""}`, va);
    const eb = h("span", `b${best === "b" ? " better" : ""}`, vb);
    ea.style.color = colors[0];
    eb.style.color = colors[1];
    v.append(ea, h("span", "sep", "|"), eb);
    t.append(h("div", "k", k), v);
    if (sub) t.append(h("div", "sub", sub));
    tiles.append(t);
  };
  pair("First fire", fmt.seconds(a.attributed_fire_s ?? a.first_fire_s), fmt.seconds(b.attributed_fire_s ?? b.first_fire_s), "on the event", fmt.better(a.attributed_fire_s, b.attributed_fire_s, "lower"));
  pair("vs ground truth", fmt.delay(a.delay_s), fmt.delay(b.delay_s), "− = before in path", fmt.better(num(a.delay_s), num(b.delay_s), "lower"));
  pair("Cells lit", a.cells_lit == null ? "—" : String(a.cells_lit), b.cells_lit == null ? "—" : String(b.cells_lit), h2h.cells_lit_at_s != null ? `at ${h2h.cells_lit_at_s.toFixed(2)} s` : "", fmt.better(a.cells_lit, b.cells_lit, "higher"));
  pair("False fires", String(a.false_fires), String(b.false_fires), "clean segments", fmt.better(a.false_fires, b.false_fires, "lower"));
  pair("Latency p99", fmt.ms(a.latency_p99_ms), fmt.ms(b.latency_p99_ms), "end to end", fmt.better(a.latency_p99_ms, b.latency_p99_ms, "lower"));
  pair("Throughput", fmt.pct(a.throughput), fmt.pct(b.throughput), "mean speed scale, moving", fmt.better(a.throughput, b.throughput, "higher"));
  p.root.append(legend, tiles);
  return p.root;
}

// ---------------------------------------------------------------- explain

export class Explain {
  readonly root: HTMLElement;
  private sentence: HTMLElement;
  private meta: HTMLElement;
  private shown: Fire | null | undefined = undefined;
  private index: RunIndex;
  onSeek: (t: number) => void = () => {};

  constructor(index: RunIndex) {
    this.index = index;
    const p = panel("Explain");
    this.root = p.root;
    this.root.classList.add("explain");
    this.root.id = "explain";
    this.root.tabIndex = -1;
    this.sentence = h("p", "sentence empty", "No reflex command in force.");
    this.meta = h("div", "meta");
    this.root.append(this.sentence, this.meta);
  }

  update(fire: Fire | null, pipeline: string, t: number): void {
    if (fire === this.shown && fire !== null) return;
    this.shown = fire;
    this.meta.replaceChildren();
    if (fire) {
      this.sentence.className = "sentence";
      this.sentence.textContent = fire.explain || `${fire.action} at ${fire.t_s.toFixed(2)} s`;
      const jump = h("button", "btn", `jump to ${fire.t_s.toFixed(2)} s`);
      jump.addEventListener("click", () => this.onSeek(fire.t_s));
      this.meta.append(h("span", "badge warn", fire.action), jump);
      if (fire.cells.length) this.meta.append(h("span", "muted", `${fire.cells.length} path cells outlined`));
      return;
    }
    this.sentence.className = "sentence empty";
    const next = this.index.fires.find((f) => f.pipeline === pipeline && f.t_s > t);
    this.sentence.textContent = next ? `No reflex command in force. Next fire at ${next.t_s.toFixed(2)} s.` : "No reflex command in force.";
    if (next) {
      const go = h("button", "btn", "go to next fire  ]");
      go.addEventListener("click", () => this.onSeek(next.t_s));
      this.meta.append(go);
    }
  }
}

// ---------------------------------------------------------------- event feed

export class Feed {
  readonly root: HTMLElement;
  private list: HTMLUListElement;
  private items: { t: number; li: HTMLLIElement }[] = [];
  private lastActive = -1;
  onSeek: (t: number) => void = () => {};

  constructor(title = "Events") {
    const p = panel(title);
    this.root = p.root;
    this.list = h("ul", "feed");
    this.root.append(this.list);
  }

  set(events: FeedEvent[], fires: Fire[]): void {
    const rows = [
      ...events.map((e) => ({ t: e.t_s, kind: e.kind, text: e.text, fire: false })),
      ...fires.map((f) => ({ t: f.t_s, kind: `reflex.${f.action}`, text: f.explain, fire: true })),
    ]
      .filter((r) => !(r.kind === "reflex.fire")) // the fire rows above carry the same text
      .sort((a, b) => a.t - b.t);
    this.items = rows.map((r) => {
      const li = h("li");
      li.tabIndex = 0;
      const t = h("span", "t", r.t.toFixed(2));
      const body = h("span");
      body.append(h("span", `kind${r.fire ? " fire" : ""}`, r.kind), r.text);
      li.append(t, body);
      const go = () => this.onSeek(r.t);
      li.addEventListener("click", go);
      li.addEventListener("keydown", (e) => e.key === "Enter" && go());
      return { t: r.t, li };
    });
    this.list.replaceChildren(...this.items.map((i) => i.li));
    if (!rows.length) this.list.append(h("li", "muted", "No cortex events in this run."));
    this.lastActive = -1;
  }

  update(t: number): void {
    let active = -1;
    this.items.forEach((it, i) => {
      const fut = it.t > t + 1e-6;
      it.li.classList.toggle("future", fut);
      if (!fut) active = i;
    });
    if (active !== this.lastActive && active >= 0) {
      this.lastActive = active;
      const li = this.items[active].li;
      const top = li.offsetTop - this.list.offsetTop;
      if (top < this.list.scrollTop || top > this.list.scrollTop + this.list.clientHeight - 30) this.list.scrollTop = Math.max(0, top - 60);
    }
  }
}
