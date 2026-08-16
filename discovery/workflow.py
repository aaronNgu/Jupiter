"""Workflow artifact: schema, frame resolver, and renderers.

The workflow JSON (contract in design-docs/luminque-discovery-p1.md) is the
source of truth for a discovered process. It is produced by the model —
analyze.py emits it with the analysis, visualize.py extracts it from a report
— but everything in this module is deterministic: no model calls.

Shape (see SCHEMA below), plus fields added after parsing:
  run              provenance: tenant_id, agent_id, window, source_report
  steps[].frames   S3-style keys of the screenshots captured during the
                   step's time_ranges — written by resolve_frames(), never
                   by the model. Keys, not URLs: presigned URLs expire.
"""

import html
import json
import re
from datetime import datetime, time, timezone
from pathlib import Path

# --- Contract text shared by the analyze.py reduce prompt and the
# --- visualize.py extraction prompt.

LENSES = {
    "process": """\
Graph the business process captured in the analysis. If the user was consuming
a demonstration of a process (tutorial video, training slides, documentation)
rather than performing it live, graph the DEMONSTRATED workflow itself — its
steps, applications, data, and loops — and note in the summary that it was
observed via a demonstration. Omit idle or non-work periods unless they matter
to the process.""",
    "activity": """\
Graph what the user physically did at the workstation, in chronological order
(including watching videos, browsing, and idle periods).""",
}

SCHEMA = """\
{
  "title": "short name of the overall process",
  "summary": "2-3 sentence description of the workflow",
  "steps": [
    {
      "id": "s1",
      "name": "short imperative step name",
      "app": "application or site used",
      "time_ranges": ["HH:MM:SS-HH:MM:SS"],
      "iterations": 1
    }
  ],
  "edges": [
    {"from": "s1", "to": "s2", "label": "optional trigger/condition", "loop": false}
  ]
}"""

GUIDELINES = """\
Guidelines: ids s1, s2, ... in rough chronological order; 5-15 steps — merge
micro-actions into meaningful process steps; time_ranges are UTC with second
precision (HH:MM:SS), taken from the observed timestamps, one entry per
occurrence of the step; set "iterations" > 1 for repeated steps; use
"loop": true on edges that return to an earlier step; include non-work
periods (breaks, videos) only when they matter to the process; every step
must be reachable from the first."""

JSON_INSTRUCTIONS = f"""\
End your response with exactly one fenced ```json code block converting the
analysis into a workflow graph.

{{lens}}

The JSON object must have this shape:
{SCHEMA}

{GUIDELINES}"""


# --- Parsing model output ----------------------------------------------------

