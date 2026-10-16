"""Run a local Python module with a cooperative Windows interrupt fallback.

Uvicorn restores and replays SIGBREAK after completing its lifespan. Python's
default Windows handler terminates with exit 3 at that point. Use the normal
KeyboardInterrupt handler instead, so the caller can finish cleanup. Actual
application failures keep their exit codes; the supervisor still rejects them.
"""

import runpy
import signal
import sys
from pathlib import Path


def main() -> int:
    """Preserve module arguments and normalize only a handled keyboard interrupt."""
    module, *arguments = sys.argv[1:]
    sys.argv = [module, *arguments]
    sys.path.insert(0, str(Path.cwd()))  # Match python -m lookup from the service folder.
    windows_break = getattr(signal, "SIGBREAK", None)
    if windows_break is not None and signal.getsignal(windows_break) == signal.SIG_DFL:
        signal.signal(windows_break, signal.default_int_handler)
    try:
        runpy.run_module(module, run_name="__main__", alter_sys=True)
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
