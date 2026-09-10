#!/usr/bin/env python3
"""Minimal Gemini API demo based on the official Interactions API quickstart.

The installed google-genai version is checked first. If that SDK version exposes
``client.interactions``, this script uses the official Python SDK path. Some
currently released SDK versions do not expose Interactions yet, so the script
then falls back to the equivalent official REST request.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

import requests
from google import genai


PROJECT_ROOT = Path(__file__).resolve().parent


def load_project_dotenv() -> None:
    """Load `.env` without adding python-dotenv as a dependency."""
    env_path = PROJECT_ROOT / ".env"
    if not env_path.is_file():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        name, _, value = line.partition("=")
        name = name.removeprefix("export ").strip()
        value = value.strip().strip("'").strip('"')
        if name and name not in os.environ:
            os.environ[name] = value


def extract_rest_output_text(interaction: dict[str, Any]) -> str:
    """Extract text from a raw Interaction response."""
    chunks: list[str] = []

    for step in interaction.get("steps", []):
        if step.get("type") != "model_output":
            continue
        for part in step.get("content", []):
            if part.get("type") == "text" and part.get("text"):
                chunks.append(str(part["text"]))

    return "".join(chunks)


def call_with_sdk(api_key: str, model: str, prompt: str) -> str:
    client = genai.Client(api_key=api_key)

    # Official quickstart API:
    # interaction = client.interactions.create(model=model, input=prompt)
    interactions = getattr(client, "interactions", None)
    if interactions is None or not hasattr(interactions, "create"):
        raise RuntimeError("SDK_UNAVAILABLE")

    interaction = interactions.create(
        model=model,
        input=prompt,
    )
    return str(interaction.output_text)


def call_with_rest(
    api_key: str,
    model: str,
    prompt: str,
    api_version: str,
    timeout: float,
) -> str:
    """Call the same Interactions endpoint documented in Google's REST example."""
    endpoint = (
        f"https://generativelanguage.googleapis.com/{api_version}/interactions"
    )
    response = requests.post(
        endpoint,
        headers={
            "x-goog-api-key": api_key,
            "Content-Type": "application/json",
        },
        json={
            "model": model,
            "input": prompt,
        },
        timeout=timeout,
    )

    try:
        body = response.json()
    except ValueError:
        body = {"raw_response": response.text}

    if not response.ok:
        formatted = json.dumps(body, ensure_ascii=False, indent=2)
        raise RuntimeError(
            f"Gemini API returned HTTP {response.status_code}:\n{formatted}"
        ) from None

    return extract_rest_output_text(body)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Test the Gemini Interactions API")
    parser.add_argument(
        "--model",
        default="gemini-3.6-flash",
        help="Gemini model name (default: %(default)s)",
    )
    parser.add_argument(
        "--prompt",
        default="总结一下今天A股大盘，领涨领跌板块，市场情绪",
        help="Prompt sent to Gemini",
    )
    parser.add_argument(
        "--api-version",
        default="v1beta",
        help="Gemini API version (default: %(default)s)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=60.0,
        help="REST request timeout in seconds (default: %(default)s)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    load_project_dotenv()

    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        print(
            "ERROR: GEMINI_API_KEY is not set. Put it in .env or export it first.",
            file=sys.stderr,
        )
        return 2

    try:
        try:
            output = call_with_sdk(api_key, args.model, args.prompt)
            transport = "google-genai SDK Interactions API"
        except RuntimeError as exc:
            if str(exc) != "SDK_UNAVAILABLE":
                raise
            output = call_with_rest(
                api_key,
                args.model,
                args.prompt,
                args.api_version,
                args.timeout,
            )
            transport = "official Interactions REST API (SDK fallback)"

        print(f"[transport] {transport}")
        print("[response]")
        print(output)
        return 0
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
