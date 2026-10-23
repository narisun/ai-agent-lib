"""Print model trace records as readable JSON, optionally selecting recent events."""

from __future__ import annotations

import argparse
import json
import sys
from collections import deque
from pathlib import Path


def main() -> None:
    """Read a JSONL file without changing it; report file and JSON errors clearly."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path, help="path to the model trace JSONL file")
    parser.add_argument("--last", type=int, help="show only the last N events")
    args = parser.parse_args()
    if args.last is not None and args.last < 1:
        parser.error("--last must be at least 1")
    try:
        with args.path.open(encoding="utf-8") as source:
            lines = deque(source, maxlen=args.last) if args.last is not None else source
            for line in lines:
                # Escaping non-ASCII characters also works in older Command Prompt consoles.
                sys.stdout.write(json.dumps(json.loads(line), indent=2, ensure_ascii=True) + "\n")
    except (OSError, ValueError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
