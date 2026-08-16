# Discovery

Turns captured workstation screenshots into a described business process: a
prose report, a structured workflow graph, and a browsable viewer where every
step links to the screenshots it was derived from.

Design and rationale: [`design-docs/luminque-discovery-p1.md`](../design-docs/luminque-discovery-p1.md).

## The pipeline

```
frames (S3 or local mirror)
   │
   ├─ map ──────▶ segment notes        one vision call per ~25-frame chunk.
   │              (notes/)             Incremental: a chunk already described
   │                                   is skipped, so vision is paid once per
   │                                   frame ever. The expensive stage.
   │
   ├─ reduce ───▶ report + workflow    one text call over the notes in a
   │              (runs/)              window. Cheap, re-runnable. Emits prose
   │                                   plus a fenced JSON workflow graph.
   │
   └─ render ───▶ .md .mmd .html       deterministic, no model. Steps are
                                       linked to frames by timestamp.
```

`map` and `reduce` are split because the vision pass is ~95% of the cost and
you want to re-run synthesis (new lens, different window, changed schema)
without paying for it again.

## Files

| File | What it does |
|---|---|
| `analyze.py` | Pipeline entry point: `map`, `reduce`, `sample` subcommands. The only thing you normally run. |
| `workflow.py` | The workflow artifact: JSON schema and prompt text, prose/JSON splitting, the timestamp→frame resolver, and the Mermaid/Markdown/HTML renderers. No model calls. |
| `store.py` | Local and S3 backends for frames (read-only, streamed) and artifacts (read/write). What makes `s3://` work everywhere a path is accepted. |
| `serve.py` | Local viewer: serves run artifacts and proxies frames from S3 through your AWS credentials, so screenshots never land on disk. |
| `visualize.py` | Fallback JSON extractor (when a report has no workflow block) and deterministic re-renderer for existing artifacts. |
| `ask_date.py` | Bedrock connectivity smoke test — asks the model for today's date. Use it to check access/quota before a real run. |

Runs also create two local directories, both gitignored and neither part of a
fresh checkout: `notes/` holds the segment-note cache keyed by exact time
range, and `runs/` holds output — `<stamp>_<mode>.md` plus
`.workflow.{json,md,mmd,html}`. They are created on first use, and point
elsewhere (including `s3://`) with `--notes-dir` / `--runs-dir`.

Notes are the only expensive artifact: losing `runs/` costs one cheap text
call to regenerate, losing `notes/` means re-paying the vision pass.

## Setup

```bash
aws sso login --profile kangaroo
```

Two account quirks worth knowing: Bedrock capacity on this account lives in
**us-east-2**, which is the default here — us-east-1 quotas are zero and
surface as `ThrottlingException: Too many tokens per day` on the first call.
And the current model generation is gated; Opus 4.5/4.6 and Haiku 4.5 work via
`us.*` inference profiles.

Both are account-level limits with no fix in this repo, and the throttling one
looks transient but is permanent. Before spending time on either, read the
Bedrock section of the [root `CLAUDE.md`](../CLAUDE.md) — it records which
regions have capacity, which model generations are blocked, and the quota
increase AWS already denied.

Check access before a long run:

```bash
uv run discovery/ask_date.py --model us.anthropic.claude-opus-4-5-20251101-v1:0 --region us-east-2
```

## Usage

### Streaming from S3 (no local screenshots)

Frames stream per chunk and are never written to disk.

```bash
uv run discovery/analyze.py map \
    --frames-dir s3://luminque-screenshots-047285411146 \
    --tenant <tenant-id> --agent <agent-id> --date 2026-07-10
```

```bash
uv run discovery/analyze.py reduce \
    --frames-dir s3://luminque-screenshots-047285411146 \
    --tenant <tenant-id> --agent <agent-id> --date 2026-07-10
```

Add `--notes-dir s3://<artifacts-bucket>/notes` and
`--runs-dir s3://<artifacts-bucket>/runs` to keep state off the machine
entirely — required when running on disposable compute, optional locally.

### From a local mirror

```bash
./scripts/pull-screenshots.sh <tenant>/<agent>/2026-07-10   # incremental
uv run discovery/analyze.py map
uv run discovery/analyze.py reduce
```

Defaults are `../screenshots`, `notes/`, and `runs/`, so the flags are only
needed when pointing elsewhere.

### Selecting what to analyze

`--tenant` / `--agent` / `--date` map onto the key layout
(`{tenant}/{agent}/{YYYY-MM-DD}/{captured_at}.png`), so a one-day-one-agent
run is a single prefix listing rather than a bucket walk. `--date` also
narrows the time window to that day. For sub-day precision use
`--start` / `--end` with ISO UTC times, alone or with `--date`:

```bash
uv run discovery/analyze.py reduce --date 2026-07-10 --start 2026-07-10T03:00 --end 2026-07-10T04:00
```

The bucket holds multiple agents; without selection flags every agent in
range is pulled in and the analysis warns that it assumes one workstation.

### Viewing

```bash
uv run discovery/serve.py     # add --runs-dir/--frames-dir for s3://
```

Open <http://127.0.0.1:8734/> for the run index. Click a step to see its
screenshots, click a thumbnail for a lightbox (←/→ to scrub, Esc to close).
Frames load only for steps you actually open.

Fully-local runs also work by opening `runs/<stamp>.workflow.html` directly —
the frame paths are relative in that case. Any run involving S3 needs
`serve.py`, because the bucket is private and browsers can't sign requests.

## Variations

```bash
# Quick look, no notes: one request over evenly-spaced frames
uv run discovery/analyze.py sample --max-frames 30

# Model didn't emit the JSON block (a warning says so): extract it for pennies
uv run discovery/visualize.py runs/<stamp>_reduce.md

# Re-render after editing the JSON or syncing more frames (no model call)
uv run discovery/visualize.py runs/<stamp>_reduce.workflow.json

# Graph what the user physically did instead of the business process
uv run discovery/analyze.py reduce --lens activity
```

`--frame-base` overrides the frame URL prefix baked into the HTML. It
defaults to a relative path when everything is local and `/frames` when S3 is
involved; set it explicitly when generating for a different host.

## Gotchas

**Notes are keyed by exact time range.** Changing `--chunk-frames` or
`--gap-minutes` produces overlapping segments against existing notes. Clear
the notes directory first — `reduce` warns when it sees overlap.

**Long map runs are paced by the request-per-minute quota** (5 RPM for Opus
4.5 cross-region), not by anything in the script. A ~50-chunk day takes a
while; boto3's adaptive retries handle the pacing, so occasional retry noise
is expected rather than a failure.

**`map` is resumable.** If a run dies partway, re-run the same command — it
skips every chunk that already has a note.

**Bounded windows.** Analyze a day or a shift, not everything ever captured.
Merging recurring processes across windows is a separate problem.
