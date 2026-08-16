#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["anthropic[bedrock]", "boto3"]
# ///
"""Discovery pipeline: reconstruct a business process from captured screenshots.

Frames live as {tenant}/{agent}/{YYYY-MM-DD}/{captured_at_iso}.png under a
layout root: the S3 screenshots bucket, or a local mirror pulled by
scripts/pull-screenshots.sh. `--frames-dir` accepts either (`s3://bucket` or a
path); S3 frames are streamed per chunk and never touch disk. `--notes-dir`
and `--runs-dir` likewise accept `s3://` URIs so runs on remote compute are
stateless. Select what to analyze with --tenant/--agent/--date and/or
--start/--end. Design: design-docs/luminque-discovery-p1.md.

Subcommands:
  map     describe chronological frame chunks into persistent segment notes
          ({notes-dir}/{tenant}/{agent}/{start}--{end}.json). Incremental:
          chunks whose note already exists are skipped, so re-runs only pay
          for new frames; vision cost is paid once per frame ever. Chunks
          split at idle gaps when possible. Notes are keyed by exact time
          range — after changing chunking parameters, clear the notes dir to
          avoid overlapping segments.
  reduce  synthesize the notes in a window into a prose report plus a fenced
          workflow-JSON block (text-only, cheap to re-run). Windows too large
          for one request are first combined into hour summaries. Frames are
          then linked to steps deterministically and the workflow is rendered
          to .json/.md/.mmd/.html (see workflow.py).
  sample  one-shot quick look: evenly-spaced frames, single request, same
          outputs as reduce.

Backends (chosen by model id):
  anthropic.*   Claude via the Anthropic SDK's Bedrock (mantle) client
                (mantle endpoint exists in us-east-1 only on this account)
  anything else Converse API via boto3 (e.g. us.anthropic.* inference
                profiles, us.amazon.nova-pro-v1:0)

Usage:
  uv run discovery/analyze.py map --tenant <t> --agent <a> --date 2026-07-21
  uv run discovery/analyze.py map --frames-dir s3://<shots-bucket> \
      --notes-dir s3://<artifacts-bucket>/notes --date 2026-07-21
  uv run discovery/analyze.py reduce --start 2026-07-21T09:00 --end 2026-07-21T17:00
  uv run discovery/analyze.py sample --max-frames 30
"""

import argparse
import base64
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

os.environ.setdefault("AWS_PROFILE", "kangaroo")

import anthropic
import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

import store
import workflow

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MODEL = "us.anthropic.claude-opus-4-5-20251101-v1:0"
# Bedrock capacity on this account lives in us-east-2 (us-east-1 quotas are 0).
DEFAULT_REGION = "us-east-2"

# Frames are ~450-700 KB PNGs; base64 inflates by 4/3. Bedrock request caps are
# below the API's 32 MB, so budget each request's image payload conservatively.
B64_BUDGET = 14 * 1024 * 1024
# Converse-API models get a stricter per-request image count.
CONVERSE_MAX_IMAGES = 20
# Above this many characters of notes, reduce inserts the hour-combine tier
# (~100K tokens; a flat reduce must fit the model context with headroom).
NOTE_CHAR_BUDGET = 400_000

SYSTEM = """\
You are a business-process analyst. You are given screenshots captured from an
employee's workstation by a screen recorder (~1 frame per second while the user
is active, near-duplicate frames removed). Each frame is preceded by its
capture time (UTC). Frames are in chronological order; gaps in timestamps mean
the user was idle or the recorder paused.

Ground every claim in what is visible in the frames. When you infer something,
say so and state the evidence."""

TASK = """\
Reconstruct the business process the user was executing:

1. **Timeline** — the main activities with approximate time ranges.
2. **Process** — the step-by-step workflow the user follows, including
   repeated loops (note how many iterations you observed).
3. **Applications and documents** — what tools, sites, and files are involved
   and what role each plays.
4. **Inputs and outputs** — where data comes from and where it ends up.
5. **Confidence and gaps** — what you could not determine and what additional
   evidence (more frames, window titles, other time ranges) would help."""

MAP_TASK = """\
Describe what the user is doing in this segment of the recording. Note the
applications and documents in use, the data being read or entered, concrete
actions taken, and any transition between activities. Be specific and concise:
a timestamped log of observations, not interpretation. End with one sentence
on what this segment appears to accomplish."""

