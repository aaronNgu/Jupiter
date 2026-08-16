#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["anthropic[bedrock]", "boto3"]
# ///
"""Minimal Bedrock connectivity check: ask the model for today's date.

Models have no clock, so the answer tests the pipeline, not the calendar.
Works with Claude (anthropic.* model IDs) and Converse-API models like Nova:

  uv run discovery/ask_date.py
  uv run discovery/ask_date.py --model us.amazon.nova-pro-v1:0
"""

import argparse
import os
import sys

os.environ.setdefault("AWS_PROFILE", "kangaroo")

QUESTION = "What is today's date?"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="us.anthropic.claude-opus-4-6-v1")
    ap.add_argument("--region", default="us-east-1")
    args = ap.parse_args()

    if args.model.startswith("anthropic."):
        import anthropic
        from anthropic import AnthropicBedrockMantle

        client = AnthropicBedrockMantle(aws_region=args.region)
        try:
            msg = client.messages.create(
                model=args.model,
                max_tokens=100,
                messages=[{"role": "user", "content": QUESTION}],
            )
        except anthropic.APIStatusError as e:
            sys.exit(f"Bedrock error {e.status_code}: {e}")
        print("".join(b.text for b in msg.content if b.type == "text"))
    else:
        import boto3
        from botocore.exceptions import ClientError

        rt = boto3.client("bedrock-runtime", region_name=args.region)
        try:
            resp = rt.converse(
                modelId=args.model,
                messages=[{"role": "user", "content": [{"text": QUESTION}]}],
                inferenceConfig={"maxTokens": 100},
            )
        except ClientError as e:
            sys.exit(f"Bedrock error {e.response['Error']['Code']}: {e.response['Error']['Message']}")
        print("".join(c.get("text", "") for c in resp["output"]["message"]["content"]))


if __name__ == "__main__":
    main()
