"""Namespace-isolated Upstash Vector adapter for versioned knowledge stores."""

import math
import re

from .ports import VersioningError


_LOGICAL_NAMESPACE = re.compile(r"knowledge-[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\Z")


def _vector(value, dimensions):
    if not isinstance(value, (list, tuple)) or len(value) != dimensions:
        return False
    if any(type(item) not in (int, float) for item in value):
        return False
    try:
        if not all(math.isfinite(float(item)) for item in value):
            return False
        norm = math.hypot(*(float(item) for item in value))
        return math.isfinite(norm) and norm > 0
    except (OverflowError, ValueError):
        return False


class UpstashVectorStore:
    """A bounded adapter whose callers can address only managed namespaces."""

    def __init__(self, index, *, environment, dimensions, writable=False, page_size=100, max_records=20000):
        if environment not in ("test", "production"):
            raise VersioningError("invalid_environment")
        if type(dimensions) is not int or not 1 <= dimensions <= 65536:
            raise VersioningError("invalid_dimensions")
        if type(writable) is not bool:
            raise VersioningError("invalid_configuration")
        if type(page_size) is not int or not 1 <= page_size <= 1000 or type(max_records) is not int or not 1 <= max_records <= 20000:
            raise VersioningError("invalid_configuration")
        self.index = index
        self.environment = environment
        self.dimensions = dimensions
        self.writable = writable
        self.page_size = page_size
        self.max_records = max_records

    def _namespace(self, logical):
        if not isinstance(logical, str) or not _LOGICAL_NAMESPACE.fullmatch(logical):
            raise VersioningError("invalid_namespace", "Namespace is not a managed knowledge namespace")
        physical = "digi-dan-{}-{}".format(self.environment, logical)
        if len(physical) > 63:
            raise VersioningError("invalid_namespace", "Namespace is not a managed knowledge namespace")
        return physical

    def _info(self):
        try:
            info = self.index.info()
            if type(getattr(info, "dimension", None)) is not int or not isinstance(getattr(info, "namespaces", None), dict):
                raise ValueError("invalid info")
            if info.dimension != self.dimensions:
                raise VersioningError("dimension_mismatch")
            return info
        except VersioningError:
            raise
        except Exception as exc:
            raise VersioningError("vector_store_error", "Vector inspection failed") from None

    @staticmethod
    def _namespace_counts(info, physical):
        entry = info.namespaces.get(physical)
        if entry is None:
            return 0, 0
        ready, pending = getattr(entry, "vector_count", None), getattr(entry, "pending_vector_count", None)
        if type(ready) is not int or ready < 0 or type(pending) is not int or pending < 0:
            raise VersioningError("vector_store_corrupt", "Namespace counts are malformed")
        return ready, pending

    def inspect(self, namespace):
        physical = self._namespace(namespace)
        info = self._info()
        ready, pending = self._namespace_counts(info, physical)
        return {"ready": ready, "pending": pending}

    def put(self, namespace, records):
        physical = self._namespace(namespace)
        if not self.writable:
            raise VersioningError("read_only")
        if not isinstance(records, list):
            raise VersioningError("invalid_records")
        if len(records) > self.max_records:
            raise VersioningError("invalid_records")
        prepared = []
        seen = set()
        for record in records:
            if (not isinstance(record, dict) or not isinstance(record.get("id"), str) or not record["id"]
                    or not isinstance(record.get("text"), str) or not record["text"] or not isinstance(record.get("metadata"), dict)
                    or not _vector(record.get("vector"), self.dimensions)):
                raise VersioningError("invalid_record")
            if record["id"] in seen:
                raise VersioningError("invalid_record")
            seen.add(record["id"])
            prepared.append({"id": record["id"], "vector": list(record["vector"]),
                             "metadata": record["metadata"], "data": record["text"]})
        if not prepared:
            return
        self._info()  # Verify the global index dimension before any write.
        try:
            result = self.index.upsert(prepared, namespace=physical)
            if result != "Success":
                raise ValueError("upsert was not successful")
        except Exception as exc:
            raise VersioningError("vector_store_error", "Vector write failed") from None

    def query(self, namespace, vector, top_k):
        physical = self._namespace(namespace)
        if not _vector(vector, self.dimensions):
            raise VersioningError("invalid_vector")
        if type(top_k) is not int or not 0 <= top_k <= self.max_records:
            raise VersioningError("invalid_top_k")
        if top_k == 0:
            return []
        self._info()
        try:
            raw = self.index.query(vector=list(vector), top_k=top_k, namespace=physical,
                                   include_vectors=True, include_metadata=True, include_data=True)
            if not isinstance(raw, list) or len(raw) > top_k:
                raise ValueError("invalid query result")
            result, seen = [], set()
            for item in raw:
                ident = getattr(item, "id", None)
                data = getattr(item, "data", None)
                metadata = getattr(item, "metadata", None)
                stored_vector = getattr(item, "vector", None)
                score = getattr(item, "score", None)
                if (not isinstance(ident, str) or not ident or ident in seen or not isinstance(data, str)
                        or not isinstance(metadata, dict) or not _vector(stored_vector, self.dimensions)
                        or type(score) not in (int, float) or not math.isfinite(float(score))):
                    raise ValueError("invalid query result")
                seen.add(ident)
                result.append({"id": ident, "text": data, "metadata": metadata,
                               "vector": list(stored_vector), "score": score})
            return result
        except VersioningError:
            raise
        except Exception as exc:
            raise VersioningError("vector_store_corrupt", "Query response is malformed") from None

    def scan(self, namespace):
        physical = self._namespace(namespace)
        self._info()
        cursor = ""
        cursors = {cursor}
        seen = set()
        rows = []
        pages = 0
        try:
            while True:
                pages += 1
                if pages > self.max_records + 1:
                    raise ValueError("range exceeds configured page limit")
                page = self.index.range(cursor=cursor, limit=self.page_size, namespace=physical,
                                        include_vectors=True, include_metadata=True, include_data=True)
                next_cursor = getattr(page, "next_cursor", None)
                vectors = getattr(page, "vectors", None)
                if not isinstance(next_cursor, str) or not isinstance(vectors, list) or len(vectors) > self.page_size:
                    raise ValueError("invalid range page")
                for item in vectors:
                    ident = getattr(item, "id", None)
                    data = getattr(item, "data", None)
                    metadata = getattr(item, "metadata", None)
                    stored_vector = getattr(item, "vector", None)
                    if (not isinstance(ident, str) or not ident or ident in seen or not isinstance(data, str)
                            or not isinstance(metadata, dict) or not _vector(stored_vector, self.dimensions)):
                        raise ValueError("invalid range record")
                    seen.add(ident)
                    rows.append({"id": ident, "text": data, "metadata": metadata, "vector": list(stored_vector)})
                    if len(rows) > self.max_records:
                        raise ValueError("range exceeds configured limit")
                if not next_cursor:
                    break
                if next_cursor in cursors:
                    raise ValueError("repeated cursor")
                cursors.add(next_cursor)
                cursor = next_cursor
            return rows
        except VersioningError:
            raise
        except Exception as exc:
            raise VersioningError("vector_store_corrupt", "Range response is malformed") from None

    def delete(self, namespace):
        physical = self._namespace(namespace)
        if not self.writable:
            raise VersioningError("read_only")
        self._info()
        try:
            self.index.delete_namespace(physical)
        except Exception as exc:
            raise VersioningError("vector_store_error", "Vector deletion failed") from None
