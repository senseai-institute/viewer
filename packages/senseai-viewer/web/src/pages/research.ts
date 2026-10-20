// Copyright 2026 SenseAI Institute
// SPDX-License-Identifier: Apache-2.0
//
// Research: talk with the research assistant about the species library, check where each value
// came from, see what a correction does to the robot, and approve or reject changes.
// Chat on the left; context on the right (Claim, Source, Impact, Experiment). The backend is
// senseai-research, reached through /api/research (server/senseai_viewer/research.py).
// Nothing here shows raw JSON: tool calls are chips, results are tables and sentences.

import { el, topbar } from "../components/chrome.ts";

// ------------------------------------------------------------------ API

interface Row { label: string; before: string; after: string }
interface Card { title?: string; summary?: string; rows?: Row[]; quote?: string | null; diff?: string; note?: string }
interface Action { id: string; kind: string; card: Card; status: string; error?: string | null; result?: Record<string, unknown> }
interface Refs { claim_id?: string; impact_id?: string; experiment_id?: string; source_id?: string; doi?: string; section?: string; text?: string; context_before?: string; context_after?: string; pending_action_id?: string }
interface Item { kind: string; text?: string; id?: string; name?: string; label?: string; ok?: boolean; refs?: Refs; action?: Action }
interface LeastVerified { claim_id: string; library_path: string; value: string; status: string; why: string; species: string | null; read_by: string[] }
interface OpenQuestion { hypothesis_id: string; statement: string; priority: string; target: string | null; read_by: string[] }
interface Source { source_id: string; title: string; authors: string; year: number | null; venue: string; doi: string | null; link: string | null; full_text: boolean; checked: string | null }
interface Passage { passage_id: string; section: string; quote: string; before: string; after: string; source: Source }
interface HistoryRow { revision: number; at: string; by: string; summary: string; reason: string }
interface ClaimCard {
  value: string;
  history: HistoryRow[];
  sources: Source[];
  passage: Passage | null;
  claim: { id: string; library_path: string; status: string; species: string | null; conditions: string | null; method: string | null; mapping: string | null; library_notes: string | null; notes_by_reviewer: string; unverified_reason: string | null; revision: number };
}
interface ProfileRow { profile: string; section: string; field: string; point: string; old: number | string | null; new: number | string | null; delta_pct: number | null }
interface Impact {
  id: string; library_path: string; old_quantity: Record<string, unknown>; new_quantity: Record<string, unknown>;
  consumers: string[]; profile_rows: ProfileRow[]; field_summaries: { sentence: string }[]; sentences: string[];
  world_available: boolean; world_reached: boolean; world_note: string;
  reflex_rows: { arm: string; theta_old: number; theta_new: number }[];
}
interface Comparison {
  arm: string; objects: number | null; judged: boolean; verdict: string;
  baseline_hit_rate: number | null; baseline_chance_hit_rate: number | null;
  patched_hit_rate: number | null; patched_chance_hit_rate: number | null;
  baseline_median_delay_s: number | null; patched_median_delay_s: number | null;
  baseline_fires_per_h: number | null; patched_fires_per_h: number | null;
  baseline_latency_p99_ms: number | null; patched_latency_p99_ms: number | null;
}
interface ExperimentView {
  experiment: { id: string; status: string; sequences: string[]; arms: string[]; error: string | null; profiles_sha: string; bench_sha: string; world_sha: string };
  comparison: Comparison[];
  runlogs: string[];
  progress: { frac?: number; message?: string } | null;
}

interface KeyVar { name: string; aliases: string[]; secret: boolean; set: boolean }
interface Service {
  id: string; name: string; group: string; purpose: string; need: "required" | "recommended" | "optional" | "none";
  without: string; get_key: string; state: "set" | "partial" | "not_set" | "no_key"; vars: KeyVar[]; checkable: boolean;
}
interface SettingsPage { groups: string[]; integrations: Service[]; where: string }
interface KeyCheck { id: string; ok: boolean; detail: string; cached: boolean }

async function json<T>(url: string, init?: RequestInit): Promise<T> {
  const r = await fetch(url, init);
  if (!r.ok) {
    let msg = `${r.status}`;
    try { msg = (await r.json()).detail ?? msg; } catch { /* not JSON */ }
    throw new Error(msg);
  }
  return (await r.json()) as T;
}

