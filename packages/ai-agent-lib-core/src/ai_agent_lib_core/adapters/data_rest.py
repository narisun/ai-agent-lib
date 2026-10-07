"""A data source over an existing REST API.

Each named query is an endpoint definition, a YAML file whose name is the
query name::

    description: Accounts in one region.
    method: GET
    path: /v1/regions/{region}/accounts
    parameters:
      region: {type: string, in: path}
      min_balance: {type: number, in: query, default: 0}
    max_rows: 100
    classification: restricted
    rows: data.items
    columns:
      account_id: id
      holder: owner.name
      balance: balance
    as_of: data.as_of

A caller supplies only parameter values. The host, the path and the fields
that are returned are fixed by the definition, so a model can neither reach
another address nor read a field the definition does not list.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal
from urllib.parse import quote

import httpx
from pydantic import ConfigDict, Field, SecretStr, ValidationError

from ai_agent_lib_core.adapters.http_support import checked_base_url, tls_verification
from ai_agent_lib_core.adapters.obligations import (
    check_filter_columns,
    effective_row_cap,
    filter_rows,
    mask_rows,
)
from ai_agent_lib_core.adapters.parameters import bind_parameters, coerce_parameter
from ai_agent_lib_core.adapters.strict_yaml import StrictYamlError, load_strict_yaml
from ai_agent_lib_core.contracts import (
    AgentLibError,
    Classification,
    Clock,
    ConfigurationError,
    CredentialsExpiredError,
    Obligations,
    OptionsModel,
    ParameterType,
    PolicyDenied,
    QueryDescription,
    QueryParameter,
    QueryResult,
    SourceMetadata,
    TransientError,
    ValidationFailed,
)

__all__ = ["RestDataSource", "RestOptions"]

_NAME = re.compile(r"^[a-z][a-z0-9_]*$")
_PLACEHOLDER = re.compile(r"\{([^{}]*)\}")
_TRANSIENT_STATUS = frozenset({408, 425, 429})
_FIRST_DELAY_SECONDS, _MAX_DELAY_SECONDS = 0.2, 2.0

Sleep = Callable[[float], Awaitable[None]]


class RestOptions(OptionsModel):
    """Options of the ``rest`` data source.

    Attributes:
        base_url: Where the API is. Every endpoint path is added to it.
        queries_dir: Directory of endpoint definitions.
        timeout_seconds: How long one request may take.
        max_retries: Extra attempts for a ``GET`` that failed for a passing reason.
        ca_file: A CA file for this API, when it is signed by a private authority.
        auth_secret: The name of the secret that holds the API's token, if any.
        auth_header: The header the token is sent in.
        auth_scheme: The word before the token. Empty sends the token alone.
        use_proxy: Send requests through the platform's outbound proxy.
        allow_http: Allow a ``base_url`` without TLS on a host other than this machine.
        max_response_bytes: The largest response that is read.
    """

    base_url: str
    queries_dir: Path
    timeout_seconds: float = Field(default=10.0, gt=0)
    max_retries: int = Field(default=2, ge=0, le=5)
    ca_file: Path | None = None
    auth_secret: str | None = None
    auth_header: str = "Authorization"
    auth_scheme: str = "Bearer"
    use_proxy: bool = False
    allow_http: bool = False
    max_response_bytes: int = Field(default=5_000_000, gt=0)


class _ParameterSpec(OptionsModel):
    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    type: ParameterType
    location: Literal["path", "query", "body"] = Field(default="query", alias="in")
    default: Any = None


class _EndpointSpec(OptionsModel):
    description: str = Field(min_length=1)
    method: Literal["GET", "POST"] = "GET"
    path: str
    parameters: dict[str, _ParameterSpec] = Field(default_factory=dict)
    max_rows: int = Field(gt=0)
    classification: str = Classification.INTERNAL.name.lower()
    rows: str | None = None
    columns: dict[str, str] = Field(min_length=1)
    as_of: str | None = None
    empty_on_not_found: bool = False


@dataclass(frozen=True, slots=True)
class _Endpoint:
    description: QueryDescription
    method: str
    path: str
    locations: Mapping[str, str]
    rows: tuple[str, ...]
    columns: Mapping[str, tuple[str, ...]]
    as_of: tuple[str, ...]
    empty_on_not_found: bool


def _field_path(text: str | None) -> tuple[str, ...]:
    return tuple(part for part in (text or "").split(".") if part)


def _read_spec(path: Path, name: str) -> _EndpointSpec:
    try:
        return _EndpointSpec.model_validate(load_strict_yaml(path.read_text(encoding="utf-8")))
    except StrictYamlError as exc:
        raise ConfigurationError(f"query {name!r}: {exc}") from None
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(part) for part in error['loc']) or '<root>'}: {error['msg']}"
            for error in exc.errors(include_input=False, include_url=False)
        )
        raise ConfigurationError(f"query {name!r}: {problems}") from None


def _classification(name: str, text: str) -> Classification:
    try:
        return Classification[text.upper()]
    except KeyError:
        allowed = ", ".join(level.name.lower() for level in Classification)
        raise ConfigurationError(
            f"query {name!r}: classification must be one of: {allowed}"
        ) from None


def _parameter(name: str, parameter_name: str, declared: _ParameterSpec) -> QueryParameter:
    if not _NAME.match(parameter_name):
        raise ConfigurationError(
            f"query {name!r}: parameter name {parameter_name!r} must be "
            "lower-case letters, digits and underscores"
        )
    parameter = QueryParameter(name=parameter_name, type=declared.type, required=True)
    if "default" not in declared.model_fields_set or declared.default is None:
        return parameter
    try:
        default = coerce_parameter(parameter, declared.default)
    except ValidationFailed:
        raise ConfigurationError(
            f"query {name!r}: the default of parameter {parameter_name!r} "
            f"is not a {declared.type.value}"
        ) from None
    return QueryParameter(name=parameter_name, type=declared.type, required=False, default=default)


def _check_shape(name: str, spec: _EndpointSpec, parameters: Sequence[QueryParameter]) -> None:
    """Check that the path, the parameters and the columns fit together."""
    in_path = {key for key, declared in spec.parameters.items() if declared.location == "path"}
    if not spec.path.startswith("/") or "?" in spec.path or "#" in spec.path:
        raise ConfigurationError(
            f"query {name!r}: path must start with '/' and hold no query string or fragment"
        )
    if set(_PLACEHOLDER.findall(spec.path)) != in_path:
        raise ConfigurationError(
            f"query {name!r}: the placeholders in the path must be exactly the parameters "
            "declared with 'in: path'"
        )
    if any(not parameter.required for parameter in parameters if parameter.name in in_path):
        raise ConfigurationError(f"query {name!r}: a path parameter cannot have a default")
    if spec.method == "GET" and any(d.location == "body" for d in spec.parameters.values()):
        raise ConfigurationError(f"query {name!r}: a GET request cannot have body parameters")
    for column in spec.columns:
        if not _NAME.match(column):
            raise ConfigurationError(
                f"query {name!r}: column name {column!r} must be "
                "lower-case letters, digits and underscores"
            )


def _load_endpoint(path: Path) -> _Endpoint:
    name = path.stem
    if not _NAME.match(name):
        raise ConfigurationError(
            f"{path.name}: the file name is the query name and must be "
            "lower-case letters, digits and underscores"
        )
    spec = _read_spec(path, name)
    classification = _classification(name, spec.classification)
    parameters = [
        _parameter(name, parameter_name, declared)
        for parameter_name, declared in spec.parameters.items()
    ]
    _check_shape(name, spec, parameters)
    return _Endpoint(
        description=QueryDescription(
            name=name,
            description=spec.description,
            parameters=tuple(parameters),
            max_rows=spec.max_rows,
            classification=classification,
        ),
        method=spec.method,
        path=spec.path,
        locations=MappingProxyType({key: d.location for key, d in spec.parameters.items()}),
        rows=_field_path(spec.rows),
        columns=MappingProxyType({key: _field_path(value) for key, value in spec.columns.items()}),
        as_of=_field_path(spec.as_of),
        empty_on_not_found=spec.empty_on_not_found,
    )


def _text(value: object) -> str:
    """Return a bound parameter value as it is written in a URL."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, date | datetime):
        return value.isoformat()
    return str(value)


