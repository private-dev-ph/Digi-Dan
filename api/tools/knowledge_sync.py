"""Coordinate a reviewed production knowledge sync using Stage3 tools."""
from __future__ import annotations

import argparse
import json
import os
import re
import tempfile
import uuid
from pathlib import Path

from api.core.config import Settings
from api.knowledge.cloud import _create_cloud_stores, cloud_configuration, create_cloud_stores
from api.knowledge import resume_source
from api.knowledge.deployed_source import DeployedSourceVerifier
from api.knowledge.ports import VersioningError
from api.tools import knowledge_production as production
from api.tools.assemble_knowledge import REPO_ROOT, _previous, _read_json, build_candidate
from api.tools.knowledge_production_preflight import FIELDS, load_production_settings, preflight
from api.tools.prepare_resume import (PIPELINE_VERSION, _validate_snapshot_obj,
                                     prepare_resume)
from api.knowledge.versioning import _validate

DEFAULT_PROBES = REPO_ROOT / "knowledge" / "production-retrieval-probes.json"
_SAFE = re.compile(r"\A[a-z][a-z0-9_]{0,63}\Z")
_RUN_ID = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._:-]{0,99}\Z")
_REVIEW = {"resume_review_required", "portfolio_review_required", "candidate_review_required",
           "retrieval_probe_review_required", "reviewed_resume_invalid"}
_SUPERSEDED = {"generation_conflict", "superseded", "stale_job"}
_REVIEWED_RESUME = REPO_ROOT / "knowledge" / "reviewed-resume.json"
_SOURCE_POLICY = REPO_ROOT / "knowledge" / "source-policy.json"
_MAX_REVIEWED_RESUME_BYTES = 256 * 1024


def _strict_json(raw: bytes):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    def reject_constant(_value):
        raise ValueError("non-finite JSON number")

    return json.loads(raw.decode("utf-8"), object_pairs_hook=unique, parse_constant=reject_constant)


