from __future__ import annotations

import logging
from pathlib import Path

from concoct.logging import configure_logging, get_logger
from concoct.secrets import REDACTED, redact, registry, scan_text


def test_redacts_registered_values_and_known_patterns() -> None:
    registry.register("super-secret-value-123")
    text = "key=super-secret-value-123 gh=ghp_" + "a" * 36 + " ant=sk-ant-" + "b" * 30
    cleaned = redact(text)
    assert "super-secret" not in cleaned
    assert "ghp_" not in cleaned
    assert "sk-ant-" not in cleaned
    assert cleaned.count(REDACTED) == 3


def test_short_values_are_not_registered() -> None:
    registry.register("abc")
    assert registry.values() == []


def test_scan_text_reports_line_and_kind() -> None:
    findings = scan_text("config.py", 'ok = 1\nAWS = "AKIAABCDEFGHIJKLMNOP"\n')
    assert [(f.line, f.kind) for f in findings] == [(2, "aws_access_key")]


def test_scan_text_detects_configured_secret() -> None:
    registry.register("my-private-token-value")
    findings = scan_text("x.txt", "token: my-private-token-value")
    assert any(f.kind == "configured_secret" for f in findings)


def test_scan_text_clean() -> None:
    assert scan_text("x.py", "API_KEY = os.environ['API_KEY']\n") == []


def test_log_output_is_redacted(tmp_path: Path) -> None:
    registry.register("hunter2-hunter2-hunter2")
    log_file = tmp_path / "concoct.log"
    configure_logging("DEBUG", log_file)
    get_logger("test").warning("using token %s", "hunter2-hunter2-hunter2")
    for handler in logging.getLogger("concoct").handlers:
        handler.flush()
    content = log_file.read_text()
    assert "hunter2" not in content
    assert REDACTED in content
    configure_logging("WARNING")