def parse_workflow(text: str) -> dict:
    """Tolerant parse of a JSON-only model response (extraction path)."""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-z]*\s*|\s*```$", "", text, flags=re.S)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise
        return json.loads(text[start:end + 1])


_JSON_FENCE = re.compile(r"```(?:json)?[ \t]*\n(\{.*?\})[ \t\n]*```", re.S)


def split_workflow(text: str) -> tuple[str, dict | None]:
    """Split a prose-report-plus-JSON-block response into (report, workflow).

    Uses the last fenced JSON block that parses and looks like a workflow;
    returns (text, None) when there is none.
    """
    for m in reversed(list(_JSON_FENCE.finditer(text))):
        try:
            wf = json.loads(m.group(1))
        except json.JSONDecodeError:
            continue
        if isinstance(wf, dict) and "steps" in wf:
            return (text[:m.start()] + text[m.end():]).strip(), wf
    return text.strip(), None


# --- Frame resolution --------------------------------------------------------

def frame_time(key: str) -> datetime | None:
    """Capture time encoded in a frame key ({tenant}/{agent}/{date}/{iso}.png)."""
    stem = key.rsplit("/", 1)[-1].removesuffix(".png")
    try:
        ts = datetime.fromisoformat(stem)
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


_RANGE = re.compile(r"(\d{1,2}):(\d{2})(?::(\d{2}))?\s*[-–—]\s*(\d{1,2}):(\d{2})(?::(\d{2}))?")


def _range_bounds(rng: str) -> tuple[time, time] | None:
    m = _RANGE.search(str(rng))
    if not m:
        return None
    sh, sm, ss, eh, em, es = (int(g) if g else None for g in m.groups())
    try:
        # A minute-granular end means "through the end of that minute".
        return time(sh, sm, ss or 0), time(eh, em, 59 if es is None else es)
    except (TypeError, ValueError):
        return None


def resolve_frames(wf: dict, frame_keys: list[str]) -> None:
    """Set steps[].frames to the keys captured inside each step's time_ranges,
    and fill run provenance (tenant/agent/window) from the keys. Deterministic;
    matches on UTC time of day, so keep frame_keys to one window (<= a day)."""
    stamped = sorted((t, k) for k in frame_keys if (t := frame_time(k)))
    for step in wf.get("steps", []):
        hits = set()
        for rng in step.get("time_ranges") or []:
            bounds = _range_bounds(rng)
            if bounds is None or bounds[1] < bounds[0]:  # unparseable or crosses midnight
                continue
            lo, hi = bounds
            hits.update(k for t, k in stamped if lo <= t.time() <= hi)
        step["frames"] = sorted(hits)
    run = wf.setdefault("run", {})
    pairs = {tuple(k.split("/")[:2]) for _, k in stamped if k.count("/") >= 3}
    if len(pairs) == 1:
        tenant, agent = next(iter(pairs))
        run.setdefault("tenant_id", tenant)
        run.setdefault("agent_id", agent)
    if stamped:
        run.setdefault("window", [stamped[0][0].isoformat(), stamped[-1][0].isoformat()])


# --- Renderers ---------------------------------------------------------------

def _label(text: str) -> str:
    return text.replace('"', "'").replace("[", "(").replace("]", ")").replace("|", "/")


def _node_id(raw: str) -> str:
    return re.sub(r"\W", "_", raw)


def to_mermaid(wf: dict) -> str:
    lines = ["flowchart TD"]
    for s in wf.get("steps", []):
        parts = [_label(s.get("name", s["id"]))]
        meta = " · ".join(
            filter(None, [
                _label(s.get("app") or ""),
                ", ".join(s.get("time_ranges") or []),
                f"×{s['iterations']}" if s.get("iterations", 1) > 1 else "",
            ])
        )
        if meta:
            parts.append(f"<i>{meta}</i>")
        lines.append(f'    {_node_id(s["id"])}["{"<br/>".join(parts)}"]')
    for e in wf.get("edges", []):
        arrow = "-.->" if e.get("loop") else "-->"
        label = f'|"{_label(e["label"])}"|' if e.get("label") else ""
        lines.append(f'    {_node_id(e["from"])} {arrow}{label} {_node_id(e["to"])}')
    return "\n".join(lines)


_HTML = """\
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<script src="https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.min.js"></script>
<style>
  * { box-sizing: border-box; }
  body { margin: 0; height: 100vh; display: flex; flex-direction: column;
         font: 14px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
         color: #1f2328; background: #fff; }
  header { padding: 14px 22px; border-bottom: 1px solid #d0d7de; }
  header h1 { margin: 0; font-size: 17px; }
  header p { margin: 6px 0 0; max-width: 90ch; color: #57606a; }
  .meta { margin-top: 6px; font-size: 12px; color: #6e7781; }
  main { flex: 1; min-height: 0; display: flex; }
  #graph { flex: 1; overflow: auto; padding: 18px; }
  .node { cursor: pointer; }
  .node.selected rect { stroke: #2563eb !important; stroke-width: 2.5px !important; }
  #panel { width: 400px; border-left: 1px solid #d0d7de; overflow: auto;
           padding: 16px 18px; background: #f6f8fa; }
  #panel h2 { margin: 0 0 4px; font-size: 15px; }
  #panel .hint { color: #6e7781; }
  #panel .count { margin: 10px 0 8px; font-size: 12px; color: #6e7781; }
  #strip { display: grid; grid-template-columns: repeat(auto-fill, minmax(110px, 1fr)); gap: 8px; }
  #strip img { width: 100%; aspect-ratio: 16/10; object-fit: cover; object-position: top;
               border: 1px solid #d0d7de; border-radius: 4px; cursor: zoom-in; background: #eaeef2; }
  #strip img.missing { opacity: .35; }
  #lightbox { position: fixed; inset: 0; background: rgba(15,18,22,.92); display: flex;
              flex-direction: column; align-items: center; justify-content: center; gap: 10px; }
  #lightbox[hidden] { display: none; }
  #lightbox img { max-width: 92vw; max-height: 84vh; border-radius: 4px; }
  #lightbox .cap { color: #c9d1d9; font-size: 13px; }
  .nav { position: fixed; top: 50%; transform: translateY(-50%); font-size: 34px; color: #c9d1d9;
         background: none; border: none; cursor: pointer; padding: 18px; }
  #prev { left: 8px; }
  #next { right: 8px; }
</style>
</head>
<body>
<header>
  <h1>__TITLE__</h1>
  <p>__SUMMARY__</p>
  <div class="meta">__META__</div>
</header>
<main>
  <div id="graph">Rendering diagram…</div>
  <aside id="panel"><p class="hint">Click a step to see its screenshots.</p></aside>
</main>
<div id="lightbox" hidden>
  <button class="nav" id="prev">&#8249;</button>
  <img id="lbimg" alt="">
  <div class="cap" id="lbcap"></div>
  <button class="nav" id="next">&#8250;</button>
</div>
<script>
const STEPS = __STEPS__;
const MMD = __MMD__;
const FRAME_BASE = __FRAME_BASE__;
const THUMB_CAP = 120;

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const frameUrl = (k) => FRAME_BASE.replace(/\\/+$/, "") + "/" + k;
const frameTs = (k) => (k.split("/").pop() || k).replace(/\\.png$/, "").replace("T", " ");

let current = null, lbIndex = 0, selectedEl = null;

function pick(n, cap) {
  if (n <= cap) return Array.from({length: n}, (_, i) => i);
  const out = [];
  for (let i = 0; i < cap; i++) {
    const j = Math.round(i * (n - 1) / (cap - 1));
    if (!out.length || out[out.length - 1] !== j) out.push(j);
  }
  return out;
}

function select(nodeId, el) {
  current = STEPS[nodeId];
  if (!current) return;
  if (selectedEl) selectedEl.classList.remove("selected");
  if (el) { el.classList.add("selected"); selectedEl = el; }
  const frames = current.frames || [];
  const meta = [current.app, (current.time_ranges || []).join(", "),
                current.iterations > 1 ? "\\u00d7" + current.iterations : ""]
    .filter(Boolean).join(" \\u00b7 ");
  const idxs = pick(frames.length, THUMB_CAP);
  const shown = idxs.length < frames.length ? ` \\u00b7 showing ${idxs.length}` : "";
  $("panel").innerHTML =
    `<h2>${esc(current.name || nodeId)}</h2>` +
    `<div class="meta">${esc(meta)}</div>` +
    `<div class="count">${frames.length} frame${frames.length === 1 ? "" : "s"}${shown}</div>` +
    (frames.length ? `<div id="strip"></div>` : `<p class="hint">No frames linked to this step.</p>`);
  const strip = $("strip");
  if (!strip) return;
  for (const i of idxs) {
    const img = document.createElement("img");
    img.src = frameUrl(frames[i]);
    img.loading = "lazy";
    img.title = frameTs(frames[i]);
    img.onerror = () => img.classList.add("missing");
    img.onclick = () => openLightbox(i);
    strip.appendChild(img);
  }
}

function openLightbox(i) { lbIndex = i; renderLightbox(); $("lightbox").hidden = false; }
function renderLightbox() {
  const k = current.frames[lbIndex];
  $("lbimg").src = frameUrl(k);
  $("lbcap").textContent = `${lbIndex + 1}/${current.frames.length} \\u00b7 ${frameTs(k)}`;
}
function stepBy(d) {
  if (!current) return;
  lbIndex = Math.min(Math.max(lbIndex + d, 0), current.frames.length - 1);
  renderLightbox();
}

$("prev").onclick = (e) => { e.stopPropagation(); stepBy(-1); };
$("next").onclick = (e) => { e.stopPropagation(); stepBy(1); };
$("lightbox").onclick = (e) => { if (e.target.id !== "lbimg") $("lightbox").hidden = true; };
document.addEventListener("keydown", (e) => {
  if ($("lightbox").hidden) return;
  if (e.key === "Escape") $("lightbox").hidden = true;
  else if (e.key === "ArrowLeft") stepBy(-1);
  else if (e.key === "ArrowRight") stepBy(1);
});

mermaid.initialize({ startOnLoad: false, theme: "neutral" });
mermaid.render("wf-svg", MMD).then(({ svg }) => {
  $("graph").innerHTML = svg;
  for (const el of $("graph").querySelectorAll(".node")) {
    const m = el.id.match(/-([A-Za-z0-9_]+)-\\d+$/);
    if (m && STEPS[m[1]]) el.addEventListener("click", () => select(m[1], el));
  }
}).catch((err) => { $("graph").textContent = "Diagram failed to render: " + err; });
</script>
</body>
</html>
"""


def to_html(wf: dict, mmd: str, frame_base: str, note: str = "") -> str:
    steps = {_node_id(s["id"]): s for s in wf.get("steps", [])}
    run = wf.get("run") or {}
    meta = []
    if run.get("tenant_id"):
        meta.append(f"{run['tenant_id']}/{run.get('agent_id', '?')}")
    if run.get("window"):
        meta.append(" – ".join(run["window"]))
    if note:
        meta.append(note)

    def js(value) -> str:
        return json.dumps(value).replace("</", "<\\/")

    return (_HTML
            .replace("__TITLE__", html.escape(wf.get("title") or "Workflow"))
            .replace("__SUMMARY__", html.escape(wf.get("summary", "")))
            .replace("__META__", html.escape(" · ".join(meta)))
            .replace("__STEPS__", js(steps))
            .replace("__MMD__", js(mmd))
            .replace("__FRAME_BASE__", js(frame_base)))


def render_outputs(wf: dict, note: str, frame_base: str) -> dict[str, str]:
    """Render the artifact set as {suffix: content} — the caller decides where
    it lands (local files or S3 objects)."""
    mmd = to_mermaid(wf)
    run = wf.get("run") or {}
    prov = []
    if run.get("tenant_id"):
        prov.append(f"agent: {run['tenant_id']}/{run.get('agent_id', '?')}")
    if run.get("window"):
        prov.append(f"window: {run['window'][0]} – {run['window'][1]}")
    rows = "\n".join(
        f"| {s['id']} | {s.get('name', '')} | {s.get('app', '')} | "
        f"{', '.join(s.get('time_ranges') or [])} | {s.get('iterations', 1)} | "
        f"{len(s.get('frames') or [])} |"
        for s in wf.get("steps", [])
    )
    md = "\n".join([
        f"# Workflow: {wf.get('title') or 'Workflow'}",
        "",
        wf.get("summary", ""),
        "",
        f"- source: {run.get('source_report', '?')}",
        f"- {note}",
        *([f"- {'; '.join(prov)}"] if prov else []),
        "",
        "```mermaid",
        mmd,
        "```",
        "",
        "| step | name | app | time (UTC) | iterations | frames |",
        "|---|---|---|---|---|---|",
        rows,
        "",
    ])
    return {
        ".workflow.json": json.dumps(wf, indent=2) + "\n",
        ".workflow.mmd": mmd + "\n",
        ".workflow.md": md,
        ".workflow.html": to_html(wf, mmd, frame_base, note),
    }


def write_outputs(base: Path, wf: dict, note: str, frame_base: str) -> list[Path]:
    """Write <base>.workflow.{json,mmd,md,html} locally; returns the paths."""
    paths = []
    for suffix, content in render_outputs(wf, note, frame_base).items():
        p = Path(f"{base}{suffix}")
        p.write_text(content)
        paths.append(p)
    return paths
