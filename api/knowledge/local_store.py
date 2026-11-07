"""Durable, isolated stdlib adapters for local Stage 3 knowledge workflows."""

import hashlib
import json
import math
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .ports import MANAGED_NAMESPACE_PREFIX, VersioningError, initial_state
from .registry_state import _json_copy, _loads_strict, _validate_state


def _connect(path):
    con = sqlite3.connect(str(path), timeout=30, isolation_level=None)
    con.execute("PRAGMA busy_timeout=30000")
    return con


@contextmanager
def _connection(path):
    con = _connect(path)
    try:
        yield con
    finally:
        con.close()


class SQLiteRegistry:
    """A revisioned registry with real SQLite compare-and-swap semantics."""

    def __init__(self, path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with _connection(self.path) as con:
            con.execute("CREATE TABLE IF NOT EXISTS knowledge_registry (id INTEGER PRIMARY KEY CHECK(id=1), revision INTEGER NOT NULL, state TEXT NOT NULL)")
            con.execute("INSERT OR IGNORE INTO knowledge_registry(id, revision, state) VALUES(1, 0, ?)",
                        (json.dumps(initial_state(), separators=(",", ":")),))

    def read(self):
        try:
            with _connection(self.path) as con:
                row = con.execute("SELECT revision,state FROM knowledge_registry WHERE id=1").fetchone()
            if row is None or type(row[0]) is not int or row[0] < 0:
                raise ValueError("invalid registry row")
            return row[0], _validate_state(_loads_strict(row[1]))
        except VersioningError:
            raise
        except Exception as exc:
            raise VersioningError("registry_corrupt", "Registry data is malformed") from exc

    def compare_and_swap(self, revision, state):
        if type(revision) is not int or revision < 0:
            raise VersioningError("invalid_revision", "Revision must be a nonnegative integer")
        safe = _validate_state(state)
        payload = json.dumps(safe, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        try:
            con = _connect(self.path)
            try:
                con.execute("BEGIN IMMEDIATE")
                cur = con.execute("UPDATE knowledge_registry SET revision=revision+1,state=? WHERE id=1 AND revision=?", (payload, revision))
                con.commit()
                return cur.rowcount == 1
            except Exception:
                con.rollback()
                raise
            finally:
                con.close()
        except sqlite3.Error as exc:
            raise VersioningError("registry_error", "Registry update failed") from exc


def _valid_vector(vector, dimensions=None):
    if not isinstance(vector, (list, tuple)) or not vector:
        return False
    if dimensions is not None and len(vector) != dimensions:
        return False
    if len(vector) > 4096 or any(type(x) not in (int, float) for x in vector):
        return False
    try:
        if any(not math.isfinite(float(x)) for x in vector):
            return False
        norm = math.hypot(*(float(x) for x in vector))
    except (OverflowError, ValueError):
        return False
    return norm > 0 and math.isfinite(norm)


class SQLiteVectorStore:
    """Persistent vectors partitioned by namespace in one local SQLite file."""

    def __init__(self, path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with _connection(self.path) as con:
            con.execute("CREATE TABLE IF NOT EXISTS knowledge_vectors (namespace TEXT NOT NULL, id TEXT NOT NULL, text TEXT NOT NULL, metadata TEXT NOT NULL, vector TEXT NOT NULL, PRIMARY KEY(namespace,id))")

    def put(self, namespace, records):
        if not isinstance(namespace, str) or not namespace.startswith(MANAGED_NAMESPACE_PREFIX):
            raise VersioningError("invalid_namespace", "Writes require a managed knowledge namespace")
        if not isinstance(records, list):
            raise VersioningError("invalid_records", "Records must be a list")
        prepared = []
        batch_dims = None
        for record in records:
            if not isinstance(record, dict) or not isinstance(record.get("id"), str) or not record["id"] or not isinstance(record.get("text"), str) or not isinstance(record.get("metadata"), dict):
                raise VersioningError("invalid_record", "Malformed vector record")
            vector = record.get("vector")
            if not _valid_vector(vector, batch_dims):
                raise VersioningError("invalid_vector", "Vectors must be finite, nonzero, and have consistent dimensions")
            if batch_dims is None:
                batch_dims = len(vector)
            prepared.append((namespace, record["id"], record["text"],
                             json.dumps(_json_copy(record["metadata"]), ensure_ascii=False, allow_nan=False),
                             json.dumps(list(vector), allow_nan=False)))
        try:
            con = _connect(self.path)
            try:
                con.execute("BEGIN IMMEDIATE")
                existing = con.execute("SELECT vector FROM knowledge_vectors WHERE namespace=? LIMIT 1", (namespace,)).fetchone()
                if existing is not None and batch_dims is not None:
                    existing_vector = _loads_strict(existing[0])
                    if not isinstance(existing_vector, list) or len(existing_vector) != batch_dims:
                        raise VersioningError("invalid_vector", "Vector dimension differs from existing namespace records")
                con.executemany("INSERT INTO knowledge_vectors(namespace,id,text,metadata,vector) VALUES(?,?,?,?,?) ON CONFLICT(namespace,id) DO UPDATE SET text=excluded.text,metadata=excluded.metadata,vector=excluded.vector", prepared)
                con.commit()
            except Exception:
                con.rollback()
                raise
            finally:
                con.close()
        except VersioningError:
            raise
        except Exception as exc:
            raise VersioningError("vector_store_error", "Vector write failed") from exc

    def inspect(self, namespace):
        try:
            with _connection(self.path) as con:
                count = con.execute("SELECT COUNT(*) FROM knowledge_vectors WHERE namespace=?", (namespace,)).fetchone()[0]
            return {"ready": count, "pending": 0}
        except Exception as exc:
            raise VersioningError("vector_store_error", "Vector inspection failed") from exc

    def scan(self, namespace):
        try:
            with _connection(self.path) as con:
                rows = con.execute("SELECT id,text,metadata,vector FROM knowledge_vectors WHERE namespace=? ORDER BY id", (namespace,)).fetchall()
            records = [{"id": i, "text": t, "metadata": _loads_strict(m), "vector": _loads_strict(v)} for i, t, m, v in rows]
            for record in records:
                if not isinstance(record["metadata"], dict) or not _valid_vector(record["vector"]):
                    raise ValueError("malformed vector record")
            return records
        except Exception as exc:
            raise VersioningError("vector_store_corrupt", "Stored vector data is malformed") from exc

    def query(self, namespace, vector, top_k):
        if not _valid_vector(vector):
            raise VersioningError("invalid_vector", "Query vector must be finite and nonzero")
        if type(top_k) is not int or top_k < 0:
            raise VersioningError("invalid_top_k", "top_k must be a nonnegative integer")
        qnorm = math.hypot(*(float(x) for x in vector))
        results = []
        for record in self.scan(namespace):
            stored = record["vector"]
            if not _valid_vector(stored, len(vector)):
                raise VersioningError("vector_store_corrupt", "Stored vector dimensions or values are invalid")
            norm = math.hypot(*(float(x) for x in stored))
            score = sum((float(a) / qnorm) * (float(b) / norm) for a, b in zip(vector, stored))
            results.append({**record, "score": score})
        results.sort(key=lambda row: (-row["score"], row["id"]))
        return results[:top_k]

    def delete(self, namespace):
        if not isinstance(namespace, str) or not namespace.startswith(MANAGED_NAMESPACE_PREFIX):
            raise VersioningError("invalid_namespace", "Deletes require a managed knowledge namespace")
        try:
            with _connection(self.path) as con:
                con.execute("BEGIN IMMEDIATE")
                con.execute("DELETE FROM knowledge_vectors WHERE namespace=?", (namespace,))
                con.commit()
        except Exception as exc:
            raise VersioningError("vector_store_error", "Vector deletion failed") from exc


class LocalEmbedder:
    """Deterministic normalized token hash embeddings for local workflows."""

    PROFILE = {"provider": "local-test", "model": "token-hash-v1", "policy_version": "1"}
    _token = re.compile(r"\w+", re.UNICODE)

    def embed(self, texts, profile):
        if not isinstance(profile, dict) or set(profile) != {"provider", "model", "policy_version", "dimensions"} or any(profile.get(k) != v for k, v in self.PROFILE.items()):
            raise VersioningError("invalid_embedding_profile", "Unsupported local embedding profile")
        dims = profile.get("dimensions")
        if type(dims) is not int or not 1 <= dims <= 4096:
            raise VersioningError("invalid_embedding_profile", "Embedding dimensions must be from 1 to 4096")
        if not isinstance(texts, list) or any(not isinstance(text, str) for text in texts):
            raise VersioningError("invalid_texts", "Texts must be a list of strings")
        output = []
        for text in texts:
            buckets = [0.0] * dims
            for token in self._token.findall(text.lower()):
                digest = hashlib.sha256(token.encode("utf-8")).digest()
                bucket = int.from_bytes(digest[:8], "big") % dims
                buckets[bucket] += 1.0
            norm = math.sqrt(sum(value * value for value in buckets))
            if norm == 0:
                raise VersioningError("invalid_text", "Text must contain at least one token")
            output.append([value / norm for value in buckets])
        return output
