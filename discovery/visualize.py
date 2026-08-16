#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["anthropic[bedrock]", "boto3"]
# ///
"""Extract and render a workflow artifact from a discovery run report.

analyze.py reduce/sample emit the workflow JSON together with the analysis;
this script is the fallback extractor for reports without one (a single
text-only model call — cheap schema iteration without re-running vision) and
the deterministic re-renderer for existing artifacts.

Outputs, next to the input (contract in workflow.py):
  <base>.workflow.json   structured workflow — source of truth
  <base>.workflow.md     summary, diagram, step table (renders on GitHub)
  <base>.workflow.mmd    bare Mermaid, for pasting into https://mermaid.live
  <base>.workflow.html   self-contained viewer: diagram + per-step screenshots

Usage:
  uv run discovery/visualize.py discovery/runs/<run>.md              # extract + render
  uv run discovery/visualize.py discovery/runs/<run>.workflow.json   # re-render only
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

from analyze import REPO_ROOT, Usage, make_backend, parse_arg_ts
import store
import workflow

# Text-only extraction: model quality matters less than for vision, and this
# combination works on the account today (see analyze.py for the Claude gates).
DEFAULT_MODEL = "us.amazon.nova-pro-v1:0"
DEFAULT_REGION = "us-east-2"

EXTRACT = f"""\
Convert the process analysis below into a workflow graph as JSON.

{{lens}}

Return ONLY a JSON object — no code fences, no commentary — with this shape:
{workflow.SCHEMA}

{workflow.GUIDELINES}

Process analysis:

"""


def window_from_report(text: str) -> tuple[datetime, datetime] | None:
    """Recover the frame window from a report's metadata line.

    Matches both '2026-07-10 03:09:14 – 12:57:05 UTC' (sample/legacy) and
    '2026-07-10 03:09:14 – 2026-07-10 12:57:05 UTC' (reduce).
    """
    m = re.search(r"(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2}) – "
                  r"(?:(\d{4}-\d{2}-\d{2}) )?(\d{2}:\d{2}:\d{2}) UTC", text)
    if not m:
        return None
    d1, t1, d2, t2 = m.groups()
    start = datetime.fromisoformat(f"{d1}T{t1}+00:00")
    end = datetime.fromisoformat(f"{d2 or d1}T{t2}+00:00")
    if end < start:  # legacy single-date form crossing midnight
        end += timedelta(days=1)
    return start, end


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input", type=Path, help="run report .md, or a .workflow.json to re-render")
    ap.add_argument("--lens", choices=sorted(workflow.LENSES), default="process",
                    help="process: the business workflow (incl. one demonstrated in a video); "
                         "activity: what the user physically did")
    ap.add_argument("--frames-dir", default=str(REPO_ROOT / "screenshots"),
                    help="frames layout root: local dir or s3://bucket[/prefix]")
    ap.add_argument("--frame-base", default=None,
                    help="frame URL prefix baked into the HTML (default: relative "
                         "path to a local --frames-dir, /frames for s3://)")
    ap.add_argument("--start", type=parse_arg_ts, default=None,
                    help="frame window override for resolution, ISO UTC")
    ap.add_argument("--end", type=parse_arg_ts, default=None)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--region", default=DEFAULT_REGION)
    args = ap.parse_args()

    window = (args.start, args.end)
    if args.input.suffixes[-2:] == [".workflow", ".json"]:
        base = args.input.with_suffix("").with_suffix("")
        wf = json.loads(args.input.read_text())
        note = f"re-rendered from {args.input.name}"
    else:
        base = args.input.with_suffix("")
        report = args.input.read_text()
        call, _ = make_backend(args.model, args.region)
        usage = Usage()
        raw = call([], EXTRACT.replace("{lens}", workflow.LENSES[args.lens]) + report, usage)
        try:
            wf = workflow.parse_workflow(raw)
        except json.JSONDecodeError:
            fail = base.with_suffix(".workflow.raw.txt")
            fail.write_text(raw)
            sys.exit(f"model did not return valid JSON; raw output saved to {fail}")
        note = (f"extracted by {args.model} ({usage.input:,} in / {usage.output:,} out tokens); "
                f"json: {base.name}.workflow.json")
        if window == (None, None):
            window = window_from_report(report) or (None, None)

    wf.setdefault("run", {}).setdefault("source_report", args.input.name)
    if window == (None, None) and wf["run"].get("window"):
        window = tuple(datetime.fromisoformat(w) for w in wf["run"]["window"])

    src = store.make_frames(args.frames_dir)
    keys = sorted(f.key for f in src.list("", *window))
    if keys:
        workflow.resolve_frames(wf, keys)
    else:
        print(f"no frames found under {src}; keeping existing frame links", file=sys.stderr)

    frame_base = args.frame_base or (
        "/frames" if store.is_s3(args.frames_dir)
        else os.path.relpath(args.frames_dir, base.parent))
    paths = workflow.write_outputs(base, wf, note, frame_base)
    print(workflow.to_mermaid(wf))
    print("\n---\nwritten: " + ", ".join(p.name for p in paths), file=sys.stderr)


if __name__ == "__main__":
    main()
