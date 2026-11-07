"""Storage and embedding seams for versioned knowledge; no cloud clients here."""
from typing import Callable, Protocol

MANAGED_NAMESPACE_PREFIX = "knowledge-"


class VersioningError(ValueError):
    def __init__(self, code: str, message: str = "Knowledge version operation failed"):
        super().__init__(message)
        self.code = code


def initial_state() -> dict:
    return {"schema_version": 1, "generation": 0, "desired": None, "active": None,
            "history": [], "versions": {}, "last_attempt": None}


class Registry(Protocol):
    def read(self) -> tuple[int, dict]: ...
    def compare_and_swap(self, revision: int, state: dict) -> bool: ...


class VectorStore(Protocol):
    def put(self, namespace: str, records: list[dict]) -> None: ...
    def inspect(self, namespace: str) -> dict: ...
    def scan(self, namespace: str) -> list[dict]: ...
    def query(self, namespace: str, vector: list[float], top_k: int) -> list[dict]: ...
    def delete(self, namespace: str) -> None: ...


class Embedder(Protocol):
    def embed(self, texts: list[str], profile: dict) -> list[list[float]]: ...


# A caller must verify that the version's recorded sources still match its
# intended environment. Local tools supply an explicit local receipt check;
# production must supply a deployed-source verifier before activation.
SourceVerifier = Callable[[dict], bool]
