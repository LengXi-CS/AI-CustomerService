"""FastGPT Enterprise Assistant Operational CLI."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from fastgpt_assistant.core.config import settings


def _run(cmd: list[str], cwd: Path | None = None) -> int:
    print(f"--> Running: {' '.join(cmd)}")
    use_shell = sys.platform == "win32"
    command = " ".join(cmd) if use_shell else cmd
    return subprocess.run(command, cwd=str(cwd or Path.cwd()), shell=use_shell).returncode



def cmd_test(_args: argparse.Namespace) -> int:
    """Run pytest suite."""
    return _run(["uv", "run", "--with", "pytest", "pytest"])


def cmd_lint(args: argparse.Namespace) -> int:
    """Run ruff lint and format check."""
    extra = ["--fix"] if args.fix else []
    return _run(["uv", "run", "--with", "ruff", "ruff", "check", "src", "tests", "scripts", *extra])


def cmd_sync_bitable(args: argparse.Namespace) -> int:
    """Sync records fresh from Feishu Bitable OpenAPI."""
    cmd = ["uv", "run", "python", "scripts/sync_bitable.py"]
    if args.no_download:
        cmd.append("--no-download")
    return _run(cmd)


def cmd_start_fastgpt(_args: argparse.Namespace) -> int:
    """Start local FastGPT docker stack."""
    compose_dir = Path("deploy/fastgpt")
    return _run(
        ["docker", "compose", "-f", "docker-compose.yml", "up", "-d"],
        cwd=compose_dir,
    )


def cmd_stop_fastgpt(_args: argparse.Namespace) -> int:
    """Stop local FastGPT docker stack."""
    compose_dir = Path("deploy/fastgpt")
    return _run(
        ["docker", "compose", "-f", "docker-compose.yml", "down"],
        cwd=compose_dir,
    )


def cmd_import_docs(args: argparse.Namespace) -> int:
    """Import official markdown docs into FastGPT dataset."""
    cmd = [
        "uv",
        "run",
        "python",
        "scripts/import_fastgpt_docs.py",
        "--fastgpt-url",
        args.fastgpt_url or settings.fastgpt_api_url,
        "--root-key",
        args.root_key or settings.fastgpt_root_key,
    ]
    return _run(cmd)


def cmd_serve(args: argparse.Namespace) -> int:
    """Start FastAPI webhook and query server."""
    host = args.host or settings.host
    port = args.port or settings.port
    return _run(
        [
            "uv",
            "run",
            "uvicorn",
            "fastgpt_assistant.server.app:app",
            "--host",
            host,
            "--port",
            str(port),
            "--reload",
        ]
    )


def cmd_query(args: argparse.Namespace) -> int:
    """Query assistant directly via CLI."""
    from fastgpt_assistant.server.app import DirectQueryRequest, direct_query

    req = DirectQueryRequest(query=args.question, user_id="cli_user")
    res = direct_query(req)
    print("\n===============================")
    print(f"Channel: {res.channel}")
    print(f"Answer:\n{res.answer}")
    if res.deliverables:
        print("\nDeliverables:")
        for d in res.deliverables:
            print(f" - [{d['media_type']}] {d['file_name']} -> {d['file_path']}")
    print("===============================\n")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="FastGPT Enterprise Assistant Ops CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # test
    subparsers.add_parser("test", help="Run standard pytest suite")

    # lint
    p_lint = subparsers.add_parser("lint", help="Run ruff linting")
    p_lint.add_argument("--fix", action="store_true", help="Auto fix fixable issues")

    # sync-bitable
    p_sync = subparsers.add_parser("sync-bitable", help="Sync Bitable data from Feishu")
    p_sync.add_argument("--no-download", action="store_true", help="Skip downloading media files")

    # docker compose
    subparsers.add_parser("start-fastgpt", help="Start FastGPT Docker Compose stack")
    subparsers.add_parser("stop-fastgpt", help="Stop FastGPT Docker Compose stack")

    # import-docs
    p_docs = subparsers.add_parser("import-docs", help="Upload website docs to FastGPT")
    p_docs.add_argument("--fastgpt-url", default="", help="FastGPT base URL")
    p_docs.add_argument("--root-key", default="", help="FastGPT root key")

    # serve
    p_serve = subparsers.add_parser("serve", help="Start FastAPI server")
    p_serve.add_argument("--host", default="", help="Bind host")
    p_serve.add_argument("--port", type=int, default=0, help="Bind port")

    # query
    p_query = subparsers.add_parser("query", help="Direct test query")
    p_query.add_argument("question", help="Question to ask")

    args = parser.parse_args()
    handlers = {
        "test": cmd_test,
        "lint": cmd_lint,
        "sync-bitable": cmd_sync_bitable,
        "start-fastgpt": cmd_start_fastgpt,
        "stop-fastgpt": cmd_stop_fastgpt,
        "import-docs": cmd_import_docs,
        "serve": cmd_serve,
        "query": cmd_query,
    }

    ret = handlers[args.command](args)
    sys.exit(ret)


if __name__ == "__main__":
    main()
