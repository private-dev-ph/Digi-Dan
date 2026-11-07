"""Read-only check of deployed knowledge sources, optionally against a candidate."""
from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path

from api.knowledge.deployed_source import DeployedSourceVerifier
from api.knowledge.manifest import KnowledgeValidationError
from api.knowledge.ports import VersioningError
from api.tools.assemble_knowledge import _previous


def _atomic_json(path: Path, value: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="\n",
                                         dir=path.parent, prefix=f".{path.name}.",
                                         suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(value, stream, sort_keys=True, indent=2, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def check(site_url: str, manifest_url: str | None = None,
          candidate_path: Path | None = None) -> dict:
    candidate = None
    if candidate_path is not None:
        candidate = _previous(Path(candidate_path))
        if candidate is None:
            raise KnowledgeValidationError("invalid_candidate")
    verifier = DeployedSourceVerifier(site_url, manifest_url=manifest_url)
    if candidate is None:
        return {"status": "source_ready", "receipt": verifier.inspect()}
    record = {"candidate_hash": candidate["content_hash"],
              "source": candidate["provenance"]}
    return {"status": "candidate_sources_match", "receipt": verifier.verify(record)}


def _emit(result: dict, output: Path | None, *, protect: Path | None = None) -> int:
    try:
        if output is not None and (protect is None or output.resolve() != protect.resolve()):
            _atomic_json(output, result)
    except Exception:
        result = {"status": "not_ready", "error_code": "report_write_failed"}
        print(json.dumps(result, sort_keys=True))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0 if result.get("status") in {"source_ready", "candidate_sources_match"} else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--site-url", required=True, help="Trusted canonical HTTPS site origin")
    parser.add_argument("--manifest-url", help="Deployed manifest URL")
    parser.add_argument("--candidate", type=Path, help="Validated local knowledge candidate")
    parser.add_argument("--output", type=Path, help="Optional JSON report path")
    args = parser.parse_args(argv)
    output_conflicts = (args.output is not None and args.candidate is not None
                        and args.output.resolve() == args.candidate.resolve())
    try:
        if output_conflicts:
            raise KnowledgeValidationError("output_conflict")
        result = check(args.site_url, args.manifest_url, args.candidate)
    except VersioningError as exc:
        result = {"status": "not_ready", "error_code": exc.code}
    except KnowledgeValidationError as exc:
        result = {"status": "not_ready", "error_code": exc.code}
    except Exception as exc:
        code = getattr(exc, "code", None)
        if not isinstance(exc, (VersioningError, KnowledgeValidationError)) or not isinstance(code, str):
            code = "production_check_failed"
        result = {"status": "not_ready", "error_code": code}
    return _emit(result, args.output, protect=args.candidate if output_conflicts else None)


if __name__ == "__main__":
    raise SystemExit(main())
