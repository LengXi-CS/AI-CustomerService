"""CLI script to sync Bitable tables from Lark and build the SQLite read model."""

import argparse
import json
import logging
import sys
from pathlib import Path

from fastgpt_assistant.core.bitable_sync import BitableSyncManager, LarkBitableClient
from fastgpt_assistant.core.config import Settings


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync Feishu Bitable to SQLite read model")
    parser.add_argument(
        "--db-path",
        default="data/read-model.db",
        help="Target SQLite database path (default: data/read-model.db)",
    )
    parser.add_argument(
        "--attachment-dir",
        default="data/resources/attachments",
        help="Attachment store directory (default: data/resources/attachments)",
    )
    parser.add_argument(
        "--no-download",
        action="store_true",
        help="Skip downloading attachments (only index metadata)",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable verbose logging",
    )
    args = parser.parse_args()

    configuration = Settings(_env_file=Path(__file__).resolve().parent.parent / ".env")

    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(level=log_level, format="%(asctime)s [%(levelname)s] %(message)s")

    app_id = configuration.lark_app_id
    app_secret = configuration.lark_app_secret
    app_token = configuration.lark_bitable_app_token

    if not app_id or not app_secret or not app_token:
        print(
            "Error: Missing required environment variables: "
            "LARK_APP_ID, LARK_APP_SECRET, LARK_BITABLE_APP_TOKEN (legacy EA_ names also accepted)",
            file=sys.stderr,
        )
        return 1

    project_root = Path(__file__).resolve().parent.parent
    db_path = project_root / args.db_path
    attachment_dir = project_root / args.attachment_dir

    print("Initializing Lark Bitable client...")
    with LarkBitableClient(app_id=app_id, app_secret=app_secret) as client:
        manager = BitableSyncManager(
            client=client,
            app_token=app_token,
            database_path=db_path,
            attachment_dir=attachment_dir,
        )
        print("Starting synchronization...")
        summary = manager.sync(download_attachments=not args.no_download)

    print("\nSync completed successfully:")
    print(json.dumps(summary.to_dict(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
