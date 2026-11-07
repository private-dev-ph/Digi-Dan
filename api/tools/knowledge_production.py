"""Read production registry state or stage a freshly reviewed candidate offline-first."""
from __future__ import annotations

import argparse
import json
import re
import tempfile
from pathlib import Path

from api.knowledge.manifest import KnowledgeValidationError
from api.knowledge.ports import VersioningError
from api.knowledge.cloud import cloud_configuration, create_cloud_stores
from api.tools.assemble_knowledge import REPO_ROOT, _previous, _read_json, build_candidate
from api.tools.knowledge_production_preflight import load_production_settings, preflight
from api.knowledge.versioning import _validate

REPO = REPO_ROOT
DEFAULT_RESUME = REPO / ".resume-state" / "production" / "last-successful.json"
DEFAULT_PROBES = REPO / "knowledge" / "production-retrieval-probes.json"
_SAFE_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_TICKET = re.compile(r"^[0-9a-f]{16}$")


def _safe_error(exc: Exception, fallback: str) -> str:
    code = getattr(exc, "code", None)
    return code if isinstance(code, str) and _SAFE_CODE.fullmatch(code) else fallback


def _probe_fixture(path: Path) -> list[dict]:
    fixture = _read_json(path)
    if (type(fixture.get("schema_version")) is not int or fixture.get("schema_version") != 1
            or fixture.get("kind") != "production-retrieval-probes"
            or not isinstance(fixture.get("probes"), list)
            or not fixture["probes"]):
        raise VersioningError("invalid_probes")
    return fixture["probes"]


def _fresh_candidate(candidate: dict, *, site_url: str, resume_path: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="knowledge-production-") as directory:
        rebuilt_path = Path(directory) / "last-successful.json"
        build_candidate(
            output_dir=Path(directory), site_url=site_url, resume_path=resume_path,
            policy_path=REPO / "knowledge" / "source-policy.json",
            legacy_path=REPO / "daniel-zachary-rag-data.json",
            manifest_url=site_url.rstrip("/") + "/api/knowledge",
        )
        rebuilt = _previous(rebuilt_path)
    if rebuilt != candidate:
        raise VersioningError("candidate_review_required")


def _selected(record: dict | None, fields: tuple[str, ...]) -> dict | None:
    if record is None:
        return None
    selected = {key: record.get(key) for key in fields}
    if "error_code" in selected and selected["error_code"] is not None:
        selected["error_code"] = (selected["error_code"] if isinstance(selected["error_code"], str)
                                   and _SAFE_CODE.fullmatch(selected["error_code"]) else "unknown_error")
    return selected


def status(runtime_settings) -> dict:
    registry, _ = create_cloud_stores(runtime_settings, writable=False)
    revision, state = registry.read()
    active = state.get("versions", {}).get(state.get("active")) if state.get("active") else None
    fields = ("ticket", "version_id", "namespace", "status", "expected_count", "error_code")
    return {
        "registry_revision": revision,
        "generation": state.get("generation"),
        "active": _selected(active, fields),
        "desired": state.get("desired"),
        "versions": [_selected(item, fields) for item in state.get("versions", {}).values()],
    }


def stage(worker_settings, *, candidate_path: Path, site_url: str,
          expected_generation: int, resume_path: Path, probes_path: Path) -> dict:
    if isinstance(expected_generation, bool) or not isinstance(expected_generation, int) or expected_generation < 0:
        raise VersioningError("invalid_generation")
    candidate = _previous(candidate_path)
    if candidate is None:
        raise KnowledgeValidationError("invalid_candidate")
    probes = _probe_fixture(probes_path)
    _validate(candidate, cloud_configuration(worker_settings)["embedding_profile"], probes)
    _fresh_candidate(candidate, site_url=site_url, resume_path=resume_path)
    from api.knowledge.cloud import create_production_stager
    stager = create_production_stager(worker_settings, site_url=site_url)
    return stager.stage(candidate, expected_generation=expected_generation, probes=probes)


def _reviewed_candidate(worker_settings, *, candidate_path: Path, site_url: str,
                        resume_path: Path) -> dict:
    candidate = _previous(candidate_path)
    if candidate is None:
        raise KnowledgeValidationError("invalid_candidate")
    _validate(candidate, cloud_configuration(worker_settings)["embedding_profile"],
              _probe_fixture(DEFAULT_PROBES))
    _fresh_candidate(candidate, site_url=site_url, resume_path=resume_path)
    return candidate


def _ticket(value: str) -> str:
    if not isinstance(value, str) or not _TICKET.fullmatch(value):
        raise VersioningError("invalid_ticket")
    return value


