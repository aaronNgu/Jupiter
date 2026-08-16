#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["boto3"]
# ///
"""Local viewer for discovery runs: serves artifacts and proxies frames.

Endpoints:
  /                index of runs (every *.workflow.html in --runs-dir)
  /runs/<name>     run artifact from --runs-dir (local dir or s3://)
  /frames/<key>    frame PNG streamed from --frames-dir (local dir or s3://),
                   fetched with your local AWS credentials, held in memory
                   only — nothing is written to disk

Viewer HTML must reference frames under /frames — that is the default frame
base whenever analyze.py/visualize.py involve S3. Retired by the Tier 1
CloudFront distribution, which serves the same paths.

Usage:
  uv run discovery/serve.py                       # local runs + local frames
  uv run discovery/serve.py \
      --frames-dir s3://luminque-screenshots-<account> \
      --runs-dir s3://luminque-discovery-<account>/runs
"""

import argparse
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

import store

REPO_ROOT = Path(__file__).resolve().parent.parent


class Handler(BaseHTTPRequestHandler):
    runs = None    # set in main()
    frames = None  # set in main()

    def do_GET(self) -> None:  # noqa: N802 (http.server API)
        path = unquote(urlparse(self.path).path)
        try:
            if path in ("/", "/index.html"):
                self.send_index()
            elif path.startswith("/runs/"):
                self.send_run(path[len("/runs/"):])
            elif path.startswith("/frames/"):
                self.send_frame(path[len("/frames/"):])
            else:
                self.send_error(404)
        except FileNotFoundError:
            self.send_error(404)
        except Exception as e:  # noqa: BLE001 — dev server: report, don't die
            if "NoSuchKey" in str(e) or "Not Found" in str(e):
                self.send_error(404)
            else:
                self.send_error(500, str(e)[:200])

    def send_payload(self, data: bytes, ctype: str, cache: bool = False) -> None:
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        if cache:  # frames are immutable — spare S3 on reselects
            self.send_header("Cache-Control", "public, max-age=86400, immutable")
        self.end_headers()
        self.wfile.write(data)

    @staticmethod
    def unsafe(rel: str) -> bool:
        return not rel or rel.startswith(("/", ".")) or ".." in rel or "\\" in rel

    def send_index(self) -> None:
        items = "\n".join(
            f'<li><a href="/runs/{name}">{name}</a></li>'
            for name in sorted(self.runs.list(".workflow.html"), reverse=True)
        ) or "<li>no runs yet</li>"
        html = ("<!doctype html><meta charset='utf-8'><title>Discovery runs</title>"
                f"<h1>Discovery runs</h1><p>{self.runs.url()}</p><ul>{items}</ul>")
        self.send_payload(html.encode(), "text/html; charset=utf-8")

    def send_run(self, rel: str) -> None:
        if self.unsafe(rel):
            return self.send_error(400)
        self.send_payload(self.runs.read_text(rel).encode(), store.content_type(rel))

    def send_frame(self, key: str) -> None:
        if self.unsafe(key):
            return self.send_error(400)
        self.send_payload(self.frames.read(key), "image/png", cache=True)

    def log_message(self, fmt: str, *fmt_args) -> None:
        print(f"  {self.address_string()} {fmt % fmt_args}", file=sys.stderr)


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--runs-dir", default=str(REPO_ROOT / "discovery" / "runs"),
                    help="run artifacts: local dir or s3://bucket[/prefix]")
    ap.add_argument("--frames-dir", default=str(REPO_ROOT / "screenshots"),
                    help="frames layout root: local dir or s3://bucket[/prefix]")
    ap.add_argument("--port", type=int, default=8734)
    ap.add_argument("--bind", default="127.0.0.1")
    args = ap.parse_args()

    Handler.runs = store.make_store(args.runs_dir)
    Handler.frames = store.make_frames(args.frames_dir)
    server = ThreadingHTTPServer((args.bind, args.port), Handler)
    print(f"serving runs from {Handler.runs.url()} and frames from {Handler.frames}\n"
          f"  -> http://{args.bind}:{args.port}/", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
