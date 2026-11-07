"""Run versioned knowledge locally with SQLite and deterministic test embeddings.

This command never loads cloud credentials or calls embedding/vector APIs.
Its source check confirms an explicitly supplied local candidate, not deployment.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from api.knowledge.local_store import LocalEmbedder, SQLiteRegistry, SQLiteVectorStore
from api.knowledge.ports import VersioningError
from api.knowledge.versioning import KnowledgeService
from api.tools.assemble_knowledge import _read_json

REPO_ROOT = Path(__file__).resolve().parents[2]


def _candidate(path: Path) -> dict:
    return _read_json(path, 16 * 1024 * 1024)


def _local_verifier(candidate: dict):
    def verify(record: dict) -> bool:
        return (record.get("candidate_hash") == candidate.get("content_hash")
                and record.get("source") == candidate.get("provenance"))
    return verify


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, default=Path(".knowledge-local-state"))
    commands = parser.add_subparsers(dest="command", required=True)
    stage = commands.add_parser("stage", help="Build and verify without activation")
    stage.add_argument("--candidate", type=Path, required=True)
    stage.add_argument("--probes", type=Path, default=REPO_ROOT / "knowledge" / "local-retrieval-probes.json")
    stage.add_argument("--dimensions", type=int, default=128)
    stage.add_argument("--expected-generation", type=int)
    for command in ("activate", "rollback"):
        sub = commands.add_parser(command)
        sub.add_argument("ticket")
        sub.add_argument("--candidate", type=Path, required=True,
                         help="Explicit local source receipt for the selected version")
        if command == "rollback":
            sub.add_argument("--expected-generation", type=int, required=True)
    query = commands.add_parser("query", help="Retrieve facts locally without generating an answer")
    query.add_argument("question")
    query.add_argument("--top-k", type=int, default=4)
    commands.add_parser("status")
    cleanup = commands.add_parser("cleanup")
    cleanup.add_argument("--retain-previous", type=int, default=2)
    cleanup.add_argument("--grace-seconds", type=float, default=3600)
    args = parser.parse_args(argv)
    try:
        db_path = args.state_dir / "knowledge.sqlite3"
        registry = SQLiteRegistry(db_path)
        vectors = SQLiteVectorStore(db_path)
        service = KnowledgeService(registry, vectors, LocalEmbedder())
        revision, state = registry.read()
        if args.command == "stage":
            candidate = _candidate(args.candidate)
            fixture = _read_json(args.probes)
            if fixture.get("schema_version") != 1 or fixture.get("kind") != "local-retrieval-probes":
                raise VersioningError("invalid_probes")
            profile = {"provider": "local-test", "model": "token-hash-v1", "dimensions": args.dimensions,
                       "policy_version": "1"}
            generation = state["generation"] if args.expected_generation is None else args.expected_generation
            result = service.sync(candidate, profile, expected_generation=generation,
                                  verify_source=_local_verifier(candidate), probes=fixture["probes"], activate=False)
        elif args.command == "activate":
            result = service.activate(args.ticket, verify_source=_local_verifier(_candidate(args.candidate)))
        elif args.command == "rollback":
            result = service.rollback(args.ticket, expected_generation=args.expected_generation,
                                      verify_source=_local_verifier(_candidate(args.candidate)))
        elif args.command == "query":
            version, records = service.retrieve(args.question, args.top_k)
            result = {"knowledge_version": version["version_id"], "namespace": version["namespace"],
                      "sources": [{k: r[k] for k in ("id", "text", "metadata", "score") if k in r} for r in records]}
        elif args.command == "cleanup":
            result = {"deleted_namespaces": service.cleanup(retain_previous=args.retain_previous,
                                                           grace_seconds=args.grace_seconds)}
        else:
            active = state["versions"].get(state["active"]) if state["active"] else None
            result = {"registry_revision": revision, "generation": state["generation"], "desired": state["desired"],
                      "active": None if active is None else {k: active[k] for k in ("ticket", "version_id", "namespace")},
                      "last_attempt": state["last_attempt"],
                      "versions": [{k: v.get(k) for k in ("ticket", "version_id", "namespace", "status", "error_code")}
                                   for v in state["versions"].values()]}
        print(json.dumps({"environment": "local-only", "result": result}, indent=2, ensure_ascii=False))
        return 0
    except Exception as exc:
        print(f"Local knowledge operation failed ({getattr(exc, 'code', 'local_operation_failed')}).", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