const rapi = {
  status: () => json<{ enabled: boolean }>("/api/research-status"),
  start: () => json<{ open_questions: OpenQuestion[]; least_verified: LeastVerified[] }>("/api/research/start"),
  newConversation: () => json<{ id: string }>("/api/research/conversations", { method: "POST" }),
  conversation: (id: string) => json<{ id: string; items: Item[] }>(`/api/research/conversations/${encodeURIComponent(id)}`),
  claim: (id: string) => json<ClaimCard>(`/api/research/claims/${encodeURIComponent(id)}`),
  impact: (id: string) => json<Impact>(`/api/research/impact/${encodeURIComponent(id)}`),
  experiment: (id: string) => json<ExperimentView>(`/api/research/experiments/${encodeURIComponent(id)}`),
  decide: (id: string, decision: "approve" | "reject", rationale: string) =>
    json<{ message: string; action: Action }>(`/api/research/actions/${encodeURIComponent(id)}/${decision}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ rationale }),
    }),
  openRun: (id: string) => json<{ run_ids: string[] }>(`/api/research-open/${encodeURIComponent(id)}`, { method: "POST" }),
  settings: () => json<SettingsPage>("/api/research/settings"),
  checkKey: (id: string) => json<KeyCheck>(`/api/research/settings/${encodeURIComponent(id)}/check`, { method: "POST" }),
};

/** POST a message and read the Server-Sent Events the research server streams back. */
async function send(conv: string, text: string, focus: Record<string, string> | null, on: (event: string, data: any) => void): Promise<void> {
  const r = await fetch(`/api/research/conversations/${encodeURIComponent(conv)}/messages`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
    body: JSON.stringify({ text, focus }),
  });
  if (!r.ok || !r.body) {
    let msg = `${r.status}`;
    try { msg = (await r.json()).detail ?? msg; } catch { /* not JSON */ }
    throw new Error(msg);
  }
  const reader = r.body.getReader();
  const dec = new TextDecoder();
  let buf = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true }).replace(/\r\n/g, "\n");
    let i: number;
    while ((i = buf.indexOf("\n\n")) >= 0) {
      const block = buf.slice(0, i);
      buf = buf.slice(i + 2);
      let ev = "message";
      const data: string[] = [];
      for (const line of block.split("\n")) {
        if (line.startsWith("event:")) ev = line.slice(6).trim();
        else if (line.startsWith("data:")) data.push(line.slice(5).trimStart());
      }
      if (data.length) on(ev, JSON.parse(data.join("\n")));
    }
  }
}

// ------------------------------------------------------------------ formatting

/** Who made a change, in plain words. */
const who = (by: string) =>
  by.startsWith("user:") ? by.slice(5) : by.startsWith("pipeline:import") ? "imported from the library" : by.startsWith("agent:") ? "the assistant" : by.replace(/^\w+:/, "");
const pct = (v: number | null) => (v === null ? "n/a" : `${Math.round(v * 100)}%`);
const num = (v: number | string | null, d = 3) => (v === null ? "—" : typeof v === "number" ? Number(v.toPrecision(d)).toString() : v);
const STATUS_TEXT: Record<string, string> = {
  unverified: "unverified", extracted: "quote found", disputed: "disputed", verified: "verified", rejected: "rejected", superseded: "superseded",
};
function statusBadge(s: string): HTMLElement {
  const cls = s === "verified" ? "teal" : s === "unverified" || s === "disputed" ? "warn" : s === "rejected" ? "alert" : "";
  const b = el("span", `badge ${cls}`, STATUS_TEXT[s] ?? s);
  b.dataset.status = s;
  return b;
}

/** Assistant text as paragraphs and bullet lists; “quotes” stay as written. No HTML injection. */
function prose(text: string): HTMLElement {
  const box = el("div", "prose");
  for (const para of text.split(/\n{2,}/)) {
    const lines = para.split("\n");
    if (lines.every((l) => /^\s*[-•]\s+/.test(l))) {
      const ul = el("ul");
      for (const l of lines) ul.append(el("li", undefined, l.replace(/^\s*[-•]\s+/, "")));
      box.append(ul);
    } else {
      box.append(el("p", undefined, para));
    }
  }
  return box;
}

// ------------------------------------------------------------------ page

type Tab = "claim" | "source" | "impact" | "experiment" | "settings";

const NEED_TEXT: Record<Service["need"], string> = {
  required: "Required", recommended: "Recommended", optional: "Optional", none: "No key needed",
};

function keyState(s: Service): string {
  if (s.state === "no_key") return "No key needed";
  if (s.state === "set") return "Set";
  const missing = s.vars.filter((v) => !v.set).map((v) => v.name);
  return s.state === "partial" ? `Partly set; missing ${missing.join(", ")}` : "Not set";
}

/** Text with any https:// address turned into a link that opens in a new tab. */
function linked(text: string): HTMLElement {
  const span = el("span");
  let at = 0;
  for (const m of text.matchAll(/https?:\/\/[^\s)]+/g)) {
    span.append(text.slice(at, m.index));
    const a = el("a", undefined, m[0]) as HTMLAnchorElement;
    a.href = m[0];
    a.target = "_blank";
    a.rel = "noopener";
    span.append(a);
    at = (m.index ?? 0) + m[0].length;
  }
  span.append(text.slice(at));
  return span;
}

/** The Settings tab: each outside service, whether its key is set, and a live test. Keys are
 * set on the research server (Railway variables or research.env); this page never sees them. */
function settingsView(page: SettingsPage): HTMLElement {
  const wrap = el("div", "settings-view");
  wrap.append(el("p", "muted", page.where));
  for (const g of page.groups) {
    wrap.append(el("h3", "section-name", g));
    const table = el("table", "grid settings-table");
    const head = el("tr");
    for (const h of ["Service", "Needed", "Key", ""]) head.append(el("th", undefined, h));
    table.append(head);
    for (const s of page.integrations.filter((i) => i.group === g)) {
      const tr = el("tr");
      tr.dataset.service = s.id;
      tr.dataset.state = s.state;
      const name = el("td");
      const more = el("details", "service-more");
      more.append(el("summary", undefined, s.name));
      more.append(el("p", undefined, s.purpose));
      const without = el("p");
      without.append(el("strong", undefined, "Without it: "), s.without);
      const get = el("p");
      get.append(el("strong", undefined, "Get a key: "), linked(s.get_key));
      more.append(without, get);
      name.append(more);
      const key = el("td");
      key.append(el("span", `key-state ${s.state}`, keyState(s)));
      for (const v of s.vars) key.append(el("div", "var-name", v.name));
      const test = el("td", "key-test");
      if (s.checkable) {
        const btn = el("button", "btn small", "Test") as HTMLButtonElement;
        const out = el("span", "check-result");
        btn.addEventListener("click", async () => {
          btn.disabled = true;
          out.textContent = "testing…";
          out.className = "check-result";
          try {
            const r = await rapi.checkKey(s.id);
            out.textContent = r.detail;
            out.className = `check-result ${r.ok ? "ok" : "problem"}`;
          } catch (e) {
            out.textContent = `could not test: ${(e as Error).message}`;
            out.className = "check-result problem";
          } finally {
            btn.disabled = false;
          }
        });
        test.append(btn, out);
      }
      tr.append(name, el("td", `need ${s.need}`, NEED_TEXT[s.need]), key, test);
      table.append(tr);
    }
    wrap.append(table);
  }
  return wrap;
}

export async function researchPage(root: HTMLElement): Promise<void> {
  document.title = "Research · SenseAI Viewer";
  root.replaceChildren(topbar({ current: "research", title: "Check the numbers behind Panther" }));
  const main = el("main", "research");
  root.append(main);

  // --- chat column
  const chatCol = el("section", "research-chat panel");
  chatCol.setAttribute("aria-label", "Conversation");
  const chatHead = el("div", "panel-title");
  chatHead.append(el("span", undefined, "Conversation"), el("span", "spacer"));
  const newBtn = el("button", "btn small", "New conversation");
  chatHead.append(newBtn);
  const log = el("div", "chat-log");
  log.setAttribute("aria-live", "polite");
  const start = el("div", "chat-start");
  const form = el("form", "composer");
  const input = el("textarea");
  input.rows = 3;
  input.placeholder = "Ask about a value, a paper, or what a change would do…";
  input.setAttribute("aria-label", "Your message");
  const sendBtn = el("button", "btn primary", "Send");
  sendBtn.type = "submit";
  form.append(input, sendBtn);
  chatCol.append(chatHead, start, log, form);

  // --- context column
  const ctxCol = el("aside", "research-context panel");
  ctxCol.setAttribute("aria-label", "Context");
  const tabs = el("div", "tabs");
  tabs.setAttribute("role", "tablist");
  const body = el("div", "tab-body");
  const panes: Record<Tab, HTMLElement> = {
    claim: el("div", "pane"), source: el("div", "pane"), impact: el("div", "pane"), experiment: el("div", "pane"),
    settings: el("div", "pane"),
  };
  const tabBtns: Record<string, HTMLButtonElement> = {};
  const labels: [Tab, string][] = [
    ["claim", "Claim"], ["source", "Source"], ["impact", "Impact"], ["experiment", "Experiment"], ["settings", "Settings"],
  ];
  for (const [t, label] of labels) {
    const b = el("button", "tab", label);
    b.setAttribute("role", "tab");
    b.id = `tab-${t}`;
    b.addEventListener("click", () => show(t));
    tabBtns[t] = b;
    tabs.append(b);
    panes[t].setAttribute("role", "tabpanel");
    panes[t].setAttribute("aria-labelledby", b.id);
    panes[t].append(el("p", "muted", empty(t)));
    body.append(panes[t]);
  }
  ctxCol.append(tabs, body);
  main.append(chatCol, ctxCol);

  function empty(t: Tab): string {
    return {
      claim: "Pick a value on the left, or ask about one. Its value, conditions, status and full history show here.",
      source: "When the assistant finds a passage in a paper, it shows here with the quote highlighted.",
      impact: "When the assistant works out what a change does, the affected robot settings show here.",
      experiment: "Bench results on night recordings show here, with the chance hit rate beside every hit rate.",
      settings: "loading the list of outside services…",
    }[t];
  }
  function show(t: Tab): void {
    for (const [k, b] of Object.entries(tabBtns)) b.setAttribute("aria-selected", String(k === t));
    for (const [k, p] of Object.entries(panes)) p.hidden = k !== t;
    if (t === "settings") void openSettings();
  }

  async function openSettings(): Promise<void> {
    try {
      panes.settings.replaceChildren(settingsView(await rapi.settings()));
    } catch (e) {
      panes.settings.replaceChildren(el("p", "error", `Could not load the settings: ${(e as Error).message}`));
    }
  }
  show("claim");

  // --- state
  const params = new URLSearchParams(location.search);
  let conv = params.get("c") ?? "";
  let focus: Record<string, string> | null = null;
  let busy = false;

  function setConv(id: string): void {
    conv = id;
    const u = new URL(location.href);
    u.searchParams.set("c", id);
    history.replaceState(null, "", u);
  }

  // --- context loaders
  async function openClaim(id: string): Promise<void> {
    focus = { claim_id: id };
    show("claim");
    const pane = panes.claim;
    pane.replaceChildren(el("p", "muted", "loading…"));
    try {
      const c = await rapi.claim(id);
      pane.replaceChildren(claimView(c));
      if (c.passage) openPassage({ source: c.passage.source, quote: c.passage.quote, section: c.passage.section, before: c.passage.before, after: c.passage.after }, false);
    } catch (e) {
      pane.replaceChildren(el("p", "error", `Could not load the claim: ${(e as Error).message}`));
    }
  }

  function openPassage(p: { source?: Source; doi?: string | null; quote: string; section: string; before: string; after: string }, select = true): void {
    const pane = panes.source;
    pane.replaceChildren();
    const doi = p.source?.doi ?? p.doi ?? null;
    if (p.source) {
      pane.append(el("h3", undefined, p.source.title || doi || p.source.source_id));
      const meta = el("p", "muted", [p.source.authors, p.source.year, p.source.venue].filter(Boolean).join(" · "));
      pane.append(meta, el("p", "muted", p.source.full_text ? "Full text in the ledger." : "Only the abstract was read."));
    }
    if (doi) {
      const a = el("a", undefined, `doi.org/${doi}`);
      a.href = `https://doi.org/${doi}`;
      a.target = "_blank";
      a.rel = "noopener";
      pane.append(a);
    }
    if (p.section) pane.append(el("p", "section-name", p.section));
    const q = el("blockquote", "passage");
    q.append(el("span", "ctx", p.before.slice(-320)), el("mark", undefined, p.quote), el("span", "ctx", p.after.slice(0, 320)));
    pane.append(q);
    if (select) show("source");
  }

  async function openImpact(id: string): Promise<void> {
    show("impact");
    panes.impact.replaceChildren(el("p", "muted", "loading…"));
    try {
      panes.impact.replaceChildren(impactView(await rapi.impact(id)));
    } catch (e) {
      panes.impact.replaceChildren(el("p", "error", `Could not load the impact: ${(e as Error).message}`));
    }
  }

  async function openExperiment(id: string, select = true): Promise<void> {
    if (select) show("experiment");
    try {
      panes.experiment.replaceChildren(experimentView(await rapi.experiment(id)));
    } catch (e) {
      panes.experiment.replaceChildren(el("p", "error", `Could not load the experiment: ${(e as Error).message}`));
    }
  }

  function useRefs(name: string | undefined, r: Refs | undefined): void {
    if (!r) return;
    if (r.experiment_id && (name === "experiment_get" || name === "experiment_run")) void openExperiment(r.experiment_id);
    else if (r.impact_id) void openImpact(r.impact_id);
    else if (name === "source_passage" && r.text) openPassage({ doi: r.doi ?? null, quote: r.text, section: r.section ?? "", before: r.context_before ?? "", after: r.context_after ?? "" });
    else if (r.claim_id && name === "claim_get") void openClaim(r.claim_id);
  }

  // --- views
  function claimView(c: ClaimCard): HTMLElement {
    const box = el("div", "claim-view");
    const head = el("div", "claim-head");
    head.append(el("div", "claim-path mono", c.claim.library_path), statusBadge(c.claim.status));
    const big = el("div", "claim-value mono", c.value);
    const facts = el("dl", "facts-list");
    const fact = (k: string, v: string | null) => { if (v) facts.append(el("dt", undefined, k), el("dd", undefined, v)); };
    fact("Species", c.claim.species);
    fact("Conditions", c.claim.conditions);
    fact("Method", c.claim.method);
    fact("Matches the library field", c.claim.mapping);
    fact("Why unverified", c.claim.unverified_reason);
    fact("Library notes", c.claim.library_notes);
    fact("Reviewer notes", c.claim.notes_by_reviewer || null);
    box.append(head, big, facts);
    if (c.sources.length) {
      box.append(el("h4", undefined, "Sources"));
      const ul = el("ul", "sources");
      for (const s of c.sources) {
        const li = el("li");
        const a = el("a", undefined, s.title || s.doi || s.source_id);
        if (s.link) { a.href = s.link; a.target = "_blank"; a.rel = "noopener"; }
        li.append(a, el("span", "muted", ` ${s.year ?? ""}${s.checked ? ` · read: ${s.checked}` : ""}`));
        ul.append(li);
      }
      box.append(ul);
    }
    box.append(el("h4", undefined, "History"));
    const ol = el("ol", "history");
    for (const h of c.history) {
      const li = el("li");
      li.dataset.revision = String(h.revision);
      li.append(el("div", "history-line", `Revision ${h.revision}: ${h.summary}`), el("div", "muted", `${who(h.by)} · ${h.at.slice(0, 16).replace("T", " ")}`));
      if (h.reason) li.append(el("div", "history-reason", h.reason));
      ol.append(li);
    }
    box.append(ol);
    const ask = el("button", "btn small", "Ask about this value");
    ask.addEventListener("click", () => { input.value = `Where does the value of ${c.claim.library_path} come from, and is it right for a robot camera?`; input.focus(); });
    box.append(ask);
    return box;
  }

  function impactView(imp: Impact): HTMLElement {
    const box = el("div", "impact-view");
    box.append(el("h3", "mono", imp.library_path));
    box.append(el("p", undefined, `${num(imp.old_quantity.value as number | null)} → ${num((imp.new_quantity.value ?? null) as number | null)} ${String(imp.old_quantity.unit ?? "")}`));
    box.append(el("h4", undefined, "Read by"));
    const ul = el("ul");
    for (const c of imp.consumers) ul.append(el("li", "mono", c));
    if (!imp.consumers.length) ul.append(el("li", "muted", "No profile field reads this value."));
    box.append(ul);
    box.append(el("h4", undefined, "What changes for the robot"));
    const sl = el("ul");
    for (const s of imp.field_summaries) sl.append(el("li", undefined, s.sentence));
    box.append(sl);
    if (imp.profile_rows.length) {
      const t = el("table", "grid");
      const hr = el("tr");
      for (const h of ["field", "where", "old", "new", "change"]) hr.append(el("th", undefined, h));
      t.append(hr);
      for (const r of imp.profile_rows) {
        const tr = el("tr");
        tr.append(el("td", "mono", `${r.profile}.${r.section}.${r.field}`), el("td", undefined, r.point), el("td", "mono", num(r.old)), el("td", "mono", num(r.new)),
          el("td", "mono", r.delta_pct === null ? "" : !isFinite(r.delta_pct) ? "new" : `${r.delta_pct > 0 ? "+" : ""}${r.delta_pct.toFixed(0)}%`));
        t.append(tr);
      }
      box.append(t);
    }
    const world = el("div", `world-row ${imp.world_reached ? "reached" : "not-reached"}`);
    world.append(el("strong", undefined, "World model: "), el("span", undefined, imp.world_note));
    box.append(world);
    const moved = imp.reflex_rows.filter((r) => r.theta_old !== r.theta_new);
    box.append(el("p", "muted", moved.length ? moved.map((r) => `${r.arm}: reflex threshold θ ${r.theta_old.toFixed(2)} → ${r.theta_new.toFixed(2)}`).join("; ") : "The reflex threshold does not change."));
    return box;
  }

  function experimentView(x: ExperimentView): HTMLElement {
    const box = el("div", "experiment-view");
    const e = x.experiment;
    box.append(el("h3", undefined, `Recordings: ${e.sequences.join(", ")}`));
    if (e.status !== "done") {
      const msg = e.status === "failed" ? `The run failed: ${e.error ?? ""}` : (x.progress?.message ?? "running…");
      box.append(el("p", e.status === "failed" ? "error" : "muted", msg));
      if (e.status !== "failed") {
        const bar = el("div", "bar");
        const fill = el("i");
        fill.style.width = `${Math.round(100 * (x.progress?.frac ?? 0))}%`;
        bar.append(fill);
        box.append(bar);
      }
      return box;
    }
    const t = el("table", "grid experiment-table");
    const hr = el("tr");
    for (const h of ["arm", "objects", "hit rate (before → after)", "chance (luck alone)", "median delay", "false fires / h", "p99 latency"]) hr.append(el("th", undefined, h));
    t.append(hr);
    const two = (a: number | null, b: number | null, f: (v: number | null) => string) => `${f(a)} → ${f(b)}`;
    for (const r of x.comparison) {
      const tr = el("tr");
      tr.dataset.arm = r.arm;
      if (!r.judged) {
        tr.append(el("td", "mono", r.arm), el("td", "mono", String(r.objects ?? 0)), el("td", "too-few", "too few objects to judge"), el("td"), el("td"), el("td"), el("td"));
      } else {
        tr.append(
          el("td", "mono", r.arm),
          el("td", "mono", String(r.objects)),
          el("td", "mono", two(r.baseline_hit_rate, r.patched_hit_rate, pct)),
          el("td", "mono chance", two(r.baseline_chance_hit_rate, r.patched_chance_hit_rate, pct)),
          el("td", "mono", two(r.baseline_median_delay_s, r.patched_median_delay_s, (v) => (v === null ? "—" : `${v.toFixed(2)} s`))),
          el("td", "mono", two(r.baseline_fires_per_h, r.patched_fires_per_h, (v) => (v === null ? "—" : v.toFixed(0)))),
          el("td", "mono", two(r.baseline_latency_p99_ms, r.patched_latency_p99_ms, (v) => (v === null ? "—" : `${v.toFixed(1)} ms`))),
        );
      }
      t.append(tr);
    }
    box.append(t);
    box.append(el("p", "muted", "A hit rate means something only if it is clearly above its chance rate: what the same amount of firing would hit by luck."));
    if (x.runlogs.length) {
      const b = el("button", "btn small", "Open the run in Compare");
      b.addEventListener("click", async () => {
        b.disabled = true;
        try {
          const { run_ids } = await rapi.openRun(e.id);
          if (run_ids.length) location.href = `/compare/${encodeURIComponent(run_ids[0])}`;
          else b.textContent = "No run log for this experiment";
        } catch (err) {
          b.textContent = `Could not open: ${(err as Error).message}`;
        }
      });
      box.append(b);
    }
    return box;
  }

  // --- chat items
  const chips = new Map<string, HTMLElement>();
  function addUser(text: string): void {
    log.append(el("div", "msg user", text));
  }
  function addNote(text: string): void {
    log.append(el("div", "msg note", text));
  }
  function addThinking(text: string): HTMLElement {
    const d = el("details", "thinking");
    d.append(el("summary", undefined, "How the assistant is thinking"), el("p", undefined, text));
    log.append(d);
    return d;
  }
  function addChip(id: string, label: string): void {
    const c = el("details", "tool-chip");
    c.append(el("summary", undefined, label));
    c.dataset.state = "running";
    chips.set(id, c);
    log.append(c);
  }
  function finishChip(id: string, ok: boolean, summary: string): void {
    const c = chips.get(id);
    if (!c) return;
    c.dataset.state = ok ? "ok" : "problem";
    c.append(el("p", ok ? "muted" : "error", summary));
  }
  function addCard(a: Action): void {
    const box = el("article", "action-card panel");
    box.dataset.action = a.id;
    box.dataset.status = a.status;
    box.append(el("h3", undefined, a.card.title ?? "Proposed change"));
    if (a.card.summary) box.append(el("p", undefined, a.card.summary));
    if (a.card.rows?.length) {
      const t = el("table", "grid");
      for (const r of a.card.rows) {
        const tr = el("tr");
        tr.append(el("th", undefined, r.label), el("td", "mono before", r.before || "—"), el("td", "arrow", "→"), el("td", "mono after", r.after));
        t.append(tr);
      }
      box.append(t);
    }
    if (a.card.quote) box.append(el("blockquote", "passage", `“${a.card.quote}”`));
    if (a.card.diff) {
      const d = el("details");
      d.append(el("summary", undefined, "The exact change to the library file"), el("pre", "diff", a.card.diff));
      box.append(d);
    }
    if (a.card.note) box.append(el("p", "muted", a.card.note));
    const status = el("p", "card-status");
    if (a.status !== "pending") {
      status.textContent = a.status === "approved" ? "Approved." : a.status === "rejected" ? "Rejected." : `Could not be applied: ${a.error ?? ""}`;
      box.append(status);
      log.append(box);
      return;
    }
    const why = el("textarea", "rationale");
    why.rows = 2;
    why.placeholder = "Your reason, in a sentence (required)";
    why.setAttribute("aria-label", "Your reason");
    const approve = el("button", "btn primary", "Approve");
    const reject = el("button", "btn", "Reject");
    const btns = el("div", "card-actions");
    btns.append(approve, reject);
    const decide = async (d: "approve" | "reject") => {
      const r = why.value.trim();
      if (r.split(/\s+/).length < 3) {
        status.textContent = "Please give your reason in a full sentence first.";
        why.focus();
        return;
      }
      approve.disabled = reject.disabled = why.disabled = true;
      status.textContent = d === "approve" ? "Applying…" : "Recording…";
      try {
        const out = await rapi.decide(a.id, d, r);
        box.dataset.status = out.action.status;
        status.textContent = out.message;
        btns.remove();
        why.remove();
        if (out.action.kind === "claim_revision" && typeof out.action.result?.claim_id === "string") void openClaim(out.action.result.claim_id);
      } catch (e) {
        approve.disabled = reject.disabled = why.disabled = false;
        status.textContent = `Not recorded: ${(e as Error).message}`;
      }
    };
    approve.addEventListener("click", () => void decide("approve"));
    reject.addEventListener("click", () => void decide("reject"));
    box.append(why, btns, status);
    log.append(box);
  }

  // --- start screen
  async function renderStart(): Promise<void> {
    start.replaceChildren();
    const head = el("div", "start-tabs");
    const b1 = el("button", "chip", "Open questions");
    const b2 = el("button", "chip", "Least-verified values Panther depends on");
    head.append(b1, b2);
    const list = el("div", "start-list");
    start.append(el("p", "muted", "Pick something to look at, or just ask."), head, list);
    let data: Awaited<ReturnType<typeof rapi.start>>;
    try {
      data = await rapi.start();
    } catch (e) {
      list.append(el("p", "error", `The research server is not answering: ${(e as Error).message}`));
      return;
    }
    const showQuestions = () => {
      b1.setAttribute("aria-pressed", "true");
      b2.setAttribute("aria-pressed", "false");
      list.replaceChildren();
      for (const q of data.open_questions.slice(0, 12)) {
        const row = el("button", "start-row");
        row.append(el("span", `badge ${q.priority === "critical" ? "alert" : q.priority === "high" ? "warn" : ""}`, q.priority), el("span", undefined, q.statement));
        row.addEventListener("click", () => { input.value = `Let's look at this open question: ${q.statement}`; input.focus(); });
        list.append(row);
      }
    };
    const showLeast = () => {
      b2.setAttribute("aria-pressed", "true");
      b1.setAttribute("aria-pressed", "false");
      list.replaceChildren();
      for (const c of data.least_verified) {
        const row = el("button", "start-row");
        row.dataset.claim = c.claim_id;
        row.append(statusBadge(c.status), el("span", "mono", c.library_path), el("span", "mono value", c.value));
        if (c.read_by.length) row.title = `Read by ${c.read_by.join(", ")}`;
        row.addEventListener("click", () => void openClaim(c.claim_id));
        list.append(row);
      }
    };
    b1.addEventListener("click", showQuestions);
    b2.addEventListener("click", showLeast);
    showQuestions();
  }

  // --- conversation
  async function load(): Promise<void> {
    log.replaceChildren();
    chips.clear();
    if (!conv) {
      start.hidden = false;
      return;
    }
    try {
      const c = await rapi.conversation(conv);
      const names = new Map<string, string>();
      for (const it of c.items) {
        if (it.kind === "user" && it.text) addUser(it.text);
        else if (it.kind === "assistant" && it.text) log.append(Object.assign(prose(it.text), { className: "msg assistant prose" }));
        else if (it.kind === "thinking" && it.text) addThinking(it.text);
        else if (it.kind === "note" && it.text) addNote(it.text);
        else if (it.kind === "tool" && it.id) { names.set(it.id, it.name ?? ""); addChip(it.id, it.label ?? it.name ?? "tool"); }
        else if (it.kind === "tool_result" && it.id) finishChip(it.id, !!it.ok, it.ok ? "Done." : "There was a problem with this step.");
        else if (it.kind === "card" && it.action) addCard(it.action);
      }
      start.hidden = c.items.length > 0;
    } catch {
      conv = "";
      start.hidden = false;
    }
  }

  newBtn.addEventListener("click", () => {
    conv = "";
    const u = new URL(location.href);
    u.searchParams.delete("c");
    history.replaceState(null, "", u);
    void load();
  });

  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const text = input.value.trim();
    if (!text || busy) return;
    busy = true;
    sendBtn.disabled = true;
    sendBtn.textContent = "Working…";
    try {
      if (!conv) setConv((await rapi.newConversation()).id);
      start.hidden = true;
      addUser(text);
      input.value = "";
      let current: HTMLElement | null = null;
      let currentText = "";
      let thinking: HTMLElement | null = null;
      const names = new Map<string, string>();
      await send(conv, text, focus, (event, d) => {
        if (event === "text_delta") {
          if (!current) { current = el("div", "msg assistant prose"); log.append(current); currentText = ""; }
          currentText += d.text;
          current.replaceChildren(...prose(currentText).childNodes);
          thinking = null;
        } else if (event === "thinking_summary") {
          if (!thinking) thinking = addThinking("");
          const p = thinking.querySelector("p");
          if (p) p.textContent += d.text;
        } else if (event === "tool_call") {
          current = null;
          thinking = null;
          names.set(d.id, d.name);
          addChip(d.id, d.label);
        } else if (event === "tool_result_summary") {
          finishChip(d.id, d.ok, d.summary);
          useRefs(names.get(d.id), d.refs);
        } else if (event === "pending_action") {
          addCard(d.action);
        } else if (event === "job_progress") {
          const id = String(d.job_id ?? "");
          let p = log.querySelector<HTMLElement>(`.job[data-job="${CSS.escape(id)}"]`);
          if (!p) { p = el("div", "msg job"); p.dataset.job = id; log.append(p); }
          p.textContent = `${d.message ?? "running"}${typeof d.frac === "number" ? ` (${Math.round(d.frac * 100)}%)` : ""}`;
          if (id) void openExperiment(id, false);
        } else if (event === "error") {
          log.append(el("div", "msg note warn-note", d.message));
        }
        log.scrollTop = log.scrollHeight;
      });
    } catch (e) {
      log.append(el("div", "msg note warn-note", `Could not send: ${(e as Error).message}`));
    } finally {
      busy = false;
      sendBtn.disabled = false;
      sendBtn.textContent = "Send";
    }
  });
  input.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter" && (ev.metaKey || ev.ctrlKey)) form.requestSubmit();
  });

  await renderStart();
  await load();
  if (params.get("claim")) void openClaim(params.get("claim")!);
}
