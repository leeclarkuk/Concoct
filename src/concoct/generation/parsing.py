"""Robust extraction of JSON payloads from model replies."""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ValidationError


class ResponseParseError(ValueError):
    """The model reply could not be turned into the expected structure."""


def extract_json(text: str) -> dict[str, Any]:
    """Return the first JSON object in ``text``.

    Structured-output replies are plain JSON; the fallbacks handle replies
    wrapped in Markdown fences or surrounded by prose.
    """
    stripped = text.strip()
    if not stripped:
        raise ResponseParseError("empty reply")
    try:
        value = json.loads(stripped)
    except json.JSONDecodeError:
        value = None
    if isinstance(value, dict):
        return value

    decoder = json.JSONDecoder()
    index = stripped.find("{")
    while index != -1:
        try:
            value, _ = decoder.raw_decode(stripped, index)
        except json.JSONDecodeError:
            index = stripped.find("{", index + 1)
            continue
        if isinstance(value, dict):
            return value
        index = stripped.find("{", index + 1)
    raise ResponseParseError("no JSON object found in reply")


def parse_model[T: BaseModel](text: str, model: type[T]) -> T:
    data = extract_json(text)
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        raise ResponseParseError(_summarise(exc)) from exc


def _summarise(exc: ValidationError, limit: int = 8) -> str:
    parts = []
    for error in exc.errors()[:limit]:
        location = ".".join(str(p) for p in error["loc"]) or "<root>"
        parts.append(f"{location}: {error['msg']}")
    return "; ".join(parts)
