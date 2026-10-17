// Copyright 2026 SenseAI Institute
// SPDX-License-Identifier: Apache-2.0
//
// The simple view: one story, told in plain words. Two camera views side by side (the profile
// and the standard robot), a big status under each, one sentence saying what is happening,
// the key moments on a scrubber, and three results. Everything else lives in the detailed
// view (/replay/{run}). URL: /, /s/{run}, /story/{run}

import { api, Chunks, type Fire, type RunIndex } from "../api/client.ts";
import { el } from "../components/chrome.ts";
import { Scene } from "../panels/scene.ts";
import { sample } from "../sample.ts";
import { type LayerId, Player, UrlState } from "../state/player.ts";

const LAYERS = new Set<LayerId>(["frames", "events", "grid", "tracks"]);
const SLOW = 0.25;

type Tone = "info" | "good" | "warn" | "bad";
interface Moment {
  t: number;
  text: string;
  tone: Tone;
}

/** "With Panther" / "Standard robot": names a non-specialist can read. */
export function friendlyName(idx: RunIndex, pipeline: string): string {
  const p = idx.pipelines.find((x) => x.name === pipeline);
  if (!p || p.profile === "null" || p.pipeline === "null_profile") return "Standard robot";
  return `With ${p.profile[0].toUpperCase()}${p.profile.slice(1)}`;
}

function shortName(idx: RunIndex, pipeline: string): string {
  const n = friendlyName(idx, pipeline);
  return n.startsWith("With ") ? n.slice(5) : "The standard robot";
}

/** The story's key moments, in time order, from ground truth and reflex fires. */
export function moments(idx: RunIndex, sides: string[]): Moment[] {
  const out: Moment[] = [];
  const gt = idx.ground_truth;
  const isPerson = idx.events.some((e) => e.kind === "track.new" && e.text.startsWith("person"));
  const who = isPerson ? "A person" : "Something";
  if (gt.t_visible_s !== undefined) out.push({ t: gt.t_visible_s, text: `${who} steps out from behind a shelf`, tone: "info" });
  if (gt.t_gt_s !== undefined) out.push({ t: gt.t_gt_s, text: `${who === "A person" ? "The person" : "It"} is now in the robot's way`, tone: "warn" });
  const clean = gt.clean_segments_s ?? [];
  for (const f of idx.fires as Fire[]) {
    if (!sides.includes(f.pipeline)) continue;
    const name = shortName(idx, f.pipeline);
    const verb = f.action === "brake" || f.action === "hold" ? "brakes" : "slows down";
    if (clean.some(([a, b]) => f.t_s >= a && f.t_s < b)) {
      out.push({ t: f.t_s, text: `${name} ${verb}, but nothing is there: a false alarm`, tone: "bad" });
    } else {
      out.push({ t: f.t_s, text: `${name} ${verb}`, tone: f.pipeline === sides[0] ? "good" : "warn" });
    }
  }
  return out.sort((a, b) => a.t - b.t);
}

function status(action: string | undefined): { text: string; cls: string } {
  if (action === "slow") return { text: "Slowing down", cls: "warn" };
  if (action === "brake" || action === "hold") return { text: "Braking", cls: "alert" };
  if (action === "swerve") return { text: "Steering away", cls: "warn" };
  return { text: "Driving normally", cls: "ok" };
}

