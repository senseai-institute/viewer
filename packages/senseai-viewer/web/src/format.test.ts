// Copyright 2026 SenseAI Institute
// SPDX-License-Identifier: Apache-2.0
import { test } from "node:test";
import assert from "node:assert/strict";
import { better, clock, delay, lead, leadSentence, ms, profileLabel, seconds, signed } from "./format.ts";
import { heat, MIN_VISIBLE, SCALE_MAX } from "./colors.ts";
import { f16ToF32 } from "./api/msgpack.ts";

test("seconds and signed", () => {
  assert.equal(seconds(6.29), "6.29 s");
  assert.equal(seconds(null), "—");
  assert.equal(signed(0.34), "+0.34 s");
  assert.equal(signed(-0.22), "−0.22 s");
  assert.equal(signed(0.001), "±0.00 s");
  assert.equal(ms(11.2487), "11.2 ms");
});

test("clock", () => {
  assert.equal(clock(6.29), "0:06.29");
  assert.equal(clock(75.5), "1:15.50");
});

test("delay and lead", () => {
  assert.equal(delay("miss"), "miss");
  assert.equal(delay(-0.22), "−0.22 s");
  assert.equal(lead(0.34), "+0.34 s");
  assert.equal(lead("+inf"), "B missed");
  assert.equal(leadSentence("Panther", "default", 0.34), "Panther reacts 340 ms earlier than default");
  assert.equal(leadSentence("A", "B", -0.1), "A reacts 100 ms later than B");
  assert.equal(leadSentence("A", "B", null), "needs ground truth");
});

test("better", () => {
  assert.equal(better(0, 1, "lower"), "a");
  assert.equal(better(11.2, 10.8, "lower"), "b");
  assert.equal(better(1, 1, "lower"), null);
  assert.equal(better(null, 1, "higher"), null);
});

test("profile label", () => {
  assert.equal(profileLabel("panther", 0), "Panther v0");
  assert.equal(profileLabel("null", 0), "default");
});

test("heat scale", () => {
  assert.equal(heat(0)[3], 0);
  assert.equal(heat(MIN_VISIBLE)[3], 0);
  const hi = heat(SCALE_MAX * 2);
  assert.deepEqual(hi.slice(0, 3), [0xff, 0x5a, 0x7a]);
  assert.ok(heat(0.2)[3] < hi[3]);
  assert.deepEqual(heat(SCALE_MAX, true).slice(0, 3), [0xfd, 0xe7, 0x25]);
});

test("float16 decode", () => {
  const out = f16ToF32(new Uint16Array([0x0000, 0x3c00, 0xc000, 0x3555, 0x7c00]));
  assert.equal(out[0], 0);
  assert.equal(out[1], 1);
  assert.equal(out[2], -2);
  assert.ok(Math.abs(out[3] - 0.333) < 1e-3);
  assert.equal(out[4], Infinity);
});
