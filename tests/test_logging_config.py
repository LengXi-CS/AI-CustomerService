"""Tests for application logging configuration.

Regression guard for the 2026-09-18 incident: ``server/app.py`` only called
``logger.setLevel(logging.INFO)`` without ever installing a root handler, so every
``logger.info`` in this package was swallowed by ``logging.lastResort`` (WARNING+).
The attachment dispatch diagnostics were therefore invisible in ``logs/5330.err.log``
even though the code emitting them ran fine.

The behavioural tests below run in a *fresh interpreter* on purpose. pytest's own
logging plugin attaches capture handlers to the root logger, so in-process tests
cannot faithfully reproduce "no root handler at all". A subprocess can.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

from fastgpt_assistant.server import app as app_module

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"


def _run_probe(code: str, **env_overrides: str) -> str:
    """Run ``code`` in a clean interpreter and return its combined output."""
    # ``python -c`` needs the snippet to start at column 0.
    snippet = textwrap.dedent(code).strip()
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC_ROOT)
    env.pop("LOG_LEVEL", None)
    env.pop("EA_LOG_LEVEL", None)
    env.update(env_overrides)
    completed = subprocess.run(
        [sys.executable, "-c", snippet],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=PROJECT_ROOT,
        env=env,
        timeout=120,
    )
    assert completed.returncode == 0, completed.stderr
    return f"{completed.stdout}\n{completed.stderr}"


def test_resolve_log_level_accepts_names_case_insensitively() -> None:
    assert app_module.resolve_log_level("debug") == app_module.logging.DEBUG
    assert app_module.resolve_log_level(" Info ") == app_module.logging.INFO
    assert app_module.resolve_log_level("WARNING") == app_module.logging.WARNING


def test_resolve_log_level_passes_through_numbers() -> None:
    assert app_module.resolve_log_level(app_module.logging.ERROR) == app_module.logging.ERROR


def test_resolve_log_level_falls_back_to_info() -> None:
    """A typo in LOG_LEVEL must never crash the service on boot."""
    assert app_module.resolve_log_level("chatty") == app_module.logging.INFO
    assert app_module.resolve_log_level("") == app_module.logging.INFO
    assert app_module.resolve_log_level(None) == app_module.logging.INFO


def test_resolve_log_level_treats_bool_as_info() -> None:
    # ``isinstance(True, int)`` is True in Python, so bools need an explicit guard.
    assert app_module.resolve_log_level(True) == app_module.logging.INFO


def test_info_logs_are_invisible_before_and_visible_after_configuration() -> None:
    """The exact production failure, and proof that it is fixed."""
    output = _run_probe(
        """
        import logging
        from fastgpt_assistant.server.app import configure_logging

        log = logging.getLogger("fastgpt_assistant")
        log.info("PROBE-BEFORE")
        configure_logging("INFO")
        log.info("PROBE-AFTER")
        """
    )

    assert "PROBE-BEFORE" not in output
    assert "PROBE-AFTER" in output


def test_configuring_logging_twice_does_not_duplicate_the_handler() -> None:
    """A second call must not print every line twice."""
    output = _run_probe(
        """
        from fastgpt_assistant.server.app import configure_logging
        import logging

        configure_logging("INFO")
        configure_logging("INFO")
        logging.getLogger("fastgpt_assistant").info("PROBE-ONCE")
        """
    )

    assert output.count("PROBE-ONCE") == 1


def test_log_level_setting_is_actually_consumed() -> None:
    """``settings.log_level`` used to be declared but never read anywhere."""
    output = _run_probe(
        """
        from fastgpt_assistant.server.app import configure_logging
        import logging

        configure_logging()
        logging.getLogger("fastgpt_assistant").debug("PROBE-DEBUG")
        """,
        LOG_LEVEL="DEBUG",
    )

    assert "PROBE-DEBUG" in output


def test_default_level_hides_debug_records() -> None:
    output = _run_probe(
        """
        from fastgpt_assistant.server.app import configure_logging
        import logging

        configure_logging()
        logging.getLogger("fastgpt_assistant").debug("PROBE-DEBUG")
        """
    )

    assert "PROBE-DEBUG" not in output


def test_configuration_leaves_uvicorn_loggers_alone() -> None:
    """uvicorn owns its own handlers; we must not hijack or duplicate them."""
    output = _run_probe(
        """
        import logging
        from fastgpt_assistant.server.app import configure_logging

        configure_logging("INFO")
        print("uvicorn-handlers", len(logging.getLogger("uvicorn.error").handlers))
        print("package-handlers", len(logging.getLogger("fastgpt_assistant").handlers))
        print("root-handlers", len(logging.getLogger().handlers))
        """
    )

    assert "uvicorn-handlers 0" in output
    assert "package-handlers 0" in output
    assert "root-handlers 1" in output
