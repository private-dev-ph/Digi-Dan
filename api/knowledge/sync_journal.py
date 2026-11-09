"""Small, privacy-safe reconciliation status journal stored in registry state."""

from __future__ import annotations

import copy
import math
import re
import time

from .ports import VersioningError


_STATUSES = {"running", "updated", "unchanged", "review_required", "superseded", "failed"}
_TRIGGERS = {"manual", "schedule", "deployment", "push", "local"}
_HEX16 = re.compile(r"[0-9a-f]{16}\Z")
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_ERROR = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,99}\Z")
_COUNT_NAMES = {"discovered", "processed", "updated", "unchanged", "review_required", "failed"}
_MAX_CAS_ATTEMPTS = 8
_MAX_JOURNAL_BYTES = 16 * 1024
_RUN_TIMEOUT = 1800


def _timestamp(value, *, nullable=False):
    if nullable and value is None:
        return None
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise VersioningError("journal_corrupt", "Reconciliation journal is malformed")
    return float(value)


def _run_id(value):
    if not isinstance(value, str) or not _RUN_ID.fullmatch(value):
        raise VersioningError("invalid_run_id", "Run identifier is invalid")
    return value


def _safe_report(report):
    """Copy only known scalar fields; arbitrary payloads never enter the registry."""
    if not isinstance(report, dict):
        raise VersioningError("invalid_report", "Reconciliation report is invalid")
    safe = {}
    status = report.get("status")
    if not isinstance(status, str) or status not in _STATUSES - {"running"}:
        raise VersioningError("invalid_report", "Reconciliation report is invalid")
    safe["status"] = status
    for key, pattern in (("ticket", _HEX16), ("version_id", _HEX64), ("candidate_hash", _HEX64)):
        value = report.get(key)
        if value is not None:
            if not isinstance(value, str) or not pattern.fullmatch(value):
                raise VersioningError("invalid_report", "Reconciliation report is invalid")
            safe[key] = value
    error_code = report.get("error_code")
    if error_code is not None:
        if not isinstance(error_code, str) or not _ERROR.fullmatch(error_code):
            raise VersioningError("invalid_report", "Reconciliation report is invalid")
        safe["error_code"] = error_code
    for key in ("activation_attempted", "activation_uncertain", "activation_performed", "post_activation_failure"):
        value = report.get(key)
        if value is not None:
            if type(value) is not bool:
                raise VersioningError("invalid_report", "Reconciliation report is invalid")
            safe[key] = value
    counts = report.get("counts")
    if counts is not None:
        if (not isinstance(counts, dict) or set(counts) - _COUNT_NAMES
                or any(type(v) is not int or not 0 <= v <= 1_000_000 for v in counts.values())):
            raise VersioningError("invalid_report", "Reconciliation report is invalid")
        safe["counts"] = dict(counts)
    # run_id and trigger are accepted as checked identity fields, although the
    # values established by begin() remain authoritative.
    if "run_id" in report:
        safe["run_id"] = _run_id(report["run_id"])
    if "trigger" in report and (not isinstance(report["trigger"], str) or report["trigger"] not in _TRIGGERS):
        raise VersioningError("invalid_report", "Reconciliation report is invalid")
    if "trigger" in report:
        safe["trigger"] = report["trigger"]
    return safe


def _validate_journal(value):
    if value is None:
        return {"attempt": 0, "latest": None, "last_success_at": None, "last_update_at": None}
    if not isinstance(value, dict) or set(value) != {"attempt", "latest", "last_success_at", "last_update_at"}:
        raise VersioningError("journal_corrupt", "Reconciliation journal is malformed")
    attempt = value["attempt"]
    if type(attempt) is not int or not 0 <= attempt < 2**53:
        raise VersioningError("journal_corrupt", "Reconciliation journal is malformed")
    last_success = _timestamp(value["last_success_at"], nullable=True)
    last_update = _timestamp(value["last_update_at"], nullable=True)
    latest = value["latest"]
    if (attempt == 0) != (latest is None):
        raise VersioningError("journal_corrupt", "Reconciliation journal is malformed")
    if last_update is not None and (last_success is None or last_update > last_success):
        raise VersioningError("journal_corrupt", "Reconciliation journal is malformed")
    if latest is not None:
        if (not isinstance(latest, dict) or latest.get("attempt") != attempt
                or type(latest.get("attempt")) is not int
                or not isinstance(latest.get("status"), str) or latest.get("status") not in _STATUSES
                or not isinstance(latest.get("trigger"), str) or latest.get("trigger") not in _TRIGGERS):
            raise VersioningError("journal_corrupt", "Reconciliation journal is malformed")
        _run_id(latest.get("run_id"))
        _timestamp(latest.get("started_at"))
        allowed = {"attempt", "status", "trigger", "run_id", "started_at", "finished_at",
                   "error_code", "ticket", "version_id", "candidate_hash", "activation_attempted",
                   "activation_uncertain", "activation_performed",
                   "post_activation_failure", "counts"}
        if set(latest) - allowed:
            raise VersioningError("journal_corrupt", "Reconciliation journal is malformed")
        # Revalidate persisted report fields and type/shape by the same allowlist.
        fields = {k: latest[k] for k in latest if k not in {"attempt", "started_at", "finished_at"}}
        if fields.get("status") == "running":
            if set(latest) != {"attempt", "status", "trigger", "run_id", "started_at"}:
                raise VersioningError("journal_corrupt", "Reconciliation journal is malformed")
        else:
            _safe_report(fields)
        if "finished_at" in latest:
            if latest["status"] == "running":
                raise VersioningError("journal_corrupt", "Reconciliation journal is malformed")
            finished_at = _timestamp(latest["finished_at"])
            if finished_at < _timestamp(latest["started_at"]):
                raise VersioningError("journal_corrupt", "Reconciliation journal is malformed")
        elif latest["status"] != "running":
            raise VersioningError("journal_corrupt", "Reconciliation journal is malformed")
        elif latest["status"] == "running" and "finished_at" in latest:
            raise VersioningError("journal_corrupt", "Reconciliation journal is malformed")
    import json
    if len(json.dumps(value, separators=(",", ":"), allow_nan=False).encode("utf-8")) > _MAX_JOURNAL_BYTES:
        raise VersioningError("journal_corrupt", "Reconciliation journal is malformed")
    return copy.deepcopy(value)


