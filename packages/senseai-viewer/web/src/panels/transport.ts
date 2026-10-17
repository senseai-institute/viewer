// Copyright 2026 SenseAI Institute
// SPDX-License-Identifier: Apache-2.0
//
// Transport: play/pause, speed, profile toggle, fire navigation, layer chips, colour scale,
// share. Pure view: every control calls back into the page.

import * as fmt from "../format.ts";
import { LAYERS, type LayerId, SPEEDS } from "../state/player.ts";

export interface TransportOpts {
  duration: number;
  profiles: { name: string; label: string }[] | null; // null = no profile toggle
  onToggle: () => void;
  onSpeed: (s: number) => void;
  onProfile: (name: string) => void;
  onLayer: (id: LayerId) => void;
  onFire: (dir: 1 | -1) => void;
  onColorblind: () => void;
  onShare: () => void;
  onHelp: () => void;
}

function btn(text: string, cls: string, label: string, onClick: () => void): HTMLButtonElement {
  const b = document.createElement("button");
  b.className = cls;
  b.textContent = text;
  b.setAttribute("aria-label", label);
  b.title = label;
  b.addEventListener("click", onClick);
  return b;
}

export class Transport {
  readonly root: HTMLElement;
  private play: HTMLButtonElement;
  private clockEl: HTMLElement;
  private speeds: HTMLButtonElement[] = [];
  private profiles: HTMLButtonElement[] = [];
  private chips = new Map<LayerId, HTMLButtonElement>();
  private cb: HTMLButtonElement;
  private status: HTMLElement;
  private duration: number;

  constructor(o: TransportOpts) {
    this.duration = o.duration;
    this.root = document.createElement("section");
    this.root.className = "panel transport";
    this.play = btn("▶", "btn primary icon", "Play (space)", o.onToggle);
    this.clockEl = document.createElement("span");
    this.clockEl.className = "clock";
    this.clockEl.setAttribute("aria-live", "off");
    const seg = document.createElement("span");
    seg.className = "seg";
    seg.setAttribute("role", "group");
    seg.setAttribute("aria-label", "Playback speed");
    for (const s of SPEEDS) {
      const b = btn(`${s}×`, "", `Speed ${s}×`, () => o.onSpeed(s));
      this.speeds.push(b);
      seg.append(b);
    }
    this.root.append(
      btn("⏮", "btn icon", "Previous fire ([)", () => o.onFire(-1)),
      this.play,
      btn("⏭", "btn icon", "Next fire (])", () => o.onFire(1)),
      this.clockEl,
      seg,
    );
    if (o.profiles && o.profiles.length > 1) {
      const ps = document.createElement("span");
      ps.className = "seg profile";
      ps.setAttribute("role", "group");
      ps.setAttribute("aria-label", "Profile (p)");
      for (const p of o.profiles) {
        const b = btn(p.label, "", `Show ${p.label}`, () => o.onProfile(p.name));
        b.dataset.name = p.name;
        this.profiles.push(b);
        ps.append(b);
      }
      this.root.append(ps);
    }
    const chips = document.createElement("span");
    chips.className = "chips";
    chips.setAttribute("role", "group");
    chips.setAttribute("aria-label", "Layers");
    LAYERS.forEach((l, i) => {
      const b = document.createElement("button");
      b.className = "chip";
      b.innerHTML = `<kbd>${i + 1}</kbd>`;
      b.append(l.label);
      b.setAttribute("aria-label", `Layer ${l.label} (${i + 1})`);
      b.addEventListener("click", () => o.onLayer(l.id));
      this.chips.set(l.id, b);
      chips.append(b);
    });
    this.root.append(chips);
    this.status = document.createElement("span");
    this.status.className = "muted grow";
    this.status.style.fontSize = "13px";
    this.cb = btn("CB", "btn", "Colour-blind-safe scale (c)", o.onColorblind);
    this.root.append(this.status, this.cb, btn("Share", "btn", "Copy a link to this moment", o.onShare), btn("?", "btn icon", "Keyboard shortcuts (?)", o.onHelp));
  }

  update(st: { t: number; playing: boolean; speed: number; profile: string | null; layers: Set<LayerId>; colorblind: boolean; buffering: boolean }): void {
    this.play.textContent = st.playing ? "❚❚" : "▶";
    this.play.setAttribute("aria-label", st.playing ? "Pause (space)" : "Play (space)");
    this.clockEl.textContent = `${fmt.clock(st.t)} / ${fmt.clock(this.duration)}`;
    this.speeds.forEach((b, i) => b.setAttribute("aria-pressed", String(SPEEDS[i] === st.speed)));
    this.profiles.forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.name === st.profile)));
    for (const [id, b] of this.chips) b.setAttribute("aria-pressed", String(st.layers.has(id)));
    this.cb.setAttribute("aria-pressed", String(st.colorblind));
    this.status.textContent = st.buffering ? "loading data…" : "";
  }

  flash(msg: string): void {
    this.status.textContent = msg;
  }
}

/** Global keyboard shortcuts (README §Keyboard). */
export function bindKeys(map: Record<string, (e: KeyboardEvent) => void>): () => void {
  const fn = (e: KeyboardEvent) => {
    const tgt = e.target as HTMLElement | null;
    if (tgt && (tgt.tagName === "INPUT" || tgt.tagName === "SELECT" || tgt.tagName === "TEXTAREA")) return;
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    const k = e.key === " " ? "space" : e.key;
    const handler = map[k];
    if (!handler) return;
    if (k === "space" && tgt?.tagName === "BUTTON") return; // let the focused button act
    e.preventDefault();
    handler(e);
  };
  window.addEventListener("keydown", fn);
  return () => window.removeEventListener("keydown", fn);
}
