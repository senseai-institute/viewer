// Copyright 2026 SenseAI Institute
// SPDX-License-Identifier: Apache-2.0
//
// One colour per contestant, the same on every page.

const COLORS: Record<string, string> = {
  panther_prior: "#5EEAD4",
  panther_vision: "#2DD4BF",
  panther_fused: "#99F6E4",
  panther: "#14B8A6",
  null_profile: "#A9B4CA",
  torchvision: "#F59E0B",
  yolo: "#C084FC",
};
const SPARE = ["#60A5FA", "#F472B6", "#A3E635", "#FB7185"];

export function armColor(name: string): string {
  if (COLORS[name]) return COLORS[name];
  let h = 0;
  for (const c of name) h = (h * 31 + c.charCodeAt(0)) >>> 0;
  return SPARE[h % SPARE.length];
}

export function isPanther(name: string): boolean {
  return name.startsWith("panther");
}

/** Driving status from a reflex action, in the words the status pill uses. */
export function statusOf(action: string | undefined): { text: string; cls: string } {
  if (action === "slow") return { text: "Slowing", cls: "warn" };
  if (action === "brake" || action === "hold") return { text: "Braking", cls: "alert" };
  if (action === "swerve") return { text: "Steering away", cls: "warn" };
  return { text: "Driving normally", cls: "ok" };
}
