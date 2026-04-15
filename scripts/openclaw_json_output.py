from __future__ import annotations

import json
import subprocess
from typing import Any


def extract_last_json_from_text(text: str) -> Any:
    rendered = str(text or "").strip()
    if not rendered:
        raise json.JSONDecodeError("No JSON content found", rendered, 0)

    decoder = json.JSONDecoder()
    parsed_candidates: list[tuple[Any, int]] = []
    for index, char in enumerate(rendered):
        if char not in "{[":
            continue
        try:
            parsed, end = decoder.raw_decode(rendered, index)
        except json.JSONDecodeError:
            continue
        parsed_candidates.append((parsed, end))

    dict_candidates = [
        (payload, end)
        for payload, end in parsed_candidates
        if isinstance(payload, dict)
    ]
    if dict_candidates:
        return max(dict_candidates, key=lambda item: item[1])[0]
    if parsed_candidates:
        return max(parsed_candidates, key=lambda item: item[1])[0]

    raise json.JSONDecodeError("No standalone JSON document found", rendered, 0)


def extract_json_from_streams(
    stdout: str | None,
    stderr: str | None,
) -> Any:
    parsed_payloads: list[Any] = []
    for text in (str(stdout or ""), str(stderr or "")):
        if not text.strip():
            continue
        try:
            parsed_payloads.append(extract_last_json_from_text(text))
        except json.JSONDecodeError:
            continue

    for payload in parsed_payloads:
        if isinstance(payload, dict):
            return payload
    if parsed_payloads:
        return parsed_payloads[0]

    combined = "\n".join(part for part in (str(stdout or ""), str(stderr or "")) if part).strip()
    raise json.JSONDecodeError(
        "No standalone JSON document found in stdout or stderr",
        combined,
        0,
    )


def parse_json_process_output(
    result: subprocess.CompletedProcess[str],
    *,
    context: str,
) -> Any:
    if result.returncode != 0:
        raise RuntimeError(
            f"{context} failed:\n"
            f"COMMAND: {' '.join(result.args if isinstance(result.args, list) else [str(result.args)])}\n"
            f"STDOUT:\n{result.stdout}\n"
            f"STDERR:\n{result.stderr}"
        )
    try:
        return extract_json_from_streams(result.stdout, result.stderr)
    except json.JSONDecodeError:
        stdout = str(result.stdout or "").strip()
        stderr = str(result.stderr or "").strip()
        if not stdout and not stderr:
            raise RuntimeError(f"{context} returned empty stdout and stderr") from None
        raise RuntimeError(
            f"{context} returned invalid JSON:\n"
            f"STDOUT:\n{result.stdout}\n"
            f"STDERR:\n{result.stderr}"
        ) from None
