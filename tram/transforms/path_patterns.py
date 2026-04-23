"""Helpers for simple dotted-path wildcard matching."""

from __future__ import annotations


def has_path_pattern(path: str) -> bool:
    """Return True when the dotted path contains wildcard segments."""
    return "*" in path.split(".")


def path_matches_pattern(path: str, pattern: str) -> bool:
    """Match dotted paths with single-segment `*` wildcards only."""
    path_tokens = path.split(".")
    pattern_tokens = pattern.split(".")
    if len(path_tokens) != len(pattern_tokens):
        return False
    return all(pattern_token == "*" or pattern_token == path_token
               for path_token, pattern_token in zip(path_tokens, pattern_tokens, strict=False))