COMBINE_TASK = """\
Below are chronological observation logs from consecutive segments of one hour
of a screen recording. Condense them into one compact timestamped outline.
Preserve concrete timestamps (HH:MM:SS), application/site and document names,
data being read or entered, repeated-step counts, and transitions between
activities. Do not interpret; keep it an observation log."""

REDUCE_INTRO = """\
Below are chronological observations from one screen recording, produced by
analyzing the raw frames segment by segment (long windows are pre-combined
into hour summaries). Using only these observations:"""


def json_task(lens: str) -> str:
    return workflow.JSON_INSTRUCTIONS.replace("{lens}", workflow.LENSES[lens])


# --- Frames ------------------------------------------------------------------

def frame_pair(key: str) -> tuple[str, str]:
    parts = key.split("/")
    return (parts[0], parts[1]) if len(parts) >= 4 else ("_", "_")


def load_frames(src, args) -> list[store.Frame]:
    prefix, match = store.selection(args.tenant, args.agent, args.date)
    frames = [f for f in src.list(prefix, args.start, args.end) if match(f.key)]
    frames.sort(key=lambda f: f.ts)

    pairs = {frame_pair(f.key) for f in frames}
    if len(pairs) > 1:
        print(f"warning: frames span {len(pairs)} tenant/agent pairs; "
              "the analysis assumes a single workstation", file=sys.stderr)
    return frames


def evenly(frames: list[store.Frame], k: int) -> list[store.Frame]:
    if k >= len(frames):
        return list(frames)
    idx = sorted({round(i * (len(frames) - 1) / (k - 1)) for i in range(k)})
    return [frames[i] for i in idx]


def fit_budget(frames: list[store.Frame], k: int) -> list[store.Frame]:
    while True:
        sel = evenly(frames, k)
        if sum(f.size * 4 // 3 for f in sel) <= B64_BUDGET or k <= 2:
            return sel
        k = max(2, int(k * 0.85))


def chunk(frames: list[store.Frame], max_frames: int,
          gap: timedelta | None = None) -> list[list[store.Frame]]:
    chunks: list[list[store.Frame]] = []
    cur: list[store.Frame] = []
    cur_bytes = 0
    prev: datetime | None = None
    for f in frames:
        size = f.size * 4 // 3
        if cur and (cur_bytes + size > B64_BUDGET or len(cur) >= max_frames
                    or (gap and prev and f.ts - prev > gap)):
            chunks.append(cur)
            cur, cur_bytes = [], 0
        cur.append(f)
        cur_bytes += size
        prev = f.ts
    if cur:
        chunks.append(cur)
    return chunks


def time_range(frames: list[store.Frame]) -> str:
    return f"{frames[0].ts:%Y-%m-%d %H:%M:%S} – {frames[-1].ts:%H:%M:%S} UTC"


# --- Model backends ----------------------------------------------------------

class Usage:
    def __init__(self) -> None:
        self.input = 0
        self.output = 0
        self.calls = 0

    def add(self, input_tokens: int, output_tokens: int) -> None:
        self.input += input_tokens
        self.output += output_tokens
        self.calls += 1


def make_backend(model: str, region: str):
    """Return (call_fn, max_images_per_request). call_fn(frames, text, usage) -> str."""
    if model.startswith("anthropic."):
        if region != "us-east-1":
            print(f"warning: mantle models exist in us-east-1 only on this account; "
                  f"--region {region} will likely fail (pass --region us-east-1)", file=sys.stderr)
        client = anthropic.AnthropicBedrockMantle(aws_region=region)

        def call(frames: list[store.Frame], text: str, usage: Usage) -> str:
            content: list[dict] = []
            for f in frames:
                content.append({"type": "text", "text": f"Frame {f.ts:%H:%M:%S} UTC:"})
                content.append({
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": base64.standard_b64encode(f.read()).decode(),
                    },
                })
            content.append({"type": "text", "text": text})
            with client.messages.stream(
                model=model,
                max_tokens=32000,
                thinking={"type": "adaptive"},
                system=SYSTEM,
                messages=[{"role": "user", "content": content}],
            ) as stream:
                msg = stream.get_final_message()
            usage.add(msg.usage.input_tokens, msg.usage.output_tokens)
            if msg.stop_reason == "max_tokens":
                print("warning: response truncated at max_tokens", file=sys.stderr)
            return "".join(b.text for b in msg.content if b.type == "text")

        return call, None

    rt = boto3.client(
        "bedrock-runtime",
        region_name=region,
        config=Config(retries={"max_attempts": 10, "mode": "adaptive"}),
    )

    def call(frames: list[store.Frame], text: str, usage: Usage) -> str:
        content: list[dict] = []
        for f in frames:
            content.append({"text": f"Frame {f.ts:%H:%M:%S} UTC:"})
            content.append({"image": {"format": "png", "source": {"bytes": f.read()}}})
        content.append({"text": text})
        resp = rt.converse(
            modelId=model,
            system=[{"text": SYSTEM}],
            messages=[{"role": "user", "content": content}],
            inferenceConfig={"maxTokens": 8192},
        )
        u = resp["usage"]
        usage.add(u["inputTokens"], u["outputTokens"])
        if resp.get("stopReason") == "max_tokens":
            print("warning: response truncated at max_tokens", file=sys.stderr)
        return "".join(c.get("text", "") for c in resp["output"]["message"]["content"])

    return call, CONVERSE_MAX_IMAGES


