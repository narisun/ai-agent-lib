"""HTTP-only entry point for the pinned local Phoenix trial.

Phoenix 20.22.0 binds its optional gRPC collector to every interface, even when
the web host is loopback. Our exporter uses HTTP, so disable that collector at
its lifecycle boundary. Keep this compatibility shim covered when upgrading.
"""

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


def main() -> None:
    """Run the packaged UI with the reviewed collector lifecycle."""
    if version("arize-phoenix") != "20.22.0":
        raise RuntimeError("Run start-phoenix.bat setup to install the verified Phoenix version.")
    http_only(import_module("phoenix.server.grpc_server").GrpcServer)
    import_module("phoenix.server.main").main()


if __name__ == "__main__":
    main()
