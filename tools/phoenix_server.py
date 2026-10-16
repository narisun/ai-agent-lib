"""Trace-viewer entry point for the pinned local Phoenix trial.

Phoenix 20.22.0 binds its optional gRPC collector to every interface, even when
the web host is loopback. Our exporter uses HTTP, so disable that collector at
its lifecycle boundary. Its optional Bedrock dependency check also needs to
respect the provider allow-list. Keep these shims covered when upgrading;
neither changes database queries, warning filters, or installed packages.
"""

from functools import wraps
from importlib import import_module
from importlib.metadata import version
from typing import Any


def http_only(grpc_server: Any) -> None:
    """Skip only the unused gRPC listener; HTTP ingestion remains enabled."""
    enter = grpc_server.__aenter__

    async def disabled_enter(self: Any) -> None:
        self._disabled = True
        await enter(self)

    grpc_server.__aenter__ = disabled_enter


def check_enabled_playground_dependencies(app: Any) -> None:
    """Check Bedrock dependencies only when Phoenix permits the AWS provider.

    Phoenix 20.22.0 runs this check even with PHOENIX_ALLOWED_PROVIDERS=NONE.
    Preserve the original check, including its warnings and exceptions, whenever
    AWS is allowed. Trace ingestion never needs a Playground model client.
    """
    check = app._warn_if_missing_aioboto3

    @wraps(check)
    def check_when_enabled() -> None:
        providers = app.get_env_allowed_providers()
        if providers is None or "AWS" in providers:
            check()

    app._warn_if_missing_aioboto3 = check_when_enabled


def main() -> None:
    """Run the packaged UI with the reviewed collector lifecycle."""
    if version("arize-phoenix") != "20.22.0":
        raise RuntimeError("Run start-phoenix.bat setup to install the verified Phoenix version.")
    http_only(import_module("phoenix.server.grpc_server").GrpcServer)
    check_enabled_playground_dependencies(import_module("phoenix.server.app"))
    import_module("phoenix.server.main").main()


if __name__ == "__main__":
    main()