# --- Notes -------------------------------------------------------------------

def nstamp(ts: datetime) -> str:
    return ts.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def seg_range(note: dict) -> str:
    return f"{note['_start']:%H:%M:%S}–{note['_end']:%H:%M:%S}"


def load_notes(notes_store, args) -> list[dict]:
    _, match = store.selection(args.tenant, args.agent, None)
    notes = []
    for rel in notes_store.list(".json"):
        if not match(rel):
            continue
        try:
            d = json.loads(notes_store.read_text(rel))
            d["_start"] = datetime.fromisoformat(d["start"])
            d["_end"] = datetime.fromisoformat(d["end"])
        except (json.JSONDecodeError, KeyError, ValueError):
            print(f"skipping unreadable note: {rel}", file=sys.stderr)
            continue
        if args.start and d["_end"] < args.start:
            continue
        if args.end and d["_start"] > args.end:
            continue
        notes.append(d)
    notes.sort(key=lambda n: n["_start"])
    for a, b in zip(notes, notes[1:]):
        if b["_start"] < a["_end"]:
            print(f"warning: overlapping notes {seg_range(a)} and {seg_range(b)} — "
                  "chunking params may have changed; consider clearing the notes dir",
                  file=sys.stderr)
    return notes


def hour_groups(notes: list[dict]) -> list[tuple[datetime, list[dict]]]:
    groups: dict[datetime, list[dict]] = {}
    for n in notes:
        h = n["_start"].replace(minute=0, second=0, microsecond=0)
        groups.setdefault(h, []).append(n)
    return sorted(groups.items())


# --- Subcommands -------------------------------------------------------------

def cmd_map(args) -> None:
    src = store.make_frames(args.frames_dir)
    frames = load_frames(src, args)
    if not frames:
        sys.exit(f"no frames found under {src} for this selection")
    notes_store = store.make_store(args.notes_dir)
    existing = set(notes_store.list(".json"))
    call, image_cap = make_backend(args.model, args.region)
    per_chunk = min(args.chunk_frames, image_cap) if image_cap else args.chunk_frames
    chunks = chunk(frames, per_chunk, timedelta(minutes=args.gap_minutes))
    usage = Usage()
    done = 0
    print(f"map: {len(frames)} frames in {len(chunks)} chunks, {time_range(frames)}", file=sys.stderr)
    for i, ch in enumerate(chunks, 1):
        tenant, agent = frame_pair(ch[0].key)
        rel = f"{tenant}/{agent}/{nstamp(ch[0].ts)}--{nstamp(ch[-1].ts)}.json"
        if rel in existing:
            continue
        print(f"  chunk {i}/{len(chunks)}: {len(ch)} frames, {time_range(ch)}", file=sys.stderr)
        notes = call(ch, MAP_TASK, usage)
        notes_store.write_text(rel, json.dumps({
            "start": ch[0].ts.isoformat(),
            "end": ch[-1].ts.isoformat(),
            "tenant": tenant,
            "agent": agent,
            "model": args.model,
            "frames": [f.key for f in ch],
            "notes": notes,
        }, indent=2) + "\n")
        done += 1
    print(f"map: {done} new notes, {len(chunks) - done} already present, in {notes_store.url()} "
          f"({usage.input:,} in / {usage.output:,} out tokens)", file=sys.stderr)