def _json_value(value: object) -> object:
    """Return a bound parameter value as it is written in a JSON body."""
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, date | datetime):
        return value.isoformat()
    return value


def _walk(document: object, path: Iterable[str]) -> object:
    """Follow ``path`` through nested objects. A missing step gives ``None``."""
    for step in path:
        if not isinstance(document, dict):
            return None
        document = document.get(step)
    return document


def _cell(value: object) -> object:
    """Keep scalars; write anything nested as compact JSON text."""
    if value is None or isinstance(value, str | int | float | bool):
        return value
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def _moment(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


class RestDataSource:
    """Runs named queries against a REST API.

    Row filters, the row cap and masks are applied to the response before
    anything is returned. The cap counts the records the endpoint sent; this
    adapter does not follow pagination.

    Args:
        name: The name of this data source.
        options: Where the API is and how to call it.
        clock: Stamps each result with the time it was fetched.
        token: The API's token, when it needs one.
        proxy: The outbound proxy to use when ``options.use_proxy`` is set.
        transport: Replaces the network, for tests.
        sleep: Waits between attempts. Replaced in tests.

    Raises:
        ConfigurationError: If the options are unsafe or inconsistent.
    """

    def __init__(
        self,
        name: str,
        options: RestOptions,
        clock: Clock,
        *,
        token: SecretStr | None = None,
        proxy: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._base = checked_base_url(
            f"data source {name!r}", options.base_url, allow_http=options.allow_http
        )
        if options.auth_secret is not None and token is None:
            raise ConfigurationError(f"data source {name!r}: the secret for its token is missing")
        if options.use_proxy and proxy is None:
            raise ConfigurationError(
                f"data source {name!r}: use_proxy is set but no outbound proxy is configured"
            )
        self._name = name
        self._options = options
        self._clock = clock
        self._token = token
        self._proxy = proxy if options.use_proxy else None
        self._transport = transport
        self._sleep = sleep
        self._endpoints: Mapping[str, _Endpoint] | None = None
        self._client: httpx.AsyncClient | None = None

    def __repr__(self) -> str:
        return f"RestDataSource(name={self._name!r}, base_url={self._base!r})"

    async def start(self) -> None:
        """Load the endpoint definitions and open the HTTP client.

        Raises:
            ConfigurationError: If a directory, a definition or the CA file is invalid.
        """
        if self._client is not None:
            raise RuntimeError("the data source is already started")
        directory = self._options.queries_dir
        if not directory.is_dir():
            raise ConfigurationError(f"data source {self._name!r}: query directory not found")
        endpoints = {}
        for path in sorted(directory.glob("*.yaml")):
            try:
                endpoint = _load_endpoint(path)
            except ConfigurationError as exc:
                raise ConfigurationError(f"data source {self._name!r}: {exc}") from None
            endpoints[endpoint.description.name] = endpoint
        self._client = httpx.AsyncClient(
            verify=tls_verification(f"data source {self._name!r}", self._options.ca_file),
            timeout=self._options.timeout_seconds,
            follow_redirects=False,
            # The environment is read by the configuration layer only.
            trust_env=False,
            proxy=self._proxy if self._transport is None else None,
            transport=self._transport,
            headers={"Accept": "application/json"},
        )
        self._endpoints = MappingProxyType(endpoints)

    def describe(self) -> Mapping[str, QueryDescription]:
        """Return the queries this source offers, by name."""
        endpoints, _ = self._started()
        return MappingProxyType({key: endpoint.description for key, endpoint in endpoints.items()})

    async def query(
        self,
        name: str,
        parameters: Mapping[str, object] | None = None,
        *,
        obligations: Obligations | None = None,
    ) -> QueryResult:
        """Call the endpoint behind the query called ``name``.

        Raises:
            ValidationFailed: If the query is unknown, a parameter is invalid or
                the API refused the request as malformed.
            PolicyDenied: If an obligation cannot be enforced or the API forbade the call.
            CredentialsExpiredError: If the API did not accept the token.
            TransientError: If the API timed out or was unavailable.
            AgentLibError: If the response was not what the definition declares.
        """
        endpoints, client = self._started()
        endpoint = endpoints.get(name)
        if endpoint is None:
            raise ValidationFailed("unknown query; a caller may only name a loaded query")
        bound = bind_parameters(endpoint.description, parameters)
        wanted = obligations if obligations is not None else Obligations()
        columns = tuple(endpoint.columns)
        # Checked before the call, so a request that would be refused is never sent.
        check_filter_columns(columns, wanted.row_filters)

        request = self._request(client, endpoint, bound)
        document = await self._send(client, endpoint, request)
        records = self._records(endpoint, document)
        rows = [
            tuple(_cell(_walk(record, path)) for path in endpoint.columns.values())
            for record in records
        ]
        rows = filter_rows(columns, rows, wanted.row_filters)
        cap = effective_row_cap(endpoint.description.max_rows, wanted)
        kept, masked = mask_rows(columns, rows[:cap], wanted.mask_columns)
        return QueryResult(
            columns=columns,
            rows=kept,
            truncated=len(rows) > cap,
            masked_columns=masked,
            source=SourceMetadata(
                source=self._name,
                retrieved_at=self._clock.now(),
                as_of=_moment(_walk(document, endpoint.as_of)) if endpoint.as_of else None,
                uri=f"datasource://{self._name}/{name}",
            ),
        )

    def _request(
        self, client: httpx.AsyncClient, endpoint: _Endpoint, bound: Mapping[str, object]
    ) -> httpx.Request:
        segments: dict[str, str] = {}
        query: dict[str, str] = {}
        body: dict[str, object] = {}
        for key, value in bound.items():
            if value is None:
                continue
            location = endpoint.locations[key]
            if location == "path":
                text = _text(value)
                if text in {"", ".", ".."}:
                    raise ValidationFailed(f"parameter {key!r} cannot be used in a path")
                segments[key] = quote(text, safe="")
            elif location == "query":
                query[key] = _text(value)
            else:
                body[key] = _json_value(value)
        path = _PLACEHOLDER.sub(lambda match: segments[match.group(1)], endpoint.path)
        headers = {}
        if self._token is not None:
            scheme = self._options.auth_scheme
            secret = self._token.get_secret_value()
            headers[self._options.auth_header] = f"{scheme} {secret}" if scheme else secret
        return client.build_request(
            endpoint.method,
            self._base + path,
            params=query or None,
            json=body if endpoint.method == "POST" else None,
            headers=headers,
        )

    async def _send(
        self, client: httpx.AsyncClient, endpoint: _Endpoint, request: httpx.Request
    ) -> object:
        # Only a GET is repeated: it cannot change anything on the other side.
        attempts = 1 + (self._options.max_retries if endpoint.method == "GET" else 0)
        delay = _FIRST_DELAY_SECONDS
        for attempt in range(1, attempts + 1):
            try:
                return await self._send_once(client, endpoint, request)
            except TransientError:
                if attempt == attempts:
                    raise
            await self._sleep(delay)
            delay = min(delay * 2, _MAX_DELAY_SECONDS)
        raise AssertionError("unreachable")  # pragma: no cover

    async def _send_once(
        self, client: httpx.AsyncClient, endpoint: _Endpoint, request: httpx.Request
    ) -> object:
        query = endpoint.description.name
        where = f"data source {self._name!r}, query {query!r}"
        try:
            response = await client.send(request, stream=True)
            try:
                status = response.status_code
                body = b""
                if httpx.codes.is_success(status):
                    body = await self._read(response, where)
            finally:
                await response.aclose()
        except httpx.TimeoutException:
            raise TransientError(f"{where}: the API did not answer in time") from None
        except httpx.TransportError as exc:
            raise TransientError(
                f"{where}: the API could not be reached ({type(exc).__name__})"
            ) from None

        if httpx.codes.is_success(status):
            try:
                return json.loads(body)
            except ValueError:
                raise AgentLibError(f"{where}: the response was not JSON") from None
        if status == httpx.codes.NOT_FOUND and endpoint.empty_on_not_found:
            return None
        if status in _TRANSIENT_STATUS or httpx.codes.is_server_error(status):
            raise TransientError(f"{where}: the API returned HTTP {status}")
        if status == httpx.codes.UNAUTHORIZED:
            raise CredentialsExpiredError(f"{where}: the API did not accept the credentials")
        if status == httpx.codes.FORBIDDEN:
            raise PolicyDenied(
                f"{where}: the API forbade this request", reason_code="upstream_forbidden"
            )
        if httpx.codes.is_redirect(status):
            raise AgentLibError(f"{where}: the API redirected, and redirects are not followed")
        if httpx.codes.is_client_error(status):
            raise ValidationFailed(f"{where}: the API refused the request with HTTP {status}")
        raise AgentLibError(f"{where}: the API returned HTTP {status}")

    async def _read(self, response: httpx.Response, where: str) -> bytes:
        limit = self._options.max_response_bytes
        chunks: list[bytes] = []
        size = 0
        async for chunk in response.aiter_bytes():
            size += len(chunk)
            if size > limit:
                raise AgentLibError(f"{where}: the response is larger than {limit} bytes")
            chunks.append(chunk)
        return b"".join(chunks)

    def _records(self, endpoint: _Endpoint, document: object) -> list[dict[str, object]]:
        if document is None:
            return []
        found = _walk(document, endpoint.rows)
        if isinstance(found, dict):
            found = [found]
        if not isinstance(found, list) or not all(isinstance(record, dict) for record in found):
            raise AgentLibError(
                f"data source {self._name!r}, query {endpoint.description.name!r}: "
                "the response does not hold records where the definition says"
            )
        return found

    async def validate(self) -> None:
        """Check that the source is started and offers at least one query.

        Raises:
            ConfigurationError: If it is not ready.
        """
        if self._client is None or self._endpoints is None:
            raise ConfigurationError(f"data source {self._name!r} is not started")
        if not self._endpoints:
            raise ConfigurationError(f"data source {self._name!r} has no queries")

    async def aclose(self) -> None:
        """Close the HTTP client. Safe to call more than once."""
        client, self._client = self._client, None
        if client is not None:
            await client.aclose()

    def _started(self) -> tuple[Mapping[str, _Endpoint], httpx.AsyncClient]:
        if self._endpoints is None or self._client is None:
            raise RuntimeError(f"data source {self._name!r} is not started")
        return self._endpoints, self._client
