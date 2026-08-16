# Jupiter — Luminque

Luminque records what people actually do on their workstations and
reconstructs the business process from it: a Windows agent captures
screenshots locally, a cloud service ingests them, and an analysis pipeline
turns a day of frames into a described workflow with the screenshots attached
as evidence.

**New here — including if you are working through an AI assistant — read
[`CLAUDE.md`](CLAUDE.md) first.** It has the component map, what is actually
deployed versus merely designed, and the AWS account facts (especially the
Bedrock capacity limits) that cannot be worked out by reading the code.

## The four components

```
Windows workstation          AWS                        Operator's laptop
   agent/  ──── HTTPS ───▶   ingestion/  ──▶ S3  ───▶   discovery/
   captures frames           stores frames              reconstructs the process
                             terraform/ provisions the middle
```

| Directory | What it is | Docs |
|---|---|---|
| [`agent/`](agent/) | Windows capture agent — one `.exe`, five modes | [`agent/CLAUDE.md`](agent/CLAUDE.md) |
| [`ingestion/`](ingestion/) | FastAPI ingest service on ECS Fargate | [`ingestion/README.md`](ingestion/README.md) |
| [`terraform/`](terraform/) | All AWS infrastructure | [`terraform/README.md`](terraform/README.md) |
| [`discovery/`](discovery/) | Screenshots → workflow analysis + viewer | [`discovery/README.md`](discovery/README.md) |
| [`design-docs/`](design-docs/) | Full technical design records for all of the above | — |

## Common tasks

| I want to… | Go to |
|---|---|
| Analyze a day of captured screenshots | [`discovery/README.md`](discovery/README.md) |
| Understand the workflow JSON, or view a run | [`discovery/README.md`](discovery/README.md) → Viewing |
| Build the Windows `.exe` | [`agent/README.md`](agent/README.md) → Building |
| Change how capture, sending, or onboarding works | [`agent/CLAUDE.md`](agent/CLAUDE.md) |
| Deploy or change AWS infrastructure | [`terraform/README.md`](terraform/README.md) |
| Onboard a new customer (create a tenant) | [`terraform/README.md`](terraform/README.md) → Creating a tenant |
| Know why something was built this way | [`design-docs/`](design-docs/) |

## Getting set up

```bash
aws sso login --profile kangaroo
export AWS_PROFILE=kangaroo
```

Python components use [uv](https://docs.astral.sh/uv/); run `uv sync` inside
`agent/` or `ingestion/`. `discovery/` scripts are self-contained — `uv run`
resolves their dependencies from the script header, no install step.

## Status

The stack is deployed and frames flow end to end. Three things are
intentionally unfinished and will bite if assumed otherwise:

- **No domain yet**, so the API is plain HTTP and real agents should not be
  enrolled until that changes — the endpoint URL is baked in at enrollment.
- **The agent `.exe` is unsigned**; whether to buy a certificate is an open
  decision.
- **Discovery has no cloud compute** — it runs from a laptop under a human's
  AWS session. The remote-execution design exists but is unbuilt.

`CLAUDE.md` has the full list with the reasoning.

Not part of the system: `sop-discovery.nosync/` is an archived prototype from
the previous iteration, kept for reference only.