def cmd_reduce(args) -> None:
    notes_store = store.make_store(args.notes_dir)
    notes = load_notes(notes_store, args)
    if not notes:
        sys.exit(f"no notes under {notes_store.url()} (run `analyze.py map` first?)")
    window = (notes[0]["_start"], max(n["_end"] for n in notes))
    window_str = f"{window[0]:%Y-%m-%d %H:%M:%S} – {window[1]:%Y-%m-%d %H:%M:%S} UTC"
    call, _ = make_backend(args.model, args.region)
    usage = Usage()

    combined = sum(len(n["notes"]) for n in notes) > NOTE_CHAR_BUDGET
    if combined:
        groups = hour_groups(notes)
        print(f"reduce: {len(notes)} segments over budget; combining into "
              f"{len(groups)} hour summaries", file=sys.stderr)
        sections = []
        for h, members in groups:
            body = "\n\n".join(
                f"### {seg_range(m)} ({len(m['frames'])} frames)\n{m['notes']}" for m in members)
            summary = call([], COMBINE_TASK + "\n\n" + body, usage)
            sections.append(f"## {h:%Y-%m-%d %H:%M} UTC hour "
                            f"(combined from {len(members)} segments)\n{summary}")
    else:
        print(f"reduce: {len(notes)} segments, {window_str}", file=sys.stderr)
        sections = [
            f"## Segment {i} ({seg_range(n)}, {len(n['frames'])} frames)\n{n['notes']}"
            for i, n in enumerate(notes, 1)
        ]

    prompt = "\n\n".join([REDUCE_INTRO, TASK, json_task(args.lens), *sections])
    result = call([], prompt, usage)
    meta = [
        "- mode: reduce" + (" (hour-combined)" if combined else ""),
        f"- model: {args.model} ({args.region})",
        f"- notes: {len(notes)} segments, {window_str}",
    ]

    # Resolver universe: all frames in the window for the notes' tenant/agent.
    pairs = {(n.get("tenant"), n.get("agent")) for n in notes}
    tenant, agent = pairs.pop() if len(pairs) == 1 else (args.tenant, args.agent)
    prefix, match = store.selection(None if tenant == "_" else tenant,
                                    None if agent == "_" else agent, None)
    src = store.make_frames(args.frames_dir)
    keys = sorted(f.key for f in src.list(prefix, *window) if match(f.key))

    finish_run(args, "reduce", meta, result, usage, keys)


def cmd_sample(args) -> None:
    src = store.make_frames(args.frames_dir)
    frames = load_frames(src, args)
    if not frames:
        sys.exit(f"no frames found under {src} for this selection")
    call, image_cap = make_backend(args.model, args.region)
    usage = Usage()
    k = min(args.max_frames, len(frames))
    if image_cap:
        k = min(k, image_cap)
    sel = fit_budget(frames, k)
    print(f"sample: {len(sel)}/{len(frames)} frames, {time_range(sel)}", file=sys.stderr)
    # A custom prompt is respected verbatim — no workflow JSON is requested.
    task = args.prompt_file.read_text() if args.prompt_file else TASK + "\n\n" + json_task(args.lens)
    result = call(sel, task, usage)
    meta = [
        "- mode: sample",
        f"- model: {args.model} ({args.region})",
        f"- frames: {len(sel)} of {len(frames)} available, {time_range(sel)}",
    ]
    finish_run(args, "sample", meta, result, usage, [f.key for f in frames])


def finish_run(args, mode: str, meta: list[str], result: str, usage: Usage,
               resolver_keys: list[str]) -> None:
    """Write the report, then resolve frames and render the workflow artifact."""
    report, wf = workflow.split_workflow(result)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    runs_store = store.make_store(args.runs_dir)
    stem = f"{stamp}_{mode}"
    report_name = f"{stem}.md"
    runs_store.write_text(report_name, "\n".join([
        f"# Discovery run {stamp}",
        "",
        *meta,
        f"- tokens: {usage.input:,} in / {usage.output:,} out over {usage.calls} calls",
        "",
        "## Process analysis",
        "",
        report,
        "",
    ]))
    print(report)

    if wf is None:
        print("warning: no workflow JSON block in the response; extract one with: "
              f"uv run discovery/visualize.py {runs_store.url(report_name)}", file=sys.stderr)
    else:
        wf.setdefault("run", {}).setdefault("source_report", report_name)
        workflow.resolve_frames(wf, resolver_keys)
        frame_base = args.frame_base or default_frame_base(args)
        note = f"emitted by {args.model} with the analysis; {len(resolver_keys)} frames in window"
        outputs = workflow.render_outputs(wf, note, frame_base)
        for suffix, content in outputs.items():
            runs_store.write_text(f"{stem}{suffix}", content)
        print("written: " + ", ".join(f"{stem}{s}" for s in outputs), file=sys.stderr)
    print(f"\n---\nwritten to {runs_store.url(report_name)} "
          f"({usage.input:,} in / {usage.output:,} out tokens, {usage.calls} calls)", file=sys.stderr)


