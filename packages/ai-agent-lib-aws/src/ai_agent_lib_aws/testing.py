"""Test support for the AWS adapters: clients that never reach the network.

A unit test builds real SDK clients, so that request and response shapes are
checked by the SDK itself, and answers their calls with ``botocore.stub.Stubber``.
"""

from __future__ import annotations

import io
import re
import threading
from collections.abc import Mapping
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import boto3
from botocore import exceptions as aws

from ai_agent_lib_aws.session import AwsSessionFactory

__all__ = [
    "FakeBedrockGuardrail",
    "FakeFirehose",
    "FakeRedshiftData",
    "FakeS3",
    "FakeSecretsManager",
    "client_error",
    "offline_session",
    "offline_sessions",
]

TEST_REGION = "eu-west-1"


def offline_session(**arguments: Any) -> Any:
    """Return an SDK session with fixed credentials, so nothing is looked up.

    Without credentials the SDK asks the instance metadata service for some,
    which is a network call a unit test must not make.
    """
    return boto3.Session(
        aws_access_key_id="testing",
        aws_secret_access_key="testing",  # noqa: S106 - a placeholder, not a credential
        region_name=arguments.get("region_name") or TEST_REGION,
    )


def offline_sessions(**settings: Any) -> AwsSessionFactory:
    """Return a session factory whose clients can be stubbed and never sign in."""
    settings.setdefault("region", TEST_REGION)
    return AwsSessionFactory(session_factory=offline_session, **settings)


def client_error(code: str, operation: str, *, status: int = 400) -> aws.ClientError:
    """Return the error the SDK raises when a service answers with ``code``."""
    return aws.ClientError(
        {
            "Error": {"Code": code, "Message": "the service's own message"},
            "ResponseMetadata": {"HTTPStatusCode": status},
        },
        operation,
    )


class FakeSecretsManager:
    """Stands in for the Secrets Manager client: a table of secrets by ID.

    Attributes:
        reads: The ID of every secret asked for, in order.
    """

    def __init__(self, secrets: Mapping[str, str | bytes]) -> None:
        self._secrets = dict(secrets)
        self.reads: list[str] = []

    def get_secret_value(self, *, SecretId: str) -> dict[str, Any]:  # noqa: N803 - the SDK's name
        """Return the secret, or raise as the service does when there is none."""
        self.reads.append(SecretId)
        if SecretId not in self._secrets:
            raise client_error("ResourceNotFoundException", "GetSecretValue")
        value = self._secrets[SecretId]
        key = "SecretString" if isinstance(value, str) else "SecretBinary"
        return {"Name": SecretId, key: value}

    def put(self, secret_id: str, value: str) -> None:
        """Change a secret, as a rotation would."""
        self._secrets[secret_id] = value


class FakeFirehose:
    """Stands in for the Firehose client: it keeps what it is sent.

    Attributes:
        records: The bytes of every record accepted, by stream, in order.
        fail_with: An error to raise on the next calls, to test fail-closed paths.
        acknowledge: Whether a put is answered with a record ID.
        status: What the stream's status is reported as.
    """

    def __init__(self, streams: tuple[str, ...] = ("audit",)) -> None:
        self.records: dict[str, list[bytes]] = {stream: [] for stream in streams}
        self.fail_with: BaseException | None = None
        self.acknowledge = True
        self.status = "ACTIVE"

    def _stream(self, name: str, operation: str) -> list[bytes]:
        if self.fail_with is not None:
            raise self.fail_with
        if name not in self.records:
            raise client_error("ResourceNotFoundException", operation)
        return self.records[name]

    def put_record(self, *, DeliveryStreamName: str, Record: Mapping[str, Any]) -> dict[str, Any]:  # noqa: N803
        """Accept one record."""
        stored = self._stream(DeliveryStreamName, "PutRecord")
        stored.append(bytes(Record["Data"]))
        return {"RecordId": f"record-{len(stored)}", "Encrypted": True} if self.acknowledge else {}

    def describe_delivery_stream(self, *, DeliveryStreamName: str) -> dict[str, Any]:  # noqa: N803
        """Describe a stream."""
        self._stream(DeliveryStreamName, "DescribeDeliveryStream")
        return {
            "DeliveryStreamDescription": {
                "DeliveryStreamName": DeliveryStreamName,
                "DeliveryStreamStatus": self.status,
            }
        }


_REDSHIFT_TYPES: Mapping[str, str] = {
    "BIGINT": "int8",
    "INTEGER": "int4",
    "DOUBLE": "float8",
    "BOOLEAN": "bool",
    "DATE": "date",
    "TIMESTAMP": "timestamp",
    "TIMESTAMP WITH TIME ZONE": "timestamptz",
}


def _redshift_type(engine_type: str) -> str:
    if engine_type.startswith("DECIMAL"):
        return "numeric"
    return _REDSHIFT_TYPES.get(engine_type, "varchar")


