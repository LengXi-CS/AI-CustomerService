"""Enable one trusted LAN for admin HTTP login, preserving the remaining .env."""

import argparse
from pathlib import Path

from fastgpt_assistant.core.admin_network import lan_networks
from fastgpt_assistant.core.dotenv_store import EnvironmentDocument, quote_value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--network", required=True)
    args = parser.parse_args()
    networks = lan_networks(args.network)
    if len(networks) != 1:
        parser.error("Specify one private IPv4 subnet")
    root = Path(__file__).resolve().parent.parent
    document = EnvironmentDocument(root / ".env", root / "data/admin/reply-backups")
    with document.lock:
        original = document.read()
        candidate = document.render(
            original,
            {
                "admin_lan_http_networks": quote_value(str(networks[0])),
            },
        )
        document.commit(original, candidate)
    print(f"Admin LAN HTTP network: {networks[0]}")


if __name__ == "__main__":
    main()
