#!/usr/bin/env python3
"""Cross-platform operational wrapper for FastGPT Enterprise Assistant."""

import sys
from pathlib import Path

# Add src to sys.path
root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(root / "src"))

from fastgpt_assistant.cli import main  # noqa: E402

if __name__ == "__main__":
    main()
