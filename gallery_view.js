// Diagram Gallery — browser side of the `diagram_gallery` Streamlit v2 component.
// Renders Mermaid and draw.io thumbnails lazily and reports double-clicks to Python.

const DRAWIO_VIEWER_URL = "https://viewer.diagrams.net/js/viewer-static.min.js";
const MERMAID_URL = "https://cdn.jsdelivr.net/npm/mermaid@10/dist/mermaid.esm.min.mjs";
const RENDER_TIMEOUT_MS = 15000;
const SIZES = { S: 170, M: 250, L: 380 };

// ============================================================
// LIBRARY LOADERS (shared across re-mounts via window)
// ============================================================
function loadDrawioViewer() {
  if (window.GraphViewer) return Promise.resolve();
  if (!window.__dgViewerPromise) {
    window.__dgViewerPromise = new Promise((resolve, reject) => {
      // Defining this hook also stops the viewer from auto-processing the whole page
      window.onDrawioViewerLoad = () => resolve();
      const script = document.createElement("script");
      script.src = DRAWIO_VIEWER_URL;
      script.onerror = () => {
        window.__dgViewerPromise = null;
        script.remove();
        reject(new Error("Could not load the draw.io viewer (offline?)"));
      };
      document.head.appendChild(script);
    });
  }
  return window.__dgViewerPromise;
}

function loadMermaid() {
  if (!window.__dgMermaidPromise) {
    window.__dgMermaidPromise = import(MERMAID_URL)
      .then((mod) => {
        mod.default.initialize({ startOnLoad: false, securityLevel: "strict", theme: "default" });
        return mod.default;
      })
      .catch(() => {
        window.__dgMermaidPromise = null;
        throw new Error("Could not load Mermaid (offline?)");
      });
  }
  return window.__dgMermaidPromise;
}

function withTimeout(promise) {
  return Promise.race([
    promise,
    new Promise((_, reject) => setTimeout(() => reject(new Error("Rendering timed out")), RENDER_TIMEOUT_MS)),
  ]);
}

// ============================================================
// THUMBNAIL RENDERERS
// ============================================================
let renderSeq = 0;

async function renderMermaid(item, box) {
  const mermaid = await loadMermaid();
  const id = `dg-mmd-${Date.now().toString(36)}-${++renderSeq}`;
  try {
    const { svg } = await mermaid.render(id, item.source);
    box.innerHTML = svg;
  } finally {
    document.getElementById(`d${id}`)?.remove(); // scratch container Mermaid leaves behind on errors
  }
  const el = box.querySelector("svg");
  el.removeAttribute("height");
  el.style.maxWidth = "100%";
  el.style.width = "100%";
  el.style.height = "100%";
}

async function renderDrawio(item, box) {
  await loadDrawioViewer();
  const host = document.createElement("div");
  host.className = "dg-drawio-host";
  host.setAttribute("data-mxgraph", JSON.stringify({
    xml: item.source, page: 0, nav: false, lightbox: false, resize: false, border: 0,
    "check-visible-state": false,
  }));
  box.replaceChildren(host);
  await new Promise((resolve, reject) => {
    try {
      window.GraphViewer.createViewerForElement(host, resolve);
    } catch (err) {
      reject(err);
    }
  });
  fitSvgToBox(host);
}

// The viewer draws at 100 % zoom; crop to the drawing and scale it into the card instead.
function fitSvgToBox(host) {
  const svg = host.querySelector("svg");
  if (!svg) throw new Error("Nothing to draw");
  const bb = svg.getBBox();
  if (!bb.width || !bb.height) throw new Error("Empty diagram");
  const pad = Math.max(bb.width, bb.height) * 0.03;
  svg.setAttribute("viewBox", `${bb.x - pad} ${bb.y - pad} ${bb.width + 2 * pad} ${bb.height + 2 * pad}`);
  svg.setAttribute("preserveAspectRatio", "xMidYMid meet");
  for (let el = svg; el && el !== host.parentElement; el = el.parentElement) {
    Object.assign(el.style, {
      width: "100%", height: "100%", minWidth: "0", minHeight: "0",
      maxWidth: "none", maxHeight: "none", position: "static", left: "", top: "",
    });
  }
}

function showMessage(box, text, isError = true) {
  const msg = document.createElement("div");
  msg.className = isError ? "dg-msg dg-msg-error" : "dg-msg";
  msg.textContent = text;
  box.replaceChildren(msg);
}