class SyncJournal:
    """CAS-backed journal; status writes do not modify activation semantics."""

    def __init__(self, registry, clock=time.time):
        self.registry = registry
        self.clock = clock

    def _now(self):
        return _timestamp(self.clock())

    def _mutate(self, fn):
        for _ in range(_MAX_CAS_ATTEMPTS):
            revision, state = self.registry.read()
            if not isinstance(state, dict):
                raise VersioningError("registry_corrupt", "Registry state is malformed")
            journal = _validate_journal(state.get("reconciliation"))
            updated = fn(journal)
            if updated is None:
                return False
            result = updated.pop("_result", True)
            state = copy.deepcopy(state)
            state["reconciliation"] = _validate_journal(updated)
            if self.registry.compare_and_swap(revision, state):
                return result
        raise VersioningError("registry_contention", "Registry changed too many times")

    def begin(self, run_id: str, trigger: str) -> int:
        run_id = _run_id(run_id)
        if not isinstance(trigger, str) or trigger not in _TRIGGERS:
            raise VersioningError("invalid_trigger", "Reconciliation trigger is invalid")
        now = self._now()

        def update(journal):
            attempt = journal["attempt"] + 1
            if attempt >= 2**53:
                raise VersioningError("journal_exhausted", "Reconciliation journal is full")
            return {**journal, "attempt": attempt, "latest": {
                "attempt": attempt, "status": "running", "trigger": trigger,
                "run_id": run_id, "started_at": now,
            }, "_result": attempt}

        return self._mutate(update)

    def finish(self, attempt: int, report: dict) -> bool:
        if type(attempt) is not int or attempt <= 0:
            raise VersioningError("invalid_attempt", "Reconciliation attempt is invalid")
        safe = _safe_report(report)
        now = self._now()

        def update(journal):
            latest = journal["latest"]
            if latest is None or latest["attempt"] != attempt or latest["status"] != "running":
                return None
            if now < latest["started_at"]:
                raise VersioningError("invalid_completion_time", "Completion time precedes run start")
            latest = {**latest, **safe, "finished_at": now}
            # Identity from begin() is authoritative if a report includes it.
            latest["run_id"] = journal["latest"]["run_id"]
            latest["trigger"] = journal["latest"]["trigger"]
            result = {**journal, "latest": latest, "_result": True}
            if safe["status"] in {"updated", "unchanged"}:
                result["last_success_at"] = now
            if safe["status"] == "updated":
                result["last_update_at"] = now
            return result

        return bool(self._mutate(update))

    def read(self, stale_after_seconds=10800):
        if type(stale_after_seconds) not in (int, float) or not math.isfinite(stale_after_seconds) or stale_after_seconds < 0:
            raise VersioningError("invalid_stale_window", "Stale window is invalid")
        _, state = self.registry.read()
        if not isinstance(state, dict):
            raise VersioningError("registry_corrupt", "Registry state is malformed")
        journal = _validate_journal(state.get("reconciliation"))
        now = self._now()
        latest = copy.deepcopy(journal["latest"])
        success = journal["last_success_at"]
        stale = success is None or now - success > stale_after_seconds
        timed_out = bool(latest and latest["status"] == "running" and now - latest["started_at"] > _RUN_TIMEOUT)
        return {"attempt": journal["attempt"], "latest": latest,
                "last_success_at": success, "last_update_at": journal["last_update_at"],
                "stale": stale, "running_timed_out": timed_out}
