"""Run the loopback-only Phase 0 sample with Python."""

import argparse
from pathlib import Path

import uvicorn

from spike.server import create_app
from spike.settings import Settings


def main() -> None:
    """Start the isolated sample; agent and application keys stay in local state."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, help="private state directory (default: .agentlib)")
    args = parser.parse_args()
    settings = Settings.local(args.state)
    app = create_app(settings)
    uvicorn.run(app, host="127.0.0.1", port=8765, access_log=False, ws="wsproto")


if __name__ == "__main__":
    main()
