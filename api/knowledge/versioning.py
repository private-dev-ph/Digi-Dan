"""Versioned, isolated knowledge builds and activation over injected local ports."""
from __future__ import annotations

import copy
import math
import re
import secrets
import time

from api.knowledge.manifest import canonical_hash
from api.knowledge.ports import MANAGED_NAMESPACE_PREFIX, VersioningError, initial_state
from api.knowledge.resume_source import validate_url


def _fail(code):
    raise VersioningError(code)


def _hash(value):
    try:
        return canonical_hash(value)
    except Exception as exc:
        raise VersioningError("invalid_candidate") from exc


def _validate(candidate, profile, probes):
    if not isinstance(candidate, dict) or candidate.get("kind") != "knowledge-candidate" or type(candidate.get("schema_version")) is not int or candidate["schema_version"] != 1 or candidate.get("complete") is not True or candidate.get("review_status") != "policy-validated":
        _fail("invalid_candidate")
    if not isinstance(candidate.get("policy_version"), str) or not candidate["policy_version"].strip() or len(candidate["policy_version"]) > 256:
        _fail("invalid_candidate")
    provenance = candidate.get("provenance")
    if not isinstance(provenance, dict) or not isinstance(provenance.get("portfolio_source"), dict) or not isinstance(provenance.get("resume_source"), dict) or not isinstance(provenance.get("resume_extractor"), dict):
        _fail("invalid_provenance")
    for key in ("portfolio_content_hash", "resume_content_hash", "legacy_content_hash", "policy_hash"):
        if not isinstance(provenance.get(key), str) or not re.fullmatch(r"[0-9a-f]{64}", provenance[key]):
            _fail("invalid_provenance")
    if not isinstance(provenance["portfolio_source"].get("site_url"), str) or not isinstance(provenance["resume_source"].get("url"), str) or not isinstance(provenance["resume_source"].get("pdf_sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", provenance["resume_source"]["pdf_sha256"]):
        _fail("invalid_provenance")
    try:
        validate_url(provenance["portfolio_source"]["site_url"], allow_local_http=True)
        validate_url(provenance["resume_source"]["url"], allow_local_http=True)
    except Exception as exc:
        raise VersioningError("invalid_provenance") from exc
    if not isinstance(profile, dict) or set(profile) != {"provider", "model", "dimensions", "policy_version"} or any(not isinstance(profile[k], str) or not profile[k].strip() for k in ("provider", "model", "policy_version")) or len(profile.get("policy_version", "")) > 256 or type(profile.get("dimensions")) is not int or profile["dimensions"] <= 0:
        _fail("invalid_embedding_profile")
    chunks = candidate.get("chunks")
    if not isinstance(chunks, list) or not chunks or len(chunks) > 20000:
        _fail("invalid_chunks")
    ids, checked = set(), []
    parents = {}
    for chunk in chunks:
        if not isinstance(chunk, dict) or not isinstance(chunk.get("id"), str) or not chunk["id"].strip() or chunk["id"] in ids or not isinstance(chunk.get("text"), str) or not chunk["text"].strip() or len(chunk["text"]) > 1200 or not isinstance(chunk.get("metadata"), dict):
            _fail("invalid_chunk")
        metadata = chunk["metadata"]
        required_metadata = {"section", "entity_id", "source_url", "owner", "title", "parent_id", "chunk_index", "chunk_count"}
        if set(metadata) != required_metadata or any(not isinstance(metadata[k], str) or not metadata[k].strip() for k in required_metadata - {"chunk_index", "chunk_count"}) or type(metadata["chunk_index"]) is not int or type(metadata["chunk_count"]) is not int or metadata["chunk_count"] < 1 or not 1 <= metadata["chunk_index"] <= metadata["chunk_count"]:
            _fail("invalid_chunk_metadata")
        parent = metadata["parent_id"]
        if metadata["owner"] not in {"portfolio", "resume", "curated"}:
            _fail("invalid_chunk_metadata")
        try:
            validate_url(metadata["source_url"], allow_local_http=True)
        except Exception as exc:
            raise VersioningError("invalid_chunk_metadata") from exc
        expected_id = parent if metadata["chunk_count"] == 1 else "{}:chunk:{:04d}".format(parent, metadata["chunk_index"])
        if chunk["id"] != expected_id:
            _fail("invalid_chunk_metadata")
        shape = (metadata["chunk_count"], metadata["section"], metadata["entity_id"], metadata["source_url"], metadata["owner"], metadata["title"])
        if parent in parents and parents[parent]["shape"] != shape:
            _fail("invalid_chunk_metadata")
        group = parents.setdefault(parent, {"shape": shape, "indices": set()})
        group["indices"].add(metadata["chunk_index"])
        ids.add(chunk["id"])
        payload = {"id": chunk["id"], "text": chunk["text"], "metadata": chunk["metadata"]}
        if chunk.get("content_hash") != _hash(payload):
            _fail("chunk_hash_mismatch")
        checked.append(payload)
    for group in parents.values():
        count = group["shape"][0]
        if len(group["indices"]) != count or group["indices"] != set(range(1, count + 1)):
            _fail("invalid_chunk_metadata")
    checked.sort(key=lambda c: c["id"])
    chunks = sorted(chunks, key=lambda c: c["id"])
    if candidate.get("content_hash") != _hash(checked):
        _fail("candidate_hash_mismatch")
    if not isinstance(probes, list) or not probes:
        _fail("invalid_probes")
    for probe in probes:
        if not isinstance(probe, dict) or not isinstance(probe.get("question"), str) or not probe["question"].strip() or not isinstance(probe.get("expected_ids"), list) or not probe["expected_ids"] or type(probe.get("top_k")) is not int or not 1 <= probe["top_k"] <= 20 or any(not isinstance(i, str) or i not in ids for i in probe["expected_ids"]) or len(set(probe["expected_ids"])) != len(probe["expected_ids"]):
            _fail("invalid_probe")
    version_id = _hash({"content_hash": candidate["content_hash"], "embedding": profile, "pipeline_policy_version": candidate.get("policy_version")})
    return chunks, candidate["content_hash"], version_id


class KnowledgeService:
    def __init__(self, registry, vectors, embedder, *, clock=time.time, sleep=time.sleep, batch_size=32, poll_attempts=5, poll_interval=0.1):
        if type(batch_size) is not int or batch_size < 1 or type(poll_attempts) is not int or poll_attempts < 1 or type(poll_interval) not in (int, float) or not math.isfinite(poll_interval) or poll_interval < 0:
            _fail("invalid_service_configuration")
        self.registry, self.vectors, self.embedder = registry, vectors, embedder
        self.clock, self.sleep = clock, sleep
        self.batch_size, self.poll_attempts, self.poll_interval = batch_size, poll_attempts, poll_interval

    def _read(self):
        revision, state = self.registry.read()
        if not isinstance(state, dict) or state.get("schema_version") != 1 or not isinstance(state.get("versions"), dict):
            _fail("registry_corrupt")
        return revision, state

    def _update(self, fn, retries=20):
        for _ in range(retries):
            revision, state = self._read()
            updated = copy.deepcopy(state)
            result = fn(updated)
            if self.registry.compare_and_swap(revision, updated):
                return result, updated
        _fail("registry_conflict")

    def _intent(self, ticket, expected_generation=None):
        def change(s):
            if expected_generation is not None and s.get("generation", 0) != expected_generation:
                _fail("generation_conflict")
            s["generation"] = s.get("generation", 0) + 1
            s["desired"] = ticket
        return self._update(change)[1]

    def _record(self, ticket):
        _, state = self._read()
        record = state.get("versions", {}).get(ticket)
        if not record:
            _fail("unknown_ticket")
        return record

    def _set_record(self, ticket, update, desired=None):
        def change(s):
            rec = s.get("versions", {}).get(ticket)
            if rec is None:
                _fail("unknown_ticket")
            if desired is not None and s.get("desired") != desired:
                return False
            update(rec, s)
            return True
        return self._update(change)

    def _active_if_same(self, candidate_hash, embedding):
        _, state = self._read()
        ticket = state.get("active")
        rec = state.get("versions", {}).get(ticket) if ticket else None
        if rec and rec.get("status") == "verified" and rec.get("candidate_hash") == candidate_hash and rec.get("embedding") == embedding:
            try:
                self._validate_storage(rec)
                return copy.deepcopy(rec)
            except VersioningError:
                return None
        return None

    def sync(self, candidate, profile, *, expected_generation, verify_source, probes, activate=True):
        chunks, candidate_hash, version_id = _validate(candidate, profile, probes)
        if type(expected_generation) is not int or expected_generation < 0:
            _fail("invalid_generation")
        source = copy.deepcopy(candidate["provenance"])
        prior = self._active_if_same(candidate_hash, profile)
        if prior and prior.get("version_id") != version_id:
            prior = None
        ticket = secrets.token_hex(8)
        if prior:
            candidate_record = copy.deepcopy(prior)
            candidate_record["source"] = source
            self._verify_source(verify_source, candidate_record)
            self._run_probes(prior, probes)
            def noop(s):
                if s.get("generation", 0) != expected_generation or s.get("active") != prior["ticket"]:
                    _fail("generation_conflict")
                # A content-identical build can carry a newer deployment
                # receipt. Keep the immutable content identity while
                # refreshing the receipt used by later activation checks.
                s["versions"][prior["ticket"]]["source"] = copy.deepcopy(source)
                s["generation"] = expected_generation + 1
                s["desired"] = prior["ticket"]
                s["last_attempt"] = {"status": "unchanged", "ticket": prior["ticket"], "source": source, "at": self.clock()}
            self._update(noop)
            return self._result("unchanged", prior)
        namespace = "{}{}-{}".format(MANAGED_NAMESPACE_PREFIX, version_id[:12], ticket)
        record = {"ticket": ticket, "version_id": version_id, "namespace": namespace, "candidate_hash": candidate_hash,
                  "embedding": copy.deepcopy(profile), "source": source, "expected_ids": [c["id"] for c in chunks],
                  "expected_count": len(chunks), "expected_hashes": {c["id"]: c["content_hash"] for c in chunks},
                  "probes": copy.deepcopy(probes), "status": "building",
                  "created_at": self.clock(), "verified_at": None, "retired_at": None, "error_code": None}
        def add(s):
            if s.get("generation", 0) != expected_generation:
                _fail("generation_conflict")
            s["generation"] = expected_generation + 1
            s["desired"] = ticket
            s.setdefault("versions", {})[ticket] = copy.deepcopy(record)
            s["last_attempt"] = {"status": "building", "ticket": ticket, "at": self.clock()}
        self._update(add)
        try:
            self._verify_source(verify_source, record)
            for offset in range(0, len(chunks), self.batch_size):
                batch = chunks[offset:offset + self.batch_size]
                vectors = self.embedder.embed([c["text"] for c in batch], profile)
                if not isinstance(vectors, list) or len(vectors) != len(batch):
                    _fail("invalid_vectors")
                rows = []
                for chunk, vector in zip(batch, vectors):
                    self._check_vector(vector, profile["dimensions"])
                    rows.append({**copy.deepcopy(chunk), "vector": list(vector)})
                self.vectors.put(namespace, rows)
            self._check_desired(ticket)
            self._wait_ready(namespace, len(chunks))
            self._validate_storage(record, chunks)
            self._run_probes(record)
            def verified(rec, state):
                if rec["status"] in ("deleting", "deleted", "superseded"):
                    _fail("stale_job")
                rec.update(status="verified", verified_at=self.clock(), error_code=None)
            ok, _ = self._set_record(ticket, verified, desired=ticket)
            if not ok:
                self._fail_record(ticket, "superseded")
                return self._result("superseded", record)
            if not activate:
                return self._result("staged", self._record(ticket))
            return self.activate(ticket, verify_source=verify_source)
        except Exception as exc:
            code = getattr(exc, "code", "build_failed")
            self._fail_record(ticket, code)
            if isinstance(exc, VersioningError):
                raise
            raise VersioningError(code) from exc

    def _check_desired(self, ticket):
        if self._read()[1].get("desired") != ticket:
            _fail("superseded")

    @staticmethod
    def _verify_source(verifier, source):
        try:
            valid = verifier(source)
        except Exception as exc:
            raise VersioningError("source_stale") from exc
        if valid is not True:
            _fail("source_stale")

    @staticmethod
    def _check_vector(vector, dimensions):
        if not isinstance(vector, (list, tuple)) or len(vector) != dimensions or any(type(v) not in (float, int) or not math.isfinite(v) for v in vector) or not any(v != 0 for v in vector) or not math.isfinite(math.hypot(*(float(v) for v in vector))):
            _fail("invalid_vectors")

    def _wait_ready(self, namespace, count):
        for attempt in range(self.poll_attempts):
            status = self.vectors.inspect(namespace)
            if isinstance(status, dict) and status.get("ready") == count and status.get("pending") == 0:
                return
            if attempt + 1 < self.poll_attempts:
                self.sleep(self.poll_interval)
        _fail("vectors_not_ready")

    def _validate_storage(self, rec, chunks=None):
        if (not isinstance(rec, dict) or not isinstance(rec.get("namespace"), str)
                or not rec["namespace"].startswith(MANAGED_NAMESPACE_PREFIX)
                or not isinstance(rec.get("expected_ids"), list)
                or any(not isinstance(i, str) for i in rec["expected_ids"])
                or type(rec.get("expected_count")) is not int
                or not isinstance(rec.get("expected_hashes"), dict)
                or set(rec["expected_ids"]) != set(rec["expected_hashes"])
                or len(rec["expected_ids"]) != rec["expected_count"]
                or not isinstance(rec.get("embedding"), dict)
                or type(rec["embedding"].get("dimensions")) is not int):
            _fail("invalid_version_record")
        rows = self.vectors.scan(rec["namespace"])
        expected = set(rec["expected_ids"])
        if not isinstance(rows, list) or len(rows) != rec["expected_count"] or {r.get("id") for r in rows if isinstance(r, dict)} != expected:
            _fail("vector_contents_mismatch")
        expected_hashes = rec.get("expected_hashes", {})
        for row in rows:
            actual_hash = _hash({"id": row.get("id"), "text": row.get("text"), "metadata": row.get("metadata")})
            expected_hash = expected_hashes.get(row["id"])
            if expected_hash is None or actual_hash != expected_hash:
                _fail("vector_contents_mismatch")
        for row in rows:
            self._check_vector(row.get("vector"), rec["embedding"]["dimensions"])

    def _run_probes(self, rec, probes=None):
        selected = probes if probes is not None else rec.get("probes")
        if not isinstance(selected, list) or not selected:
            _fail("invalid_version_record")
        for probe in selected:
            vectors = self.embedder.embed([probe["question"]], rec["embedding"])
            if not isinstance(vectors, list) or len(vectors) != 1:
                _fail("invalid_vectors")
            self._check_vector(vectors[0], rec["embedding"]["dimensions"])
            results = self.vectors.query(rec["namespace"], vectors[0], probe["top_k"])
            got = {r.get("id") for r in results if isinstance(r, dict)}
            if not set(probe["expected_ids"]).issubset(got):
                _fail("probe_failed")

    def _fail_record(self, ticket, code):
        def fail(rec, state):
            if rec.get("status") not in ("verified", "deleting", "deleted"):
                rec.update(status="failed", error_code=code)
            if state.get("desired") == ticket:
                state["last_attempt"] = {"status": "failed", "ticket": ticket, "error_code": code, "at": self.clock()}
        try:
            self._set_record(ticket, fail)
        except VersioningError:
            pass

    def activate(self, ticket, *, verify_source, expected_generation=None):
        if expected_generation is not None and (type(expected_generation) is not int or expected_generation < 0):
            _fail("invalid_generation")
        rec = self._record(ticket)
        if expected_generation is not None and self._read()[1].get("generation", 0) != expected_generation:
            _fail("generation_conflict")
        if rec.get("status") != "verified":
            _fail("version_not_verified")
        if self._read()[1].get("desired") != ticket:
            _fail("superseded")
        self._validate_storage(rec)
        self._run_probes(rec)
        self._verify_source(verify_source, rec)
        def change(s):
            if expected_generation is not None and s.get("generation", 0) != expected_generation:
                _fail("generation_conflict")
            current = s.get("versions", {}).get(ticket)
            if s.get("desired") != ticket or current is None or current.get("status") != "verified":
                _fail("superseded")
            old = s.get("active")
            if old and old != ticket:
                oldrec = s["versions"].get(old)
                if oldrec:
                    oldrec["retired_at"] = self.clock()
                s["history"] = [old] + [x for x in s.get("history", []) if x != old and x != ticket]
            s["active"] = ticket
            s["last_attempt"] = {"status": "active", "ticket": ticket, "at": self.clock()}
            return copy.deepcopy(current)
        active, _ = self._update(change)
        return self._result("active", active)

    def rollback(self, ticket, *, expected_generation, verify_source):
        if type(expected_generation) is not int or expected_generation < 0:
            _fail("invalid_generation")
        rec = self._record(ticket)
        state = self._read()[1]
        if rec.get("status") != "verified" or ticket not in ([state.get("active")] + state.get("history", [])):
            _fail("version_not_verified")
        self._validate_storage(rec)
        self._run_probes(rec)
        self._verify_source(verify_source, rec)
        def intent(s):
            current = s.get("versions", {}).get(ticket)
            if s.get("generation", 0) != expected_generation:
                _fail("generation_conflict")
            if not current or current.get("status") != "verified" or ticket not in ([s.get("active")] + s.get("history", [])):
                _fail("version_not_verified")
            s["generation"] = expected_generation + 1
            s["desired"] = ticket
        self._update(intent)
        return self.activate(ticket, verify_source=verify_source,
                             expected_generation=expected_generation + 1)

    def get_active(self):
        state = self._read()[1]
        ticket = state.get("active")
        rec = state.get("versions", {}).get(ticket) if ticket else None
        if not self._valid_record(rec, ticket):
            _fail("no_active_version")
        return copy.deepcopy(rec)

    def retrieve(self, question, top_k):
        if type(top_k) is not int or not 0 <= top_k <= 20 or not isinstance(question, str) or not question.strip():
            _fail("invalid_query")
        rec = self.get_active()
        if top_k == 0:
            return rec, []
        try:
            vectors = self.embedder.embed([question], rec["embedding"])
            if not isinstance(vectors, list) or len(vectors) != 1:
                _fail("invalid_vectors")
            self._check_vector(vectors[0], rec["embedding"]["dimensions"])
            results = self.vectors.query(rec["namespace"], vectors[0], top_k)
            if not isinstance(results, list) or not results or len(results) > top_k:
                _fail("invalid_query_results")
            hashes = rec["expected_hashes"]
            seen = set()
            for row in results:
                if not isinstance(row, dict) or row.get("id") not in hashes or row["id"] in seen:
                    _fail("invalid_query_results")
                seen.add(row["id"])
                self._check_vector(row.get("vector"), rec["embedding"]["dimensions"])
                score = row.get("score")
                if type(score) not in (int, float) or not math.isfinite(score):
                    _fail("invalid_query_results")
                if hashes[row["id"]] != _hash({"id": row.get("id"), "text": row.get("text"), "metadata": row.get("metadata")}):
                    _fail("vector_contents_mismatch")
            return rec, results
        except VersioningError:
            raise
        except Exception as exc:
            raise VersioningError("retrieval_failed") from exc

    @staticmethod
    def _valid_record(rec, ticket):
        if not isinstance(rec, dict) or rec.get("ticket") != ticket or rec.get("status") != "verified":
            return False
        if not isinstance(rec.get("namespace"), str) or not rec["namespace"].startswith(MANAGED_NAMESPACE_PREFIX):
            return False
        if not isinstance(rec.get("version_id"), str) or not re.fullmatch(r"[0-9a-f]{64}", rec["version_id"]):
            return False
        if not isinstance(rec.get("candidate_hash"), str) or not re.fullmatch(r"[0-9a-f]{64}", rec["candidate_hash"]):
            return False
        embedding = rec.get("embedding")
        if not isinstance(embedding, dict) or set(embedding) != {"provider", "model", "dimensions", "policy_version"} or any(not isinstance(embedding.get(k), str) or not embedding[k] for k in ("provider", "model", "policy_version")) or type(embedding.get("dimensions")) is not int or not 1 <= embedding["dimensions"] <= 65536:
            return False
        ids, hashes = rec.get("expected_ids"), rec.get("expected_hashes")
        if not isinstance(ids, list) or any(not isinstance(i, str) for i in ids) or type(rec.get("expected_count")) is not int or not 1 <= rec["expected_count"] <= 20000 or len(ids) != rec["expected_count"] or len(set(ids)) != len(ids) or not isinstance(hashes, dict) or set(hashes) != set(ids):
            return False
        return all(isinstance(i, str) and isinstance(hashes[i], str) and re.fullmatch(r"[0-9a-f]{64}", hashes[i]) for i in ids)

    def cleanup(self, *, retain_previous=2, grace_seconds=3600):
        if type(retain_previous) is not int or retain_previous < 0 or type(grace_seconds) not in (int, float) or not math.isfinite(grace_seconds) or grace_seconds < 0:
            _fail("invalid_cleanup_policy")
        state = self._read()[1]
        protected = {state.get("active"), state.get("desired"), *state.get("history", [])[:retain_previous]}
        deleted = []
        for ticket, rec in state.get("versions", {}).items():
            if ticket in protected or rec.get("status") in ("building", "deleted"):
                continue
            namespace = rec.get("namespace")
            if not isinstance(namespace, str) or not namespace.startswith(MANAGED_NAMESPACE_PREFIX):
                continue
            def mark(r, s):
                retained = {s.get("active"), s.get("desired"), *s.get("history", [])[:retain_previous]}
                age_from = (r.get("retired_at") or r.get("verified_at") or r.get("created_at")) if r.get("status") == "verified" else r.get("created_at")
                if ticket in retained:
                    _fail("cleanup_race")
                if r.get("status") == "deleting":
                    return
                if r.get("status") in ("verified", "failed", "superseded") and age_from is not None and self.clock() - age_from >= grace_seconds:
                    r["status"] = "deleting"
                else:
                    _fail("cleanup_race")
            try:
                self._set_record(ticket, mark)
                self.vectors.delete(namespace)
                self._set_record(ticket, lambda r, s: r.update(status="deleted"))
                deleted.append(namespace)
            except Exception:
                continue
        return deleted

    @staticmethod
    def _result(status, rec):
        return {"status": status, "ticket": rec["ticket"], "version_id": rec["version_id"], "namespace": rec["namespace"]}
