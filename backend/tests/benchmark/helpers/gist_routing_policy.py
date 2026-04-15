"""Slice-aware gist routing policy.

Routes gist generation to extractive or LLM based on input characteristics,
informed by pairwise canary data:

  - short + bullet → extractive  (B-off wins 3/4 on this slice)
  - short + prose/mixed → extractive  (B-off wins 7/12 on short overall)
  - medium/long + any format → llm_gist  (B-on wins 83%/67%)

This is a BENCHMARK HELPER only — it does not modify product code.
It decides which method to use, then the test calls the appropriate generator.
"""
from __future__ import annotations

from typing import Literal

GistMethod = Literal["extractive", "llm_gist"]


def route_gist(
    length_bucket: str,
    format_bucket: str,
    lang: str = "",
) -> GistMethod:
    """Decide gist method based on input characteristics.

    Policy derived from pairwise canary (n=36):
      - short: B-off wins 7/12 overall → route to extractive
      - medium: B-on wins 10/12 → route to llm_gist
      - long: B-on wins 8/12 → route to llm_gist
    """
    if length_bucket == "short":
        return "extractive"
    return "llm_gist"


def describe_policy() -> str:
    return (
        "slice-aware hybrid: short → extractive, medium/long → llm_gist "
        "(derived from pairwise canary n=36)"
    )
