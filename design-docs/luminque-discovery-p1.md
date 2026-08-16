# Luminque Discovery — Phase 1: Pipeline & Workflow Viewer

> **Status: in progress.** Steps 1–4 of the implementation order are built
> (`discovery/workflow.py` contract + resolver + renderers, `analyze.py`
> map/reduce/sample, `visualize.py` fallback extractor/re-renderer); Tier 1
> hosting is not. Remote execution / S3 streaming (steps 6–9) is planned —
> see "Remote execution & S3 streaming". The ingestion contract lives in
> `luminque-ingestion-p1.md`; hosting/IaC in `luminque-infra-p1.md`.

## Context

Discovery today is two manual scripts: `discovery/analyze.py` (frames →
prose report, `sample` or `mapreduce` mode) and `discovery/visualize.py`
(report → workflow JSON via a second model call → Mermaid). Screenshots live
in S3 as `{tenant_id}/{agent_id}/{YYYY-MM-DD}/{captured_at_iso}.png`;
`scripts/pull-screenshots.sh` mirrors that layout locally, so a frame's
identity is its capture timestamp end to end.

This phase makes the pipeline scale past one-request-sized recordings and
adds a viewer where each workflow step links to its screenshots.

## Decisions

| Concern | Choice | Why |
|---|---|---|
| Workflow contract | Extended `.workflow.json`, not `.mmd` | Mermaid is a lossy rendering with nowhere to put frame keys. JSON is already the source of truth; `.mmd`/`.md` remain derived debug artifacts. |
| Step↔frame linking | Deterministic timestamp resolution, no LLM | The model emits `time_ranges` per step; every frame's key contains `captured_at`. A resolver selects frames inside each range and writes their S3 keys into the JSON. No hallucinated keys; overlapping ranges give an honest many-to-many mapping. |
| JSON emission | The final (reduce) call emits prose analysis **then** a fenced JSON block, one response | Adaptive thinking does the reasoning, so trailing JSON costs little quality; prose-first keeps uncertainty articulated (a bare schema invites confident slot-filling). Emitting from full context beats the current telephone game (Nova re-extracting from compressed prose). Check `stop_reason` for truncation. |
| Extraction fallback | Keep `visualize.py`'s extract-from-text path | Vision is ~95% of cost. When the schema changes, re-run reduce/extraction over stored notes for pennies instead of re-paying vision. |
| Pipeline shape | Split map from reduce; persist segment notes | Notes are append-only per time window: runs become resumable, new frames only cost new segments, vision is paid once per frame ever. Matches the future discovery service (map on schedule, reduce on demand). |
| Scale ceiling | Hierarchical reduce (map → hour combine → reduce) | A full day (~8–12K frames → 300–500 segments) puts the flat reduce at/past the context window. Hour-level combine summaries (must preserve timestamps + app names) keep every call bounded; two tiers covers weeks. Trigger automatically above ~100K estimated note tokens. |
| Chunk boundaries | Prefer idle gaps over blind 25-frame blocks | Timestamps already encode idleness; segments aligned to activity produce cleaner notes. Window titles (in Postgres per the ingestion contract) are a better signal later — not MVP. |
| Analysis scope | Bounded windows (a day, a shift), not "everything synced" | Cross-window workflow merging is a later problem; per-window workflows are the honest MVP output. |
| Viewer | Self-contained HTML per run, emitted next to the JSON | Inlined workflow JSON, Mermaid via CDN, node click → filmstrip panel of that step's frames via relative paths. No app, no build, no server. |
| Hosting | Tier 0 now: none (open the file, or `python -m http.server`) | `<img>` works off `file://`; frames are already synced locally. |
| Hosting, later | Tier 1: CloudFront + OAC + basic-auth CloudFront Function over the existing screenshots bucket | Frames are already in the bucket; upload run HTML to `runs/` and relative paths resolve against bucket keys. No compute, no new subprocessor, bucket stays block-all-public. ~60 lines of Terraform when sharing is needed. |
| Not chosen | Reviving `sop-discovery.nosync/apps/web-ui` | Prototype's data model (sessions, devices, deviations) is from the previous iteration and contradicts the no-sessions contract. Steal its look, not its plumbing. |
| Not chosen | JSON-only analysis output | Loses the auditable report and invites schema slot-filling; see JSON emission row. |
| Not chosen | Third-party static hosts (Pages/Netlify/Vercel) | Third subprocessor holding customer screenshots — fails the same test that ruled out PaaS in `luminque-infra-p1.md`. |

## Artifact contract

`<base>.workflow.json`, extended:

```json
{
  "title": "...",
  "summary": "...",
  "run": {
    "tenant_id": "...",
    "agent_id": "...",
    "window": ["2026-07-16T03:09:00Z", "2026-07-16T03:31:00Z"],
    "source_report": "20260716T032144Z_mapreduce.md"
  },
  "steps": [{
    "id": "s1",
    "name": "Create Purchase Invoice",
    "app": "Dynamics 365 Business Central",
    "time_ranges": ["03:09:12-03:11:05"],
    "iterations": 2,
    "frames": ["{tenant}/{agent}/2026-07-16/2026-07-16T03:09:12+00:00.png"]
  }],
  "edges": [{"from": "s1", "to": "s2", "label": "...", "loop": false}]
}
```

- `frames` is written by the deterministic resolver, never the model.
- Store S3 **keys**, not URLs — presigned URLs expire; mint at view time.
- Frames are ~1 fps, so a 2-minute step holds ~120 keys; store all, let the
  viewer subsample thumbnails.
- Ask the model for second-granularity time ranges (segment notes have them).

## Pipeline shape

