// Copyright 2026 SenseAI Institute
// SPDX-License-Identifier: Apache-2.0
//
// The Markdown bench writes (replay_report.md): headings, paragraphs, "> " notes, "- " lists
// (one nesting level), pipe tables, **bold**, *italic* and `code`. Parsed into a small tree
// first (testable without a DOM), then built with textContent only, so a report can never
// inject HTML.

export type Inline = { t: "text" | "b" | "i" | "code"; s: string };
export type Block =
  | { k: "h"; level: number; text: Inline[] }
  | { k: "p"; text: Inline[] }
  | { k: "quote"; text: Inline[] }
  | { k: "ul"; items: { text: Inline[]; sub: Inline[][] }[] }
  | { k: "table"; head: Inline[][]; rows: Inline[][][] };

export function inline(s: string): Inline[] {
  const out: Inline[] = [];
  const re = /\*\*(.+?)\*\*|`([^`]+)`|\*([^*]+)\*/g;
  let last = 0;
  for (let m = re.exec(s); m; m = re.exec(s)) {
    if (m.index > last) out.push({ t: "text", s: s.slice(last, m.index) });
    if (m[1] !== undefined) out.push({ t: "b", s: m[1] });
    else if (m[2] !== undefined) out.push({ t: "code", s: m[2] });
    else out.push({ t: "i", s: m[3] });
    last = m.index + m[0].length;
  }
  if (last < s.length) out.push({ t: "text", s: s.slice(last) });
  return out;
}

const cells = (line: string): string[] => line.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((c) => c.trim());
const isRule = (line: string): boolean => /^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$/.test(line);

export function parse(md: string): Block[] {
  const lines = md.replace(/\r\n/g, "\n").split("\n");
  const out: Block[] = [];
  let para: string[] = [];
  const flush = () => {
    if (para.length) out.push({ k: "p", text: inline(para.join(" ")) });
    para = [];
  };
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    const h = /^(#{1,6})\s+(.*)$/.exec(line);
    if (!line.trim()) {
      flush();
    } else if (h) {
      flush();
      out.push({ k: "h", level: h[1].length, text: inline(h[2]) });
    } else if (line.startsWith(">")) {
      flush();
      out.push({ k: "quote", text: inline(line.replace(/^>\s?/, "")) });
    } else if (line.trim().startsWith("|") && i + 1 < lines.length && isRule(lines[i + 1])) {
      flush();
      const head = cells(line).map(inline);
      const rows: Inline[][][] = [];
      i += 2;
      while (i < lines.length && lines[i].trim().startsWith("|")) rows.push(cells(lines[i++]).map(inline));
      i--;
      out.push({ k: "table", head, rows });
    } else if (/^- /.test(line)) {
      flush();
      const items: { text: Inline[]; sub: Inline[][] }[] = [];
      while (i < lines.length && /^\s*- /.test(lines[i])) {
        const l = lines[i];
        if (/^\s+- /.test(l) && items.length) items[items.length - 1].sub.push(inline(l.replace(/^\s+- /, "")));
        else items.push({ text: inline(l.replace(/^- /, "")), sub: [] });
        i++;
      }
      i--;
      out.push({ k: "ul", items });
    } else {
      para.push(line.trim());
    }
  }
  flush();
  return out;
}

function inl(parent: HTMLElement, xs: Inline[]): void {
  for (const x of xs) {
    if (x.t === "text") parent.append(document.createTextNode(x.s));
    else {
      const e = document.createElement(x.t === "b" ? "strong" : x.t === "i" ? "em" : "code");
      e.textContent = x.s;
      parent.append(e);
    }
  }
}

/** Render Markdown into a <div class="md">. */
export function render(md: string): HTMLElement {
  const root = document.createElement("div");
  root.className = "md";
  for (const b of parse(md)) {
    if (b.k === "h") {
      const e = document.createElement(`h${Math.min(6, b.level + 1)}`);
      inl(e, b.text);
      root.append(e);
    } else if (b.k === "p" || b.k === "quote") {
      const e = document.createElement(b.k === "p" ? "p" : "blockquote");
      inl(e, b.text);
      root.append(e);
    } else if (b.k === "ul") {
      const ul = document.createElement("ul");
      for (const it of b.items) {
        const li = document.createElement("li");
        inl(li, it.text);
        if (it.sub.length) {
          const sub = document.createElement("ul");
          for (const s of it.sub) {
            const sl = document.createElement("li");
            inl(sl, s);
            sub.append(sl);
          }
          li.append(sub);
        }
        ul.append(li);
      }
      root.append(ul);
    } else {
      const wrap = document.createElement("div");
      wrap.className = "md-table";
      const t = document.createElement("table");
      t.className = "grid";
      const tr = document.createElement("tr");
      for (const c of b.head) {
        const th = document.createElement("th");
        inl(th, c);
        tr.append(th);
      }
      const thead = document.createElement("thead");
      thead.append(tr);
      const tb = document.createElement("tbody");
      for (const r of b.rows) {
        const row = document.createElement("tr");
        for (const c of r) {
          const td = document.createElement("td");
          inl(td, c);
          row.append(td);
        }
        tb.append(row);
      }
      t.append(thead, tb);
      wrap.append(t);
      root.append(wrap);
    }
  }
  return root;
}