export async function storyPage(root: HTMLElement, run: string): Promise<void> {
  const idx = await api.index(run);
  const url = new UrlState();
  const pair = idx.default_pair ?? [idx.pipelines[0].name];
  const sides = pair.slice(0, 2);
  const ms = moments(idx, sides);
  const firstSeen = idx.ground_truth.t_visible_s;
  const start = firstSeen !== undefined ? Math.max(0, Math.round((firstSeen - 1.2) * 10) / 10) : 0;
  const player = new Player(idx.duration_s, url.num("t", start));
  player.setSpeed(SLOW);
  const chunks = new Chunks(run, idx.duration_s);
  chunks.onLoad = () => player.invalidate();
  player.canAdvance = (t) => chunks.at(t) !== null;
  document.title = `${idx.title} · SenseAI`;

  root.replaceChildren();
  const main = el("main", "story");
  root.append(main);

  // Headline: the result in one sentence.
  const h2h = idx.head_to_head;
  const lead = typeof h2h?.lead_s === "number" ? h2h.lead_s : null;
  const head = el("header", "story-head");
  const brand = el("div", "story-brand");
  brand.append(el("span", "brand-mark"), "SenseAI");
  const h1 = el("h1");
  if (sides.length > 1 && lead !== null && lead > 0) {
    h1.append(`In the dark, ${shortName(idx, sides[0])} reacts `);
    h1.append(el("em", undefined, `${lead.toFixed(2)} s sooner`));
    h1.append(" than a standard robot.");
  } else {
    h1.textContent = idx.title;
  }
  const lux = idx.lux.max;
  const sub = el(
    "p",
    undefined,
    `A robot drives down a warehouse aisle${lux !== null && lux <= 1 ? " in near darkness" : ""}. ` +
      "A person steps out in front of it. Press play and watch what each robot does.",
  );
  head.append(brand, h1, sub);
  main.append(head);

  // Two camera views with a big status under each.
  const views = el("section", `story-views${sides.length === 1 ? " one" : ""}`);
  const panes = sides.map((name, i) => {
    const pane = el("div", `story-pane ${i === 0 ? "a" : "b"}`);
    const title = el("div", "story-pane-title", friendlyName(idx, name));
    const scene = new Scene(idx, { simple: true });
    const badge = el("div", "story-status ok", "Driving normally");
    badge.setAttribute("aria-live", "polite");
    pane.append(title, scene.root, badge);
    views.append(pane);
    return { name, scene, badge, last: "" };
  });
  const hint = el("p", "story-hint", "The glow shows where the robot noticed something unexpected. The green box is what it recognised.");
  main.append(views, hint);

  // What is happening now, in one sentence.
  const caption = el("p", "story-caption");
  caption.setAttribute("aria-live", "polite");
  main.append(caption);

  // Controls and scrubber with the key moments on it.
  const controls = el("section", "story-controls");
  const play = el("button", "btn primary story-play", "▶ Play");
  const slow = el("button", "btn", "Slow motion");
  slow.setAttribute("aria-pressed", "true");
  const again = el("button", "btn", "↺ Watch again");
  const bar = el("div", "story-bar");
  bar.setAttribute("role", "slider");
  bar.setAttribute("aria-label", "Playback position");
  bar.tabIndex = 0;
  const fill = el("div", "story-fill");
  const knob = el("div", "story-knob");
  bar.append(fill);
  for (const m of ms) {
    const dot = el("button", `story-dot ${m.tone}`);
    dot.style.left = `${(m.t / idx.duration_s) * 100}%`;
    dot.title = `${m.t.toFixed(2)} s: ${m.text}`;
    dot.setAttribute("aria-label", dot.title);
    dot.addEventListener("click", (e) => {
      e.stopPropagation();
      player.seek(Math.max(0, m.t - 0.3));
    });
    bar.append(dot);
  }
  bar.append(knob);
  const time = el("span", "story-time mono");
  controls.append(play, slow, again, bar, time);
  main.append(controls);

  // Key moments, as a list you can click.
  const list = el("ol", "story-moments");
  const items = ms.map((m) => {
    const li = el("li", m.tone);
    const b = el("button");
    b.append(el("span", "mono", `${m.t.toFixed(2)} s`), el("span", undefined, m.text));
    b.addEventListener("click", () => player.seek(Math.max(0, m.t - 0.3)));
    li.append(b);
    list.append(li);
    return { m, li };
  });
  const momentsBox = el("section", "story-box");
  momentsBox.append(el("h2", undefined, "Key moments"), list);

  // Three results.
  const results = el("section", "story-box");
  results.append(el("h2", undefined, "The result"));
  if (h2h && sides.length > 1) {
    const a = h2h.a;
    const b = h2h.b;
    const cards = el("div", "story-cards");
    const card = (big: string, label: string, detail: string) => {
      const c = el("div", "story-card");
      c.append(el("div", "big", big), el("div", "label", label), el("div", "detail", detail));
      cards.append(c);
    };
    if (lead !== null) card(`${lead.toFixed(2)} s`, "sooner", `${shortName(idx, sides[0])} reacts this much earlier than the standard robot.`);
    const early = (d: number | string | null) => (typeof d === "number" ? (d < 0 ? "yes" : "no") : "no");
    card(
      early(a.delay_s) === "yes" ? "Before" : "After",
      "the person was in the way",
      `${shortName(idx, sides[0])}: ${early(a.delay_s) === "yes" ? "slowed before the person reached its path" : "slowed after"}. Standard robot: ${early(b.delay_s) === "yes" ? "before" : "only after"}.`,
    );
    card(`${a.false_fires} vs ${b.false_fires}`, "false alarms", `Times each robot slowed down when nothing was there.`);
    results.append(cards);
  } else {
    results.append(el("p", "muted", "This run has one robot only; open the detailed view for its numbers."));
  }
  const lower = el("div", "story-lower");
  lower.append(momentsBox, results);
  main.append(lower);

  // Footer: the way to the detail, and what the data is.
  const foot = el("footer", "story-foot");
  const more = el("a", undefined, "Open the detailed view →");
  more.href = `/replay/${encodeURIComponent(run)}?t=${player.t.toFixed(2)}`;
  const all = el("a", undefined, "All runs");
  all.href = "/runs";
  foot.append(more, all);
  if (idx.synthetic) foot.append(el("span", "muted", "Demo data: simulated to show how the viewer works, not a measurement."));
  main.append(foot);

  // Behaviour.
  const seekFromX = (clientX: number) => {
    const r = bar.getBoundingClientRect();
    player.seek(((clientX - r.left) / r.width) * idx.duration_s);
  };
  let dragging = false;
  bar.addEventListener("pointerdown", (e) => {
    dragging = true;
    bar.setPointerCapture(e.pointerId);
    seekFromX(e.clientX);
  });
  bar.addEventListener("pointermove", (e) => dragging && seekFromX(e.clientX));
  bar.addEventListener("pointerup", () => (dragging = false));
  bar.addEventListener("keydown", (e) => {
    if (e.key === "ArrowLeft") player.seek(player.t - 0.1);
    if (e.key === "ArrowRight") player.seek(player.t + 0.1);
  });
  play.addEventListener("click", () => player.toggle());
  slow.addEventListener("click", () => {
    const on = player.speed !== SLOW;
    player.setSpeed(on ? SLOW : 1);
    slow.setAttribute("aria-pressed", String(on));
  });
  again.addEventListener("click", () => {
    player.seek(start);
    if (!player.playing) player.toggle();
  });
  window.addEventListener("keydown", (e) => {
    if (e.key === " " && (e.target as HTMLElement).tagName !== "BUTTON") {
      e.preventDefault();
      player.toggle();
    }
  });

  player.on(() => {
    const t = player.t;
    chunks.prefetch(t);
    const win = chunks.at(t);
    for (const p of panes) {
      const s = sample(win, p.name, t, idx.n_ticks);
      p.scene.render({ t, win, sample: s, layers: LAYERS, colorblind: false, pathCells: null, profileLabel: "" });
      const st = status(s?.action);
      if (st.text !== p.last) {
        p.badge.textContent = st.text;
        p.badge.className = `story-status ${st.cls}`;
        p.last = st.text;
      }
    }
    let now: Moment | null = null;
    for (const m of ms) if (m.t <= t + 1e-6) now = m;
    const text = now ? now.text : "The aisle ahead is clear.";
    if (caption.textContent !== text) {
      caption.textContent = text;
      caption.className = `story-caption ${now?.tone ?? "info"}`;
    }
    for (const it of items) {
      it.li.classList.toggle("past", it.m.t <= t + 1e-6);
      it.li.classList.toggle("now", it.m === now);
    }
    const pct = (t / idx.duration_s) * 100;
    fill.style.width = `${pct}%`;
    knob.style.left = `${pct}%`;
    bar.setAttribute("aria-valuenow", t.toFixed(1));
    time.textContent = `${t.toFixed(2)} s`;
    play.textContent = player.playing ? "❚❚ Pause" : "▶ Play";
    more.href = `/replay/${encodeURIComponent(run)}?t=${t.toFixed(2)}`;
  });

  if (url.get("t") === null) player.toggle(); // autoplay from just before the action
}
