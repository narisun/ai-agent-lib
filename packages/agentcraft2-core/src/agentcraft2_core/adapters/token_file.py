"""The token a developer's own sign-in left in a file, for a server that runs elsewhere.

A local agent knows its caller by the development identity, which no deployed server
accepts. To call a server on AWS from a developer's machine, the developer signs in with
their own tooling (the corporate single sign-on, with its second factor) and leaves the
token in a file. The server then sees the real developer and the registered agent, which
is what it sees in production, and nothing changes on the server.

The file is read on every call, so a refreshed token is used at once, and its content is
never logged, repeated in an error or kept.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

from pydantic import SecretStr

from agentcraft2_core.contracts import ConfigurationError, RequestContext, describe

__all__ = ["TokenFileExchanger"]

# What a bearer token is made of. Anything else, a line break above all, would let the file's
# content write other headers.
_TOKEN = re.compile(r"[A-Za-z0-9._~+/=-]+")


class TokenFileExchanger:
    """Presents the token in a file as the credential for one server.

    It is the local-development counterpart of an exchanger that asks an issuer for a
    token bound to the server. It never contacts anything: what the file holds is what
    is presented, for the person and for the service alike.

    Args:
        path: The file that holds the token alone.
        server: The server it is for, named in errors.
    """

    def __init__(self, path: Path, server: str) -> None:
        self._path = path
        self._server = server

    @property
    def path(self) -> Path:
        """The file the token is read from."""
        return self._path

    async def exchange(self, context: RequestContext, audience: str) -> SecretStr:  # noqa: ARG002
        """Return the token in the file, whoever the caller is."""
        return await self._read(audience)

    async def service_token(self, audience: str) -> SecretStr:
        """Return the token in the file, for the calls the service makes in its own name."""
        return await self._read(audience)

    async def _read(self, audience: str) -> SecretStr:
        try:
            text = await asyncio.to_thread(self._path.read_text, encoding="utf-8")
        except FileNotFoundError:
            raise self._missing(audience) from None
        except (OSError, UnicodeDecodeError) as exc:
            raise ConfigurationError(
                f"the token file for MCP server {self._server!r} could not be read "
                f"({describe(exc)})",
                expected=f"a readable file at {self._path}",
                fix="check the file's permissions",
            ) from exc
        token = text.strip()
        if not token:
            raise ConfigurationError(
                f"the token file for MCP server {self._server!r} is empty",
                expected=f"a token for {audience} in {self._path}",
                fix="sign in again with your own tooling and write the token to the file",
            )
        if not _TOKEN.fullmatch(token):
            # What it holds is not repeated: it may be a credential of any kind.
            raise ConfigurationError(
                f"the token file for MCP server {self._server!r} does not hold a bearer token",
                expected=f"the token alone, on one line, in {self._path}",
                actual="text with characters a token does not have",
                fix="write only the token, without a scheme such as 'Bearer' or other text",
            )
        return SecretStr(token)

    def _missing(self, audience: str) -> ConfigurationError:
        return ConfigurationError(
            f"MCP server {self._server!r} runs elsewhere, and there is no token to present to it",
            expected=f"a token for {audience} in {self._path}",
            actual="no such file",
            fix=(
                f"sign in with your own tooling for the development client of this agent, with "
                f"{audience} as the audience, and write the token to {self._path}; it is read "
                "on every call, so a refreshed token is used at once"
            ),
        )
