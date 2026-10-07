"""Print generated configuration documents.

Usage::

    python -m ai_agent_lib_core.config reference > docs/variables.md
    python -m ai_agent_lib_core.config env-example > .env.example
"""

from __future__ import annotations

import sys

from ai_agent_lib_core.config.reference import render_env_example, render_reference

_RENDERERS = {"reference": render_reference, "env-example": render_env_example}


def main(argv: list[str]) -> int:
    """Write the requested document to standard output."""
    if len(argv) != 1 or argv[0] not in _RENDERERS:
        sys.stderr.write(f"usage: python -m ai_agent_lib_core.config {{{'|'.join(_RENDERERS)}}}\n")
        return 2
    sys.stdout.write(_RENDERERS[argv[0]]())
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
