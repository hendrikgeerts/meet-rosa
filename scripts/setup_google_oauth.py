#!/usr/bin/env python3
"""One-shot script to complete Google OAuth consent. Run once after placing
google_credentials.json in the repo root. Opens your browser, you click Allow,
token lands at data/google_token.json.

Use --force to delete an existing token first (nodig na scope-reductie:
de oude bredere scopes blijven anders actief)."""
import argparse
import logging
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from integrations.google_auth import get_credentials  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true",
                        help="delete existing token before consenting")
    args = parser.parse_args()

    creds_path = REPO_ROOT / "google_credentials.json"
    token_path = REPO_ROOT / "data" / "google_token.json"
    token_path.parent.mkdir(parents=True, exist_ok=True)
    if args.force and token_path.exists():
        print(f"deleting existing token at {token_path}")
        token_path.unlink()
    creds = get_credentials(creds_path, token_path)
    print(f"OK — token stored at {token_path}")
    print(f"Scopes granted: {creds.scopes}")


if __name__ == "__main__":
    main()