def default_frame_base(args) -> str:
    # Fully local: relative path from the runs dir to the frames dir, so the
    # HTML works from file:// with zero setup. Anything S3: /frames, the path
    # discovery/serve.py (and later CloudFront) serves frames under.
    if not store.is_s3(args.frames_dir) and not store.is_s3(args.runs_dir):
        return os.path.relpath(args.frames_dir, args.runs_dir)
    return "/frames"


# --- CLI ---------------------------------------------------------------------

def parse_arg_ts(s: str) -> datetime:
    dt = datetime.fromisoformat(s)
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def apply_date_window(args) -> None:
    """Fold --date into the --start/--end window (intersecting if both given)."""
    if not getattr(args, "date", None):
        return
    try:
        day = datetime.fromisoformat(args.date).replace(tzinfo=timezone.utc)
    except ValueError:
        sys.exit(f"invalid --date {args.date!r} (expected YYYY-MM-DD)")
    nxt = day + timedelta(days=1)
    args.start = max(args.start, day) if args.start else day
    args.end = min(args.end, nxt) if args.end else nxt


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--frames-dir", default=str(REPO_ROOT / "screenshots"),
                       help="frames layout root: local dir or s3://bucket[/prefix]")
        p.add_argument("--tenant", default=None, help="tenant id (key segment 1)")
        p.add_argument("--agent", default=None, help="agent id (key segment 2)")
        p.add_argument("--date", default=None, help="YYYY-MM-DD (key segment 3 + day window)")
        p.add_argument("--start", type=parse_arg_ts, default=None,
                       help="ISO time, e.g. 2026-07-10T12:00 (UTC)")
        p.add_argument("--end", type=parse_arg_ts, default=None)
        p.add_argument("--model", default=DEFAULT_MODEL)
        p.add_argument("--region", default=DEFAULT_REGION,
                       help="Bedrock region (bare anthropic.* mantle ids need us-east-1)")

    def output(p: argparse.ArgumentParser) -> None:
        p.add_argument("--runs-dir", default=str(REPO_ROOT / "discovery" / "runs"),
                       help="run artifacts destination: local dir or s3://bucket[/prefix]")
        p.add_argument("--lens", choices=sorted(workflow.LENSES), default="process",
                       help="process: the business workflow (incl. one demonstrated in a video); "
                            "activity: what the user physically did")
        p.add_argument("--frame-base", default=None,
                       help="frame URL prefix baked into the HTML (default: relative "
                            "path when fully local, /frames when S3 is involved)")

    def notes(p: argparse.ArgumentParser) -> None:
        p.add_argument("--notes-dir", default=str(REPO_ROOT / "discovery" / "notes"),
                       help="segment notes location: local dir or s3://bucket[/prefix]")

    m = sub.add_parser("map", help="describe frame chunks into persistent segment notes")
    common(m)
    notes(m)
    m.add_argument("--chunk-frames", type=int, default=25, help="max frames per segment request")
    m.add_argument("--gap-minutes", type=float, default=5.0,
                   help="start a new chunk at idle gaps longer than this")
    m.set_defaults(func=cmd_map)

    r = sub.add_parser("reduce", help="synthesize notes into a report + workflow artifact")
    common(r)
    output(r)
    notes(r)
    r.set_defaults(func=cmd_reduce)

    s = sub.add_parser("sample", help="one-shot analysis of evenly-spaced frames")
    common(s)
    output(s)
    s.add_argument("--max-frames", type=int, default=30, help="frames in the single request")
    s.add_argument("--prompt-file", type=Path, default=None,
                   help="override the analysis prompt (skips workflow JSON)")
    s.set_defaults(func=cmd_sample)

    args = ap.parse_args()
    apply_date_window(args)
    try:
        args.func(args)
    except anthropic.PermissionDeniedError as e:
        sys.exit(
            "403 from Bedrock — Anthropic models are gated on the one-time "
            f"use-case form for this account (region {args.region}).\n{e}"
        )
    except ClientError as e:
        sys.exit(f"Bedrock error {e.response['Error']['Code']}: {e.response['Error']['Message']}")


if __name__ == "__main__":
    main()