def _redshift_field(value: object) -> dict[str, Any]:
    """Return one value as the Data API writes it in a result."""
    if value is None:
        return {"isNull": True}
    if isinstance(value, bool):
        return {"booleanValue": value}
    if isinstance(value, int):
        return {"longValue": value}
    if isinstance(value, float):
        return {"doubleValue": value}
    if isinstance(value, datetime):
        return {"stringValue": value.isoformat(sep=" ")}
    if isinstance(value, date | Decimal):
        return {"stringValue": str(value)}
    return {"stringValue": str(value)}


class FakeRedshiftData:
    """Stands in for the Redshift Data API client, with DuckDB as the database.

    Each ``*.csv`` file in ``data_dir`` is a table. A statement arrives in the
    Redshift dialect with ``:name`` parameters, as the real service takes it,
    and is run by an in-process engine. The three-step protocol is kept: a
    statement is submitted, reports ``STARTED`` for a while, then is read in pages.

    Attributes:
        requests: Every ``execute_statement`` request, in order.
        cancelled: The ID of every statement that was cancelled, in order.
        page_size: How many rows one page of a result holds.
        polls_before_done: How many status checks report ``STARTED`` first.
        end_as: A status to report in place of the real outcome. ``STARTED``
            makes every statement run forever.
        fail_with: An error to raise on the next calls.
    """

    def __init__(self, data_dir: Path, *, page_size: int = 2, polls_before_done: int = 1) -> None:
        import duckdb

        self._engine = duckdb.connect(":memory:")
        for path in sorted(data_dir.glob("*.csv")):
            self._engine.read_csv(str(path)).create(path.stem.lower())
        self._lock = threading.Lock()
        self._statements: dict[str, dict[str, Any]] = {}
        self.requests: list[dict[str, Any]] = []
        self.cancelled: list[str] = []
        self.page_size = page_size
        self.polls_before_done = polls_before_done
        self.end_as: str | None = None
        self.fail_with: BaseException | None = None

    def close(self) -> None:
        """Close the engine."""
        self._engine.close()

    def _statement(self, statement_id: str, operation: str) -> dict[str, Any]:
        if self.fail_with is not None:
            raise self.fail_with
        with self._lock:
            if statement_id not in self._statements:
                raise client_error("ResourceNotFoundException", operation)
            return self._statements[statement_id]

    def execute_statement(self, **request: Any) -> dict[str, Any]:
        """Accept a statement and run it. A statement that fails still gets an ID."""
        import duckdb
        import sqlglot

        if self.fail_with is not None:
            raise self.fail_with
        if ("WorkgroupName" in request) == ("ClusterIdentifier" in request):
            raise client_error("ValidationException", "ExecuteStatement")
        values = {item["name"]: item["value"] for item in request.get("Parameters", ())}
        statement: dict[str, Any] = {"polls": 0, "status": "FINISHED", "columns": [], "rows": []}
        cursor = self._engine.cursor()
        try:
            sql = sqlglot.transpile(request["Sql"], read="redshift", write="duckdb")[0]
            cursor.execute(sql, values) if values else cursor.execute(sql)
            statement["columns"] = [
                {
                    "name": str(column[0]),
                    "label": str(column[0]),
                    "typeName": _redshift_type(str(column[1])),
                }
                for column in cursor.description or ()
            ]
            statement["rows"] = cursor.fetchall()
        except (duckdb.Error, sqlglot.errors.SqlglotError) as exc:
            statement["status"], statement["error"] = "FAILED", str(exc)
        finally:
            cursor.close()
        with self._lock:
            self.requests.append(dict(request))
            statement_id = f"statement-{len(self.requests)}"
            self._statements[statement_id] = statement
        return {"Id": statement_id}

    def describe_statement(self, *, Id: str) -> dict[str, Any]:  # noqa: N803 - the SDK's name
        """Report how far a statement is."""
        statement = self._statement(Id, "DescribeStatement")
        with self._lock:
            statement["polls"] += 1
            running = statement["polls"] <= self.polls_before_done
        status = "STARTED" if running else (self.end_as or statement["status"])
        reply: dict[str, Any] = {"Id": Id, "Status": status, "HasResultSet": status == "FINISHED"}
        if status == "FAILED":
            reply["Error"] = statement.get("error", "the database's own message")
        return reply

    def get_statement_result(self, *, Id: str, NextToken: str | None = None) -> dict[str, Any]:  # noqa: N803
        """Return one page of a finished statement's rows."""
        statement = self._statement(Id, "GetStatementResult")
        first = int(NextToken or 0)
        page = statement["rows"][first : first + self.page_size]
        reply: dict[str, Any] = {
            "ColumnMetadata": statement["columns"],
            "Records": [[_redshift_field(value) for value in row] for row in page],
            "TotalNumRows": len(statement["rows"]),
        }
        if first + self.page_size < len(statement["rows"]):
            reply["NextToken"] = str(first + self.page_size)
        return reply

    def cancel_statement(self, *, Id: str) -> dict[str, Any]:  # noqa: N803 - the SDK's name
        """Record that a statement was cancelled."""
        self._statement(Id, "CancelStatement")
        with self._lock:
            self.cancelled.append(Id)
        return {"Status": True}


