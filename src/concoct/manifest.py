"""Per-repository manifest, stored inside ``.git`` so it never enters history."""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import ValidationError

from concoct.models import Manifest

MANIFEST_RELATIVE = Path(".git") / "concoct" / "manifest.json"


def manifest_path(repo_path: Path) -> Path:
    return Path(repo_path) / MANIFEST_RELATIVE


def write_manifest(repo_path: Path, manifest: Manifest) -> Path:
    path = manifest_path(repo_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(manifest.model_dump_json(indent=2), encoding="utf-8")
    tmp.replace(path)
    return path


def read_manifest(repo_path: Path) -> Manifest | None:
    path = manifest_path(repo_path)
    if not path.is_file():
        return None
    try:
        return Manifest.model_validate(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError, ValidationError):
        return None


def find_local_repositories(output_dir: Path) -> list[tuple[Path, Manifest]]:
    """Concoct-generated repositories directly under ``output_dir``."""
    output_dir = Path(output_dir)
    if not output_dir.is_dir():
        return []
    found = []
    for child in sorted(output_dir.iterdir()):
        if child.is_dir() and (manifest := read_manifest(child)) is not None:
            found.append((child, manifest))
    return found