// ============================================================
// GALLERY
// ============================================================
function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
}

function formatDate(mtime) {
  return new Date(mtime * 1000).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

function buildCard(state, item) {
  const card = el("div", `dg-card dg-kind-${item.kind}`);
  card.tabIndex = 0;
  card.title = `${item.path}${item.block ? ` (block ${item.block})` : ""}\nDouble-click to open in draw.io`;

  card.appendChild(el("div", "dg-thumb"));

  const body = el("div", "dg-body");
  body.appendChild(el("div", "dg-name", item.block ? `${item.name} #${item.block}` : item.name));
  const sub = el("div", "dg-sub");
  const kindLabel = item.kind === "drawio" ? item.subtype : `Mermaid · ${item.subtype}`;
  sub.appendChild(el("span", `dg-badge dg-badge-${item.kind}`, kindLabel));
  sub.appendChild(el("span", "dg-path", item.dir ? `📁 ${item.dir}` : "📁 ."));
  body.appendChild(sub);

  if (item.labels.length) {
    const chips = el("div", "dg-chips");
    item.labels.forEach((label) => chips.appendChild(el("span", "dg-chip", label)));
    if (item.label_count > item.labels.length) {
      chips.appendChild(el("span", "dg-chip dg-chip-more", `+${item.label_count - item.labels.length}`));
    }
    body.appendChild(chips);
  }

  const meta = [formatDate(item.mtime)];
  if (item.kind === "drawio" && item.pages?.length) {
    meta.push(item.pages.length > 1 ? `${item.pages.length} pages` : item.pages[0]);
  }
  if (item.vertices) meta.push(`${item.vertices} shapes`);
  body.appendChild(el("div", "dg-meta", meta.join(" · ")));
  if (item.kind === "drawio" && item.pages?.length > 1) {
    card.title += `\nPages: ${item.pages.join(", ")}`;
  }
  card.appendChild(body);

  const open = () => {
    card.classList.add("dg-opening");
    setTimeout(() => card.classList.remove("dg-opening"), 900);
    state.setTriggerValue("open", { path: item.path, block: item.block, t: Date.now() });
  };
  card.addEventListener("dblclick", open);
  card.addEventListener("keydown", (e) => {
    if (e.key === "Enter") open();
  });

  card.__item = item;
  card.__haystack = [item.name, item.dir, item.subtype, item.search, ...(item.pages || [])]
    .join(" ")
    .toLowerCase();
  return card;
}

function renderThumb(state, card) {
  const item = card.__item;
  const box = card.querySelector(".dg-thumb");
  card.dataset.rendered = "1";

  if (item.image) {
    const img = el("img");
    img.alt = item.name;
    img.src = item.image;
    box.replaceChildren(img);
    return;
  }
  if (!item.source) {
    showMessage(box, item.error || "No preview available");
    return;
  }

  box.classList.add("dg-loading");
  state.queue = state.queue.then(async () => {
    if (!card.isConnected) {
      delete card.dataset.rendered; // filtered out before its turn — retry when visible again
      box.classList.remove("dg-loading");
      return;
    }
    try {
      await withTimeout(item.kind === "drawio" ? renderDrawio(item, box) : renderMermaid(item, box));
    } catch (err) {
      // Prefer the parser's explanation from Python; else the first line of the renderer's message
      const reason = item.error || String(err?.message || err).split("\n")[0].replace(/:\s*$/, "");
      showMessage(box, `⚠️ ${reason.slice(0, 160)}`);
    } finally {
      box.classList.remove("dg-loading");
    }
  });
}

function layout(state) {
  const terms = state.query.toLowerCase().split(/\s+/).filter(Boolean);
  const visible = state.cards.filter((card) => {
    const item = card.__item;
    if (state.kind !== "all" && item.kind !== state.kind) return false;
    return terms.every((t) => card.__haystack.includes(t));
  });

  const byName = (a, b) => a.__item.name.localeCompare(b.__item.name, undefined, { numeric: true });
  if (state.sort === "newest") visible.sort((a, b) => b.__item.mtime - a.__item.mtime);
  else visible.sort(byName);

  const groups = new Map();
  if (state.sort === "folder") {
    visible.forEach((card) => {
      const dir = card.__item.dir;
      if (!groups.has(dir)) groups.set(dir, []);
      groups.get(dir).push(card);
    });
  } else {
    groups.set(null, visible);
  }

  const sections = [];
  [...groups.keys()]
    .sort((a, b) => (a ?? "").localeCompare(b ?? "", undefined, { numeric: true }))
    .forEach((dir) => {
      const section = el("section", "dg-section");
      if (dir !== null) {
        const header = el("h4", "dg-folder", dir ? `📁 ${dir}` : "📁 (top folder)");
        header.appendChild(el("span", "dg-folder-count", String(groups.get(dir).length)));
        section.appendChild(header);
      }
      const grid = el("div", "dg-grid");
      groups.get(dir).forEach((card) => grid.appendChild(card));
      section.appendChild(grid);
      sections.push(section);
    });

  state.results.replaceChildren(...sections);
  state.count.textContent = `${visible.length} of ${state.cards.length} diagrams`;
  if (!visible.length) state.results.appendChild(el("div", "dg-empty", "No diagram matches the filter."));
  observe(state);
}

function observe(state) {
  state.observer?.disconnect();
  state.observer = new IntersectionObserver((entries) => {
    entries.forEach((entry) => {
      if (entry.isIntersecting && !entry.target.dataset.rendered) {
        state.observer.unobserve(entry.target);
        renderThumb(state, entry.target);
      }
    });
  }, { rootMargin: "400px 0px" });
  state.cards.forEach((card) => {
    if (!card.dataset.rendered && card.isConnected) state.observer.observe(card);
  });
}

function buildToolbar(state, data) {
  const bar = el("div", "dg-toolbar");

  const search = el("input", "dg-search");
  search.type = "search";
  search.placeholder = "🔍 Filter by file name, folder or text inside the diagram…";
  search.value = state.query;
  search.addEventListener("input", () => {
    state.query = search.value;
    layout(state);
  });
  bar.appendChild(search);

  const segmented = (options, current, onChange) => {
    const group = el("div", "dg-seg");
    options.forEach(([value, label]) => {
      const btn = el("button", value === current ? "dg-active" : "", label);
      btn.type = "button";
      btn.addEventListener("click", () => {
        group.querySelectorAll("button").forEach((b) => b.classList.remove("dg-active"));
        btn.classList.add("dg-active");
        onChange(value);
      });
      group.appendChild(btn);
    });
    return group;
  };

  bar.appendChild(segmented([["all", "All"], ["mermaid", "Mermaid"], ["drawio", "draw.io"]], state.kind, (v) => {
    state.kind = v;
    layout(state);
  }));
  bar.appendChild(segmented([["folder", "By folder"], ["name", "A–Z"], ["newest", "Newest"]], state.sort, (v) => {
    state.sort = v;
    layout(state);
  }));
  bar.appendChild(segmented(Object.keys(SIZES).map((k) => [k, k]), state.size, (v) => {
    state.size = v;
    state.root.style.setProperty("--dg-card-w", `${SIZES[v]}px`);
  }));

  state.count = el("span", "dg-count");
  bar.appendChild(state.count);

  const hint = el("div", "dg-hint",
    data.hasDrawio
      ? "Double-click a card (or press Enter) to open it in the draw.io app — Mermaid becomes editable draw.io shapes."
      : "⚠️ draw.io desktop app not found — install it from drawio.com to open diagrams with a double-click.");
  return [bar, hint];
}

export default function (component) {
  const { data, parentElement, setTriggerValue } = component;
  let root = parentElement.querySelector(":scope > .dg-root");

  // Streamlit re-invokes us on every rerun; keep the rendered thumbnails when nothing changed
  if (root?.__dg && root.__dg.sig === data.sig) {
    root.__dg.setTriggerValue = setTriggerValue;
    observe(root.__dg);
    return () => root.__dg.observer?.disconnect();
  }

  const previous = root?.__dg;
  previous?.observer?.disconnect();
  root?.remove();

  root = el("div", "dg-root");
  const state = {
    sig: data.sig,
    root,
    setTriggerValue,
    queue: Promise.resolve(),
    query: previous?.query ?? "",
    kind: previous?.kind ?? "all",
    sort: previous?.sort ?? "folder",
    size: previous?.size ?? "M",
  };
  root.__dg = state;
  root.style.setProperty("--dg-card-w", `${SIZES[state.size]}px`);

  root.append(...buildToolbar(state, data));
  state.results = el("div", "dg-results");
  root.appendChild(state.results);
  state.cards = data.items.map((item) => buildCard(state, item));
  parentElement.appendChild(root);

  layout(state);
  return () => state.observer?.disconnect();
}
