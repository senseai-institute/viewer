// Copyright 2026 SenseAI Institute
// SPDX-License-Identifier: Apache-2.0
// Hash router: #/ (list), #/r/<id>?t=<tick> (recording), #/compare?a=<id>:<sid>&b=… (compare).

import "uplot/dist/uPlot.min.css";
import "./style.css";
import { h } from "./format";
import { renderCompare } from "./pages/compare";
import { renderList } from "./pages/list";
import { renderRecording } from "./pages/recording";

const app = document.getElementById("app") as HTMLElement;
let cleanup: (() => void) | null = null;
let generation = 0;

async function route(): Promise<void> {
  const gen = ++generation;
  cleanup?.();
  cleanup = null;
  const raw = location.hash.replace(/^#/, "") || "/";
  const [path = "/", query = ""] = raw.split("?");
  const params = new URLSearchParams(query);
  const parts = path.split("/").filter(Boolean);
  document.querySelectorAll<HTMLAnchorElement>("[data-nav]").forEach((a) => {
    a.classList.toggle("active", a.dataset.nav === (parts[0] === "compare" ? "compare" : "list"));
  });
  try {
    let done: (() => void) | null = null;
    if (parts[0] === "r" && parts[1]) {
      const t = params.get("t");
      done = await renderRecording(app, decodeURIComponent(parts[1]), t === null ? null : Number(t));
      document.title = `${decodeURIComponent(parts[1])} · SenseAI Viewer`;
    } else if (parts[0] === "compare") {
      done = await renderCompare(app, params);
      document.title = "Compare · SenseAI Viewer";
    } else {
      await renderList(app);
      document.title = "SenseAI Viewer";
    }
    if (gen !== generation) done?.();
    else cleanup = done;
  } catch (err) {
    console.error(err);
    app.replaceChildren(h("div", { class: "card error" }, h("h1", {}, "Something went wrong"), h("p", {}, String(err))));
  }
}

let lastPath = "";
window.addEventListener("hashchange", () => {
  // The recording page rewrites ?t= as you scrub; only re-render when the page itself changes.
  const path = location.hash.split("?")[0] ?? "";
  if (path === lastPath && path.startsWith("#/r/")) return;
  lastPath = path;
  void route();
});
lastPath = location.hash.split("?")[0] ?? "";
void route();