def activate(worker_settings, *, ticket: str, candidate_path: Path, site_url: str,
             expected_generation: int, resume_path: Path) -> dict:
    ticket = _ticket(ticket)
    if type(expected_generation) is not int or expected_generation < 0:
        raise VersioningError("invalid_generation")
    candidate = _reviewed_candidate(worker_settings, candidate_path=candidate_path,
                                    site_url=site_url, resume_path=resume_path)
    from api.knowledge.production_rollout import create_production_rollout
    rollout = create_production_rollout(worker_settings, site_url=site_url)
    return rollout.activate(ticket, candidate, expected_generation=expected_generation)


def rollback(worker_settings, *, ticket: str, candidate_path: Path, site_url: str,
             expected_generation: int, resume_path: Path) -> dict:
    ticket = _ticket(ticket)
    if type(expected_generation) is not int or expected_generation < 0:
        raise VersioningError("invalid_generation")
    candidate = _reviewed_candidate(worker_settings, candidate_path=candidate_path,
                                    site_url=site_url, resume_path=resume_path)
    from api.knowledge.production_rollout import create_production_rollout
    rollout = create_production_rollout(worker_settings, site_url=site_url)
    return rollout.rollback(ticket, candidate, expected_generation=expected_generation)


def verify(runtime_settings, *, ticket: str, site_url: str) -> dict:
    ticket = _ticket(ticket)
    from api.knowledge.cloud import create_cloud_service
    from api.knowledge.deployed_source import DeployedSourceVerifier
    service = create_cloud_service(runtime_settings, writable=False)
    record = service.get_active()
    if record.get("ticket") != ticket:
        raise VersioningError("active_ticket_mismatch")
    service._validate_storage(record)
    service._run_probes(record)
    current = service.get_active()
    if (current.get("ticket") != record.get("ticket") or
            current.get("version_id") != record.get("version_id")):
        raise VersioningError("active_version_changed")
    DeployedSourceVerifier(site_url).verify(record)
    return {"ticket": ticket, "version_id": record.get("version_id"),
            "expected_count": record.get("expected_count"),
            "probe_count": len(record.get("probes", []))}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=REPO / ".env.cloud-production")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status", help="Read selected production registry state")
    stage_parser = commands.add_parser("stage", help="Stage and verify a production candidate")
    stage_parser.add_argument("--candidate", type=Path, required=True)
    stage_parser.add_argument("--site-url", required=True)
    stage_parser.add_argument("--expected-generation", type=int, required=True)
    stage_parser.add_argument("--resume", type=Path, default=DEFAULT_RESUME)
    stage_parser.add_argument("--probes", type=Path, default=DEFAULT_PROBES)
    for command in ("activate", "rollback"):
        operation = commands.add_parser(command, help=f"{command.title()} a reviewed production version")
        operation.add_argument("ticket")
        operation.add_argument("--candidate", type=Path, required=True)
        operation.add_argument("--site-url", required=True)
        operation.add_argument("--expected-generation", type=int, required=True)
        operation.add_argument("--resume", type=Path, default=DEFAULT_RESUME)
    verify_parser = commands.add_parser("verify", help="Run read-only smoke checks for the active version")
    verify_parser.add_argument("--ticket", required=True)
    verify_parser.add_argument("--site-url", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "verify":
            _ticket(args.ticket)
            _, runtime = load_production_settings(args.env_file)
            result = verify(runtime, ticket=args.ticket, site_url=args.site_url)
            print(json.dumps({"environment": "production", "result": result}, indent=2, ensure_ascii=False))
            return 0
        if args.command in ("activate", "rollback"):
            _ticket(args.ticket)
        worker, runtime = load_production_settings(args.env_file)
        preflight(worker, runtime, online=False)
        if args.command == "status":
            result = status(runtime)
        elif args.command == "stage":
            result = stage(worker, candidate_path=args.candidate, site_url=args.site_url,
                           expected_generation=args.expected_generation,
                           resume_path=args.resume, probes_path=args.probes)
        elif args.command == "activate":
            result = activate(worker, ticket=args.ticket, candidate_path=args.candidate,
                              site_url=args.site_url, expected_generation=args.expected_generation,
                              resume_path=args.resume)
        else:
            result = rollback(worker, ticket=args.ticket, candidate_path=args.candidate,
                              site_url=args.site_url, expected_generation=args.expected_generation,
                              resume_path=args.resume)
        print(json.dumps({"environment": "production", "result": result}, indent=2, ensure_ascii=False))
        return 0
    except Exception as exc:
        print(json.dumps({"environment": "production", "status": "not_ready",
                          "error_code": _safe_error(exc, "production_operation_failed")}, sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
