"""In-process syntax checks for individual files."""

from __future__ import annotations

import ast
import json
import tomllib
import warnings

import yaml


def check_syntax(path: str, content: str) -> str | None:
    """Return an error description if ``content`` does not parse, else ``None``."""
    lower = path.lower()
    try:
        if lower.endswith((".py", ".pyi")):
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", SyntaxWarning)
                ast.parse(content, filename=path)
        elif lower.endswith(".json") and not lower.endswith(("tsconfig.json", "jsconfig.json")):
            # tsconfig permits comments/trailing commas, so it is not strict JSON.
            json.loads(content)
        elif lower.endswith(".toml"):
            tomllib.loads(content)
        elif lower.endswith((".yml", ".yaml")):
            list(yaml.safe_load_all(content))
    except SyntaxError as exc:
        return f"{path}:{exc.lineno}: {exc.msg}"
    except json.JSONDecodeError as exc:
        return f"{path}:{exc.lineno}: invalid JSON: {exc.msg}"
    except tomllib.TOMLDecodeError as exc:
        return f"{path}: invalid TOML: {exc}"
    except yaml.YAMLError as exc:
        return f"{path}: invalid YAML: {exc}"
    return None
