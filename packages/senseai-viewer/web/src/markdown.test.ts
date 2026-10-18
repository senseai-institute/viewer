// Copyright 2026 SenseAI Institute
// SPDX-License-Identifier: Apache-2.0
import { test } from "node:test";
import assert from "node:assert/strict";
import { inline, parse } from "./markdown.ts";

test("inline marks", () => {
  assert.deepEqual(inline("a **b** `c` *d* <x>"), [
    { t: "text", s: "a " }, { t: "b", s: "b" }, { t: "text", s: " " }, { t: "code", s: "c" },
    { t: "text", s: " " }, { t: "i", s: "d" }, { t: "text", s: " <x>" },
  ]);
});

test("bench report blocks", () => {
  const md = [
    "# Replay comparison (DSEC)",
    "",
    "> **AGREEMENT MODE: no labels.**",
    "",
    "panther vs yolo",
    "on 1 sequence.",
    "",
    "| arm | hit |",
    "|---|---|",
    "| panther | 1.00 |",
    "| yolo | 0.50 |",
    "",
    "- **panther**: {...}",
    "  - SDK stages: world p99 1.5 ms",
    "- **yolo**: {...}",
  ].join("\n");
  const b = parse(md);
  assert.deepEqual(b.map((x) => x.k), ["h", "quote", "p", "table", "ul"]);
  assert.equal(b[2].k === "p" && b[2].text[0].s, "panther vs yolo on 1 sequence.");
  const t = b[3];
  assert.ok(t.k === "table" && t.rows.length === 2 && t.rows[1][1][0].s === "0.50");
  const ul = b[4];
  assert.ok(ul.k === "ul" && ul.items.length === 2 && ul.items[0].sub.length === 1);
});
