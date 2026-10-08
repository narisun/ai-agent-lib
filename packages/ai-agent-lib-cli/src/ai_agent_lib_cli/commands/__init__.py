"""The commands of ``agentlib``, by what they do.

``create`` writes, ``look`` reads and runs, ``rules`` tries the policy and
``check`` runs the gate a change passes and
``deploy`` writes what runs a service in AWS. The
command line itself, which puts them together, is :mod:`ai_agent_lib_cli.app`.
"""

from ai_agent_lib_cli.commands.check import check, evals
from ai_agent_lib_cli.commands.create import init, link, new, registry, update
from ai_agent_lib_cli.commands.deploy import deploy
from ai_agent_lib_cli.commands.look import config, doctor, graph, run
from ai_agent_lib_cli.commands.rules import policy

__all__ = [
    "check",
    "config",
    "deploy",
    "doctor",
    "evals",
    "graph",
    "init",
    "link",
    "new",
    "policy",
    "registry",
    "run",
    "update",
]
