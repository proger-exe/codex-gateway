from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import jsonschema

from .schema_store import SchemaRecord


MAX_VALIDATION_RETRIES = 2


class StructuredOutputError(Exception):
    def __init__(self, code: str, message: str, *, status_code: int = 422, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.retryable = retryable


@dataclass
class StructuredValidationFailure:
    kind: str
    message: str
    details: list[str]
    raw_output: str


@dataclass
class StructuredParseResult:
    parsed: dict[str, Any] | list[Any]
    raw_output: str


def validate_json_schema(schema: dict[str, Any]) -> None:
    try:
        validator_cls = jsonschema.validators.validator_for(schema)
        validator_cls.check_schema(schema)
    except jsonschema.SchemaError as exc:
        raise StructuredOutputError(
            "invalid_schema_payload",
            f"invalid JSON Schema: {exc.message}",
            status_code=422,
        ) from exc


def serialize_compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def build_initial_prompt(
    *,
    user_message: str,
    schema_record: SchemaRecord,
    chat_context: dict[str, Any] | list[Any] | str | None,
) -> str:
    parts = [
        "You must return only valid JSON that matches the provided schema.",
        "Do not include markdown fences, explanations, commentary, or extra text.",
        f"Schema ID: {schema_record.schema_id}",
        f"JSON Schema: {json.dumps(schema_record.schema, ensure_ascii=True, indent=2)}",
    ]
    if chat_context is not None:
        if isinstance(chat_context, str):
            rendered_context = chat_context
        else:
            rendered_context = json.dumps(chat_context, ensure_ascii=True, indent=2)
        parts.append(f"Chat context:\n{rendered_context}")
    parts.append(f"User request:\n{user_message}")
    parts.append("Return only the final JSON response.")
    return "\n\n".join(parts)


def parse_and_validate_output(
    raw_output: str,
    schema_record: SchemaRecord,
) -> StructuredParseResult:
    text = raw_output.strip()
    if not text:
        raise StructuredOutputError(
            "invalid_model_output",
            "model returned empty output",
            status_code=502,
            retryable=True,
        )

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        failure = StructuredValidationFailure(
            kind="json_parse_error",
            message="model output was not valid JSON",
            details=[f"JSON parse error at line {exc.lineno} column {exc.colno}: {exc.msg}"],
            raw_output=text,
        )
        raise StructuredOutputError(
            "invalid_model_output",
            failure.message,
            status_code=502,
            retryable=True,
        ) from exc

    try:
        jsonschema.validate(parsed, schema_record.schema)
    except jsonschema.ValidationError as exc:
        details = [exc.message]
        if list(exc.path):
            details.append(f"path: {'/'.join(str(part) for part in exc.path)}")
        if exc.validator:
            details.append(f"validator: {exc.validator}")
        raise StructuredOutputError(
            "schema_validation_failed",
            "; ".join(details),
            status_code=502,
            retryable=True,
        ) from exc

    if not isinstance(parsed, (dict, list)):
        raise StructuredOutputError(
            "schema_validation_failed",
            "model output must be a JSON object or array",
            status_code=502,
            retryable=True,
        )

    return StructuredParseResult(parsed=parsed, raw_output=text)


def classify_failure(
    error: StructuredOutputError,
    raw_output: str,
) -> StructuredValidationFailure:
    if error.code == "schema_validation_failed":
        return StructuredValidationFailure(
            kind="schema_validation_error",
            message="model output failed schema validation",
            details=[part.strip() for part in error.message.split(";") if part.strip()],
            raw_output=raw_output.strip(),
        )
    if raw_output.strip():
        return StructuredValidationFailure(
            kind="json_parse_error",
            message="model output was not valid JSON",
            details=[error.message],
            raw_output=raw_output.strip(),
        )
    return StructuredValidationFailure(
        kind="empty_or_garbage_output",
        message="model output was empty",
        details=[error.message],
        raw_output="",
    )


def build_retry_prompt(
    *,
    original_prompt: str,
    schema_record: SchemaRecord,
    failure: StructuredValidationFailure,
) -> str:
    if failure.kind == "json_parse_error":
        corrective = (
            "Your previous answer was not valid JSON. Return only a valid JSON object or array. "
            "Do not use markdown fences or any explanatory text."
        )
    elif failure.kind == "schema_validation_error":
        corrective = (
            "Your previous answer did not match the required schema. Fix the JSON so it matches exactly."
        )
    else:
        corrective = (
            "Your previous answer was empty or unusable. Generate the full JSON response from scratch."
        )

    issues = "\n".join(f"- {item}" for item in failure.details) if failure.details else "- unspecified validation failure"
    return "\n\n".join(
        [
            corrective,
            f"Schema ID: {schema_record.schema_id}",
            f"JSON Schema: {json.dumps(schema_record.schema, ensure_ascii=True, indent=2)}",
            "Validation issues:",
            issues,
            "Return only the corrected final JSON. No prose. No code fences.",
            f"Original task context:\n{original_prompt}",
        ]
    )
