"""The data options of ``new mcp``: which reader they name, and running it."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from typing import Any

import click

from ai_agent_lib_cli.errors import CliError
from ai_agent_lib_cli.read_redshift import RedshiftRequest
from ai_agent_lib_cli.readers import Proposal, from_csv, from_openapi, from_redshift
from ai_agent_lib_cli.toolbox import Toolbox

__all__ = ["print_proposal", "proposal_for", "redshift_options"]

_READERS = {
    "csv_folder": "--from-csv",
    "openapi": "--from-openapi",
    "redshift_schema": "--from-redshift",
}
_REDSHIFT_ONLY = (
    "database",
    "workgroup",
    "cluster",
    "db_user",
    "secret_arn",
    "tables",
    "aws_profile",
    "aws_region",
)
_OPTION_NAMES = {"tables": "--table", "only": "--query"}


def redshift_options(command: Callable[..., Any]) -> Callable[..., Any]:
    """Add the options that say which Redshift database to read, and as whom."""
    for decorator in reversed(
        (
            click.option("--database", default=None, help="Redshift: the database."),
            click.option("--workgroup", default=None, help="Redshift: a Serverless workgroup."),
            click.option("--cluster", default=None, help="Redshift: a provisioned cluster."),
            click.option("--db-user", default=None, help="Redshift: the user, on a cluster."),
            click.option("--secret-arn", default=None, help="Redshift: a secret with credentials."),
            click.option(
                "--table", "tables", multiple=True, help="Redshift: read only this table."
            ),
            click.option("--aws-profile", default=None, help="Redshift: the AWS profile to use."),
            click.option("--aws-region", default=None, help="Redshift: the AWS region."),
        )
    ):
        command = decorator(command)
    return command


def proposal_for(tools: Toolbox, default_source: str, given: Mapping[str, Any]) -> Proposal | None:
    """Run the reader the data options name, if they name one."""
    chosen = [key for key in _READERS if given[key] is not None]
    if len(chosen) > 1:
        raise CliError(f"give only one of {', '.join(_READERS.values())}")
    source = given["source"] or default_source
    only, per_table = given["only"], given["per_table"]
    reader = chosen[0] if chosen else None
    stray = [key for key in _REDSHIFT_ONLY if given[key]] if reader != "redshift_schema" else []
    if given["base_url"] and reader != "openapi":
        stray.append("base_url")
    if reader is None:
        stray += [key for key in ("source", "only") if given[key]]
    if stray:
        raise CliError(
            "these options belong to a data option such as --from-csv, which was not given: "
            + ", ".join(_OPTION_NAMES.get(key, f"--{key.replace('_', '-')}") for key in stray)
        )
    if reader == "csv_folder":
        return asyncio.run(
            from_csv(given["csv_folder"], source=source, per_table=per_table, only=only)
        )
    if reader == "openapi":
        return asyncio.run(
            from_openapi(given["openapi"], source=source, base_url=given["base_url"], only=only)
        )
    if reader == "redshift_schema":
        if not given["database"]:
            raise CliError("--from-redshift needs --database, and --workgroup or --cluster")
        request = RedshiftRequest(
            schema=given["redshift_schema"],
            database=given["database"],
            workgroup=given["workgroup"],
            cluster_id=given["cluster"],
            db_user=given["db_user"],
            secret_arn=given["secret_arn"],
            profile=given["aws_profile"],
            region=given["aws_region"],
            tables=tuple(given["tables"]),
        )
        found = asyncio.run(tools.fetch_catalog(request))
        return asyncio.run(
            from_redshift(found, request, source=source, per_table=per_table, only=only)
        )
    return None


def print_proposal(server_id: str, proposal: Proposal) -> None:
    """Print what a reader proposes, and what it left out."""
    click.echo(proposal.summary)
    click.echo("Proposed tools:")
    for query in proposal.plan.queries:
        masked = f"   masks {', '.join(query.masked)} for analysts" if query.masked else ""
        click.echo(f"  {server_id}.{query.name}({query.signature}){masked}")
        click.echo(f"      {query.description}")
    for note in proposal.notes:
        click.echo(f"note: {note}", err=True)