```
map     frames in window        → discovery/notes/{tenant}/{agent}/{start}--{end}.json
                                  (skip if exists; the only stage touching images)
reduce  notes in window         → report .md + workflow .json (prose + fenced JSON;
                                  hour-combine tier when notes exceed budget)
render  workflow .json          → .html (+ .mmd/.md debug artifacts)
```

Note file: `{start, end, frames: [...], model, notes}`.

## Viewer

One HTML file per run: workflow JSON inlined, Mermaid from CDN, per-node
click handlers open a filmstrip/lightbox of the step's frames. Frame base
path is a single generator-set constant: default `../../screenshots`
(local, relative to `discovery/runs/`), `--frame-base ..` when generating
for bucket upload. Auth at Tier 0: none. Tier 1: one shared basic-auth
secret at the edge.

## Implementation order

1. ~~Frame resolver + extended schema~~ — done, in `discovery/workflow.py`
   (shared by `analyze.py` and `visualize.py`).
2. ~~HTML emitter~~ — done (`workflow.to_html`, emitted as
   `<base>.workflow.html` by both entry points).
3. ~~`analyze.py` restructure~~ — done: `map`/`reduce`/`sample` subcommands,
   persistent notes, prose + fenced JSON in one response (`sample` emits the
   JSON too; a custom `--prompt-file` is respected verbatim and skips it).
4. ~~Hierarchical combine tier~~ — done, triggers above `NOTE_CHAR_BUDGET`
   (~100K tokens of notes).
5. Tier 1 CloudFront Terraform (when someone else needs to see a run).
6. ~~S3 streaming in `discovery/`~~ — done: `store.py` (FrameSource/Store,
   local + S3), `analyze.py` over `Frame` objects with
   `--tenant`/`--agent`/`--date` and `s3://` notes/runs, `serve.py` viewer
   proxy, defaults moved to us-east-2 / Converse maxTokens 8192.
7. Terraform: artifacts bucket; discovery-role trust/Bedrock/SSM edits +
   instance profile; EC2 instance.
8. `scripts/run-discovery.sh` (start instance → sync code → SSM run →
   stop) and viewing via `serve.py`.
9. End-to-end: one agent, one day, triggered via SSM; artifacts in S3;
   viewer streaming frames.

## Remote execution & S3 streaming (planned 2026-07-22)

Frames must no longer require local download: analysis runs next to the data
(the `luminque-infra-p1.md` perimeter rule — laptops only receive outputs),
triggered per agent per day. Decisions:

| Concern | Choice | Why |
|---|---|---|
| Frame source | `--frames` accepts a local dir or `s3://` URI; `--tenant`/`--agent`/`--date` narrow the listing to date prefixes | Keys are date-partitioned, so one-day-one-agent is a single prefix list. Object sizes come from the listing, so chunk budgeting needs no fetches; bytes stream per chunk at request time and never touch disk. |
| Notes/runs storage | `--notes-dir`/`--runs-dir` accept `s3://` URIs; incremental skip = one `list_objects_v2` on the notes prefix | Compute becomes stateless and disposable; any machine continues the same incremental ledger. Local paths keep working unchanged. |
| Artifacts bucket | New `luminque-discovery-<account>` with `notes/` and `runs/` prefixes | The screenshots bucket's lifecycle rule is bucket-wide (30-day expiry) and would delete paid-for notes; raw PII and derived artifacts also want different access policies. Screenshots bucket stays read-only to discovery. |
| Compute | EC2 `t3.small`, private subnet, **no inbound SG rules**, stopped between runs | SSM agent dials out, so no SSH/bastion/API surface. Stopped cost ≈ EBS only. |
| Trigger | SSM: Session Manager (console browser terminal) or `aws ssm send-command` from the laptop, wrapped in `scripts/run-discovery.sh` | No HTTP API to build or secure at MVP (Postman-style triggering needs a server; nothing here has one). Run Command needs a raised `executionTimeout` and CloudWatch log output (inline output truncates at ~24K chars). |
| IAM | Extend the existing `luminque-discovery` role: add `ec2.amazonaws.com` trust, `bedrock:InvokeModel*`, artifacts-bucket read/write, `AmazonSSMManagedInstanceCore`; wrap in an instance profile | Same edits a Fargate task needs later — nothing throwaway. Role currently trusts only `ecs-tasks.amazonaws.com` and has no Bedrock or write perms. |
| Code deploy | Wrapper `aws s3 sync`s the working `discovery/` to the artifacts bucket; instance pulls before each run | No git credentials on the instance; what runs is the working tree. |
| Viewer | `discovery/serve.py`: local proxy serving run artifacts from the artifacts bucket and `GET /frames/<key>` from the screenshots bucket via local creds; HTML generated with `--frame-base http://localhost:<port>/frames` | Browsers can't read a private bucket and presigned URLs die with the SSO session. Frames stream on demand for steps actually viewed. Retires when Tier 1 CloudFront lands (only the frame base changes). |
| Bedrock region | Script defaults move to us-east-2; bump Converse `maxTokens` for reduce | Account capacity: us-east-1 quotas are all 0; Opus 4.5 CRIS in us-east-2 = 2M TPM / 5 RPM / 6.75M tokens-day. 5 RPM paces long map runs via the existing adaptive retries. |
| Not chosen | Bedrock batch inference (for now) | Halves cost and escapes the 5 RPM cap, but 100-record job minimum and 1 GB input cap don't fit single-day runs; revisit at multi-day scale. |

## Later / open

- Cross-window workflow dedupe/merging (recurring processes).
- Window-title metadata sidecar from Postgres for smarter chunk boundaries.
- Read API / presign endpoint if the viewer outgrows static hosting
  (seed of the dashboard read side).
