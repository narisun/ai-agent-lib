"""Load pinned prompt versions from package resources, independent of the working directory."""

import re
from importlib.resources import files


def load_prompt(agent: str, prompt_type: str, version: int) -> str:
    """Read prompts/<agent>/<type>.v<version>.md; fail early for a missing version."""
    if (
        not re.fullmatch(r"[a-z][a-z0-9_-]*", agent)
        or not re.fullmatch(r"[a-z][a-z0-9_-]*", prompt_type)
        or isinstance(version, bool)
        or version < 1
    ):
        raise ValueError("Use an agent name, prompt type, and positive integer version")
    return (
        files(__package__)
        .joinpath(agent, f"{prompt_type}.v{version}.md")
        .read_text(encoding="utf-8")
    )


# Pin each resource explicitly. A new file does not silently change a running agent.
COMMON = load_prompt("rmprep", "common", 3)
MEMO_PROMPT = COMMON + load_prompt("memoagent", "system", 2)
QNA_PROMPT = COMMON + load_prompt("qnaagent", "system", 3)