def _seed_reviewed_resume(state_dir: Path, site_url: str, *,
                          reviewed_path: Path = _REVIEWED_RESUME,
                          policy_path: Path = _SOURCE_POLICY) -> Path:
    """Seed only an absent local cache from the reviewed extraction artifact."""
    state_dir = Path(state_dir)
    destination = state_dir / "last-successful.json"
    if destination.exists() or destination.is_symlink():
        return destination
    try:
        with Path(reviewed_path).open("rb") as stream:
            raw = stream.read(_MAX_REVIEWED_RESUME_BYTES + 1)
        if len(raw) > _MAX_REVIEWED_RESUME_BYTES:
            raise ValueError("reviewed snapshot exceeds size limit")
        snapshot = _strict_json(raw)
        _validate_snapshot_obj(snapshot)
        policy = _read_json(Path(policy_path))
        expected_hash = policy.get("resume_content_hash")
        if (snapshot.get("pipeline_version") != PIPELINE_VERSION
                or not isinstance(expected_hash, str)
                or snapshot.get("content_hash") != expected_hash):
            raise ValueError("reviewed snapshot does not match source policy")
        resume_source.validate_url(site_url)
        source_url = snapshot["source"]["url"]
        resume_source.validate_url(source_url)
        if resume_source.origin(source_url) != resume_source.origin(site_url):
            raise ValueError("reviewed snapshot source origin mismatch")

        state_dir.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile("wb", dir=state_dir, prefix=".reviewed-resume.",
                                             suffix=".tmp", delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                # Same-directory hard-link is atomic and does not replace a
                # concurrently created local snapshot.
                os.link(temporary, destination)
            except FileExistsError:
                pass
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    except Exception:
        raise VersioningError("reviewed_resume_invalid") from None
    return destination


def _settings(env_file: Path | None):
    if env_file is not None:
        worker, runtime = load_production_settings(env_file)
    else:
        values = dict(os.environ)
        required = (*FIELDS.keys(), "UPSTASH_REDIS_REST_READ_ONLY_TOKEN")
        if any(not isinstance(values.get(key), str) or not values[key].strip() for key in required):
            raise VersioningError("invalid_cloud_configuration")
        if values["KNOWLEDGE_ENVIRONMENT"] != "production":
            raise VersioningError("production_environment_required")
        if any(values[k] != values[k].strip() for k in
               ("UPSTASH_REDIS_REST_TOKEN", "UPSTASH_REDIS_REST_READ_ONLY_TOKEN")):
            raise VersioningError("invalid_cloud_configuration")
        try:
            worker_values = {field: values[key] for key, field in FIELDS.items() if values.get(key) is not None}
            worker = Settings(**worker_values)
            runtime_values = dict(worker_values)
            runtime_values["upstash_redis_rest_token"] = values["UPSTASH_REDIS_REST_READ_ONLY_TOKEN"]
            runtime = Settings(**runtime_values)
        except Exception:
            raise VersioningError("invalid_cloud_configuration") from None
        if worker.upstash_redis_rest_token == runtime.upstash_redis_rest_token:
            raise VersioningError("distinct_production_tokens_required")
    preflight(worker, runtime, online=False)
    return worker, runtime


def _status(runtime):
    return production.status(runtime)


def _candidate(site_url: str, state: Path, tesseract_cmd: str = "tesseract"):
    resume_dir = state / "resume"
    resume_path = resume_dir / "last-successful.json"
    manifest = site_url.rstrip("/") + "/api/knowledge"
    _seed_reviewed_resume(resume_dir, site_url)
    prepare_resume(state_dir=resume_dir, manifest_url=manifest, tesseract_cmd=tesseract_cmd)
    candidate_dir = state / "candidate"
    build_candidate(output_dir=candidate_dir, site_url=site_url, resume_path=resume_path,
                    policy_path=REPO_ROOT / "knowledge" / "source-policy.json",
                    legacy_path=REPO_ROOT / "daniel-zachary-rag-data.json", manifest_url=manifest)
    candidate_path = candidate_dir / "last-successful.json"
    candidate = _previous(candidate_path)
    if candidate is None:
        raise VersioningError("invalid_candidate")
    try:
        probes = production._probe_fixture(DEFAULT_PROBES)
    except Exception:
        raise VersioningError("retrieval_probe_review_required") from None
    # Validate using the configured production embedding profile before staging.
    return candidate_path, resume_path, candidate, probes


def _version_id(candidate, worker, probes):
    return _validate_candidate(candidate, worker, probes)[2]


def _validate_candidate(candidate, worker, probes):
    try:
        return _validate(candidate, cloud_configuration(worker)["embedding_profile"], probes)
    except Exception as exc:
        if getattr(exc, "code", None) in {"invalid_probe", "invalid_probes"}:
            raise VersioningError("retrieval_probe_review_required") from None
        raise


def _summary(report):
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    fields = ("status", "error_code", "ticket", "version_id", "candidate_hash", "chunk_count", "counts", "verification",
              "activation_performed", "activation_attempted", "activation_uncertain", "post_activation_failure")
    safe = {k: report.get(k) for k in fields if k in report}
    with open(path, "a", encoding="utf-8") as stream:
        stream.write("## Knowledge sync\n\n```json\n" + json.dumps(safe, sort_keys=True) + "\n```\n")


def _emit(state_dir: Path, report: dict, code: int) -> int:
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        target = state_dir / "report.json"
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=state_dir, prefix=".report.", suffix=".tmp", delete=False) as f:
            temp = Path(f.name)
            json.dump(report, f, sort_keys=True, indent=2)
            f.write("\n")
        os.replace(temp, target)
        _summary(report)
    except Exception:
        report = {**report, "status": "failed", "error_code": "report_write_failed"}
        code = 1
    print(json.dumps(report, sort_keys=True))
    return code


def _error(exc):
    code = getattr(exc, "code", None)
    if not isinstance(code, str) or not _SAFE.fullmatch(code):
        code = "sync_failed"
    status = "review_required" if code in _REVIEW else "superseded" if code in _SUPERSEDED else "failed"
    return {"status": status, "error_code": code}