class FakeS3:
    """Stands in for the S3 client: objects by bucket and key, each with its versions.

    Attributes:
        owner: The account that owns every bucket.
        versioned: Whether the buckets keep versions. Without versioning S3
            reports the version of every object as ``null``.
        reads: The ``get_object`` requests, in order.
        fail_with: An error to raise on the next calls.
    """

    def __init__(self, *, owner: str = "111122223333", versioned: bool = True) -> None:
        self._objects: dict[tuple[str, str], list[bytes]] = {}
        self.owner = owner
        self.versioned = versioned
        self.reads: list[dict[str, Any]] = []
        self.fail_with: BaseException | None = None

    def put(self, bucket: str, key: str, content: bytes | str) -> str:
        """Store a new version of an object and return its version ID."""
        data = content.encode("utf-8") if isinstance(content, str) else content
        versions = self._objects.setdefault((bucket, key), [])
        versions.append(data)
        return f"v{len(versions)}"

    def get_object(self, **request: Any) -> dict[str, Any]:
        """Return the latest version of an object, or raise as S3 does."""
        self.reads.append(dict(request))
        if self.fail_with is not None:
            raise self.fail_with
        if request.get("ExpectedBucketOwner", self.owner) != self.owner:
            raise client_error("AccessDenied", "GetObject", status=403)
        if not any(bucket == request["Bucket"] for bucket, _ in self._objects):
            raise client_error("NoSuchBucket", "GetObject", status=404)
        versions = self._objects.get((request["Bucket"], request["Key"]))
        if not versions:
            raise client_error("NoSuchKey", "GetObject", status=404)
        content = versions[-1]
        return {
            "Body": io.BytesIO(content),
            "ContentLength": len(content),
            "VersionId": f"v{len(versions)}" if self.versioned else "null",
        }


_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_ATTACK = re.compile(r"(?i)ignore (?:all |your )?(?:previous|prior) instructions")
_NO_USAGE: Mapping[str, int] = {
    "topicPolicyUnits": 0,
    "contentPolicyUnits": 1,
    "wordPolicyUnits": 0,
    "sensitiveInformationPolicyUnits": 1,
    "sensitiveInformationPolicyFreeUnits": 0,
    "contextualGroundingPolicyUnits": 0,
}


class FakeBedrockGuardrail:
    """Stands in for the Bedrock runtime client's ``apply_guardrail``.

    It behaves like a small guardrail: it acts on e-mail addresses, on the
    words in ``denied_words`` and, in input only, on a well-known attack on
    the prompt. Like the real service it returns the matched text beside each
    finding, which an adapter must never pass on.

    Attributes:
        guardrails: The ID and version pairs that exist.
        denied_words: Words the guardrail blocks.
        pii_action: What the guardrail does with an e-mail address:
            ``BLOCKED``, ``ANONYMIZED`` or ``NONE`` to only detect it.
        requests: Every request, in order.
        fail_with: An error to raise on the next calls.
        reply: A reply to return in place of the computed one.
    """

    def __init__(self, guardrails: tuple[tuple[str, str], ...] = (("gr1abc", "3"),)) -> None:
        self.guardrails = set(guardrails)
        self.denied_words: tuple[str, ...] = ()
        self.pii_action = "BLOCKED"
        self.requests: list[dict[str, Any]] = []
        self.fail_with: BaseException | None = None
        self.reply: dict[str, Any] | None = None

    def apply_guardrail(self, **request: Any) -> dict[str, Any]:
        """Assess the text of a request."""
        self.requests.append(dict(request))
        if self.fail_with is not None:
            raise self.fail_with
        if (request["guardrailIdentifier"], request["guardrailVersion"]) not in self.guardrails:
            raise client_error("ResourceNotFoundException", "ApplyGuardrail", status=404)
        if self.reply is not None:
            return self.reply
        text = " ".join(block["text"]["text"] for block in request["content"])
        assessment: dict[str, Any] = {}
        emails = [
            {"match": match, "type": "EMAIL", "action": self.pii_action}
            for match in _EMAIL.findall(text)
        ]
        if emails:
            assessment["sensitiveInformationPolicy"] = {"piiEntities": emails, "regexes": []}
        words = [
            {"match": word, "action": "BLOCKED"}
            for word in self.denied_words
            if word.lower() in text.lower()
        ]
        if words:
            assessment["wordPolicy"] = {"customWords": words, "managedWordLists": []}
        if request["source"] == "INPUT" and _ATTACK.search(text):
            assessment["contentPolicy"] = {
                "filters": [{"type": "PROMPT_ATTACK", "confidence": "HIGH", "action": "BLOCKED"}]
            }
        acted = any(
            item.get("action") != "NONE"
            for policy in assessment.values()
            for items in policy.values()
            for item in items
        )
        return {
            "usage": dict(_NO_USAGE),
            "action": "GUARDRAIL_INTERVENED" if acted else "NONE",
            "outputs": [{"text": "Sorry, this cannot be processed."}] if acted else [],
            "assessments": [assessment] if assessment else [],
        }
