"""Shared strict JSON and validation helpers for registry adapters."""

import json

from .ports import VersioningError


def _json_copy(value):
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError, OverflowError) as exc:
        raise VersioningError("invalid_state", "Value is not valid JSON data") from exc


def _strict_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _loads_strict(raw):
    def bad_constant(_):
        raise ValueError("invalid JSON constant")
    return json.loads(raw, object_pairs_hook=_strict_object, parse_constant=bad_constant)


def _validate_state(state):
    if not isinstance(state, dict):
        raise VersioningError("invalid_state", "Registry state must be an object")
    required = {"schema_version", "generation", "desired", "active", "history", "versions", "last_attempt"}
    if not required.issubset(state):
        raise VersioningError("invalid_state", "Registry state is missing required fields")
    if type(state["schema_version"]) is not int or state["schema_version"] != 1:
        raise VersioningError("invalid_state", "Unsupported registry schema")
    if type(state["generation"]) is not int or state["generation"] < 0:
        raise VersioningError("invalid_state", "Invalid registry generation")
    versions, history = state["versions"], state["history"]
    if not isinstance(history, list) or not isinstance(versions, dict):
        raise VersioningError("invalid_state", "Invalid registry collections")
    if any(not isinstance(ticket, str) or not isinstance(record, dict) for ticket, record in versions.items()):
        raise VersioningError("invalid_state", "Invalid version records")
    if any("status" in record and not isinstance(record["status"], str) for record in versions.values()):
        raise VersioningError("invalid_state", "Version status must be a string")
    if any(not isinstance(ticket, str) or ticket not in versions for ticket in history):
        raise VersioningError("invalid_state", "History must reference known version tickets")
    if len(set(history)) != len(history):
        raise VersioningError("invalid_state", "History tickets must be unique")
    active, desired, last_attempt = state["active"], state["desired"], state["last_attempt"]
    if active is not None and (not isinstance(active, str) or active not in versions):
        raise VersioningError("invalid_state", "Active must reference a known version ticket")
    if desired is not None and (not isinstance(desired, str) or desired not in versions):
        raise VersioningError("invalid_state", "Desired must reference a known version ticket")
    if last_attempt is not None and not isinstance(last_attempt, dict):
        raise VersioningError("invalid_state", "Last attempt must be null or an object")
    return _json_copy(state)