def run(mode: str, site_url: str, state_dir: Path, env_file: Path | None,
        trigger: str, run_id: str | None, tesseract_cmd: str = "tesseract"):
    if mode not in {"check", "sync", "status"}:
        raise VersioningError("invalid_mode")
    worker, runtime = _settings(env_file)
    if mode == "status":
        journal = _journal(runtime)
        return {"status": "status", "journal": journal.read(), "active": _status(runtime).get("active")}
    if mode == "check":
        preflight(worker, runtime, online=True)
        status = _status(runtime)
        candidate_path, resume_path, candidate, probes = _candidate(site_url, state_dir, tesseract_cmd)
        _validate_candidate(candidate, worker, probes)
        DeployedSourceVerifier(site_url).verify({"candidate_hash": candidate["content_hash"], "source": candidate["provenance"]})
        desired = _version_id(candidate, worker, probes)
        active = status.get("active") or {}
        return {"status": "check_ready", "change_needed": active.get("version_id") != desired,
                "version_id": desired, "chunk_count": len(candidate["chunks"])}
    if os.environ.get("KNOWLEDGE_SYNC_ENABLED", "").lower() != "true":
        raise VersioningError("sync_disabled")
    journal = _journal(worker, writable=True)
    attempt = journal.begin(run_id or uuid.uuid4().hex, trigger)
    activation = False
    ticket = None
    verified = None
    activation_attempted = False
    activation_uncertain = False
    candidate_hash = None
    try:
        initial = _status(runtime)
        expected = initial.get("generation")
        if type(expected) is not int or expected < 0:
            raise VersioningError("invalid_generation")
        candidate_path, resume_path, candidate, probes = _candidate(site_url, state_dir, tesseract_cmd)
        _validate_candidate(candidate, worker, probes)
        candidate_hash = candidate.get("content_hash")
        staged = production.stage(worker, candidate_path=candidate_path, site_url=site_url,
                                  expected_generation=expected, resume_path=resume_path, probes_path=DEFAULT_PROBES)
        if staged.get("status") == "unchanged":
            ticket = staged.get("ticket")
            verified = production.verify(runtime, ticket=ticket, site_url=site_url,
                                         cached_probes=True, expected_version=staged.get("version_id"),
                                         expected_probes=probes)
            report = {"status": "unchanged", "ticket": ticket, "version_id": verified.get("version_id"),
                      "candidate_hash": candidate_hash,
                      "counts": {"processed": staged.get("chunk_count", 0),
                                 "updated": staged.get("embedded_chunk_count", 0),
                                 "unchanged": staged.get("reused_chunk_count", 0)}, "activation_performed": False}
        elif staged.get("status") == "staged":
            staged_status = _status(runtime)
            if staged_status.get("generation") != expected + 1:
                raise VersioningError("generation_conflict")
            ticket = staged.get("ticket")
            activation_attempted = True
            activated = production.activate(worker, ticket=ticket, candidate_path=candidate_path,
                                            site_url=site_url, expected_generation=expected + 1,
                                            resume_path=resume_path)
            activation = bool(activated.get("activation_performed"))
            verified = production.verify(runtime, ticket=ticket, site_url=site_url)
            report = {"status": "updated", "ticket": ticket, "version_id": verified.get("version_id"),
                      "candidate_hash": candidate_hash,
                      "counts": {"processed": staged.get("chunk_count", 0),
                                 "updated": staged.get("embedded_chunk_count", 0),
                                 "unchanged": staged.get("reused_chunk_count", 0)}, "activation_performed": activation}
        else:
            raise VersioningError("stage_not_created")
        if not journal.finish(attempt, report):
            raise VersioningError("superseded")
        if staged.get("status") == "unchanged":
            report["verification"] = {"stage": staged.get("probe_verification"),
                                      "runtime": verified.get("probe_verification")}
        return report
    except Exception as exc:
        if activation_attempted and not activation:
            try:
                current = _status(runtime)
                activation = current.get("active", {}).get("ticket") == ticket
                activation_uncertain = not activation
            except Exception:
                activation_uncertain = True
        report = _error(exc)
        report["activation_performed"] = activation
        report["activation_attempted"] = activation_attempted
        if activation_uncertain:
            report["activation_uncertain"] = True
        if activation:
            report["post_activation_failure"] = True
        if ticket is not None:
            report["ticket"] = ticket
        if candidate_hash is not None:
            report["candidate_hash"] = candidate_hash
        if isinstance(verified, dict) and verified.get("version_id"):
            report["version_id"] = verified["version_id"]
        try:
            journal.finish(attempt, report)
        except Exception:
            report["journal_error"] = "journal_finish_failed"
        raise _ReportedFailure(report)


class _ReportedFailure(Exception):
    def __init__(self, report):
        self.report = report


def _journal(settings, *, writable=False):
    from api.knowledge.sync_journal import SyncJournal
    if writable:
        # Writable construction is deliberately restricted to the journal path.
        registry, _ = _create_cloud_stores(settings, writable=True, production_staging=True)
    else:
        registry, _ = create_cloud_stores(settings, writable=False)
    return SyncJournal(registry)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("check", "sync", "status"), default="check")
    parser.add_argument("--site-url", required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--trigger", choices=("local", "manual", "schedule", "deployment", "push"), default="local")
    parser.add_argument("--run-id")
    parser.add_argument("--tesseract-cmd", default="tesseract")
    args = parser.parse_args(argv)
    if args.run_id is not None and not _RUN_ID.fullmatch(args.run_id):
        return _emit(args.state_dir, {"status": "failed", "error_code": "invalid_run_id"}, 1)
    try:
        report = run(args.mode, args.site_url, args.state_dir, args.env_file, args.trigger, args.run_id,
                     args.tesseract_cmd)
        return _emit(args.state_dir, report, 0)
    except _ReportedFailure as exc:
        return _emit(args.state_dir, exc.report, 1)
    except Exception as exc:
        return _emit(args.state_dir, _error(exc), 1)


if __name__ == "__main__":
    raise SystemExit(main())
