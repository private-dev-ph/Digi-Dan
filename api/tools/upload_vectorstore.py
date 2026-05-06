import argparse
import json
from pathlib import Path

from api.core.config import get_settings
from api.core.models import Document
from api.tools.vector_store import upsert_documents


def main() -> None:
    parser = argparse.ArgumentParser(description="Upload documents to Upstash Vector.")
    parser.add_argument("path", help="Path to .txt, .json, or .jsonl file")
    parser.add_argument("--namespace", default=None, help="Upstash namespace")
    args = parser.parse_args()

    settings = get_settings()
    namespace = args.namespace or settings.upstash_namespace
    documents = _load_documents(Path(args.path))
    count = upsert_documents(documents, namespace, settings)
    print(f"Upserted {count} documents into namespace '{namespace}'")


def _load_documents(path: Path) -> list[Document]:
    suffix = path.suffix.lower()
    if suffix == ".txt":
        return [Document(id=path.stem, text=path.read_text(encoding="utf-8"), metadata={"source": str(path)})]
    if suffix == ".jsonl":
        return [
            _document_from_obj(json.loads(line), fallback_id=f"{path.stem}-{index}")
            for index, line in enumerate(path.read_text(encoding="utf-8").splitlines())
            if line.strip()
        ]
    if suffix == ".json":
        data = json.loads(path.read_text(encoding="utf-8"))
        items = data if isinstance(data, list) else data.get("documents", [])
        return [_document_from_obj(item, fallback_id=f"{path.stem}-{index}") for index, item in enumerate(items)]
    raise ValueError("Supported files: .txt, .json, .jsonl")


def _document_from_obj(obj: dict, fallback_id: str) -> Document:
    return Document(
        id=obj.get("id") or fallback_id,
        text=obj["text"],
        metadata=obj.get("metadata", {}),
    )


if __name__ == "__main__":
    main()
