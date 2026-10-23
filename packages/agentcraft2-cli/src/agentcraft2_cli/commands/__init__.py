"""The commands of ``agentcraft2``, by what they do.

``create`` writes, ``look`` reads and runs, ``rules`` tries the policy and
``check`` runs the gate a change passes and
``deploy`` writes what runs a service in AWS. The
command line itself, which puts them together, is :mod:`agentcraft2_cli.app`.
"""

from agentcraft2_cli.commands.check import check, evals
from agentcraft2_cli.commands.create import init, link, new, registry, update
from agentcraft2_cli.commands.deploy import deploy
from agentcraft2_cli.commands.install import install
from agentcraft2_cli.commands.look import config, doctor, graph, run
from agentcraft2_cli.commands.rules import policy

__all__ = [
    "check",
    "config",
    "deploy",
    "doctor",
    "evals",
    "graph",
    "init",
    "install",
    "link",
    "new",
    "policy",
    "registry",
    "run",
    "update",
]
