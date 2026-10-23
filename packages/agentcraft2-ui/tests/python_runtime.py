"""Run child test processes with the same installed UI packages as pytest."""

import sys
from importlib.metadata import distribution


def python_command(*arguments: str) -> list[str]:
    """Support both ordinary installations and dependency-only pip target folders."""
    packages = str(distribution("agentcraft2-ui").locate_file(""))
    bootstrap = (
        "import site, sys, runpy; "
        f"site.addsitedir({packages!r}); "
        "mode = sys.argv.pop(1); target = sys.argv.pop(1); "
        "sys.argv[0] = target; "
        "runpy.run_module(target, run_name='__main__', alter_sys=True) "
        "if mode == '-m' else exec(compile(target, '<test>', 'exec'))"
    )
    return [sys.executable, "-I", "-S", "-c", bootstrap, *arguments]
