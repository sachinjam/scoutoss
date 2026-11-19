"""A small in-memory stand-in for ``opensearchpy.OpenSearch`` used by unit tests.

It implements just enough of the API surface our services touch (index/create/update,
bulk NDJSON, search with scroll, count, a subset of the query DSL and a few aggregations)
to test idempotency, rollups, the DLQ and the copilot executor without a cluster. The
integration suite runs the same code against a real OpenSearch.
"""

from __future__ import annotations

import copy
import fnmatch
import itertools
import json
from collections import Counter, defaultdict
from typing import Any


class NotFoundError(Exception):
    status_code = 404


class ConflictError(Exception):
    status_code = 409


def _get(doc: dict[str, Any], field: str) -> Any:
    if field.endswith(".raw") or field.endswith(".keyword"):
        field = field.rsplit(".", 1)[0]
    cur: Any = doc
    for part in field.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def _values(doc: dict[str, Any], field: str) -> list[Any]:
    v = _get(doc, field)
    if v is None:
        return []
    return list(v) if isinstance(v, list) else [v]


def _eq(a: Any, b: Any) -> bool:
    if isinstance(a, str) and isinstance(b, str):
        return a.upper() == b.upper()
    if isinstance(a, bool) or isinstance(b, bool):
        return a == b
    if isinstance(a, int | float) and isinstance(b, int | float):
        return float(a) == float(b)
    return str(a) == str(b)


def _cmp(value: Any, op: str, bound: Any) -> bool:
    try:
        if isinstance(bound, str) and not isinstance(value, str):
            bound = float(bound)
        a, b = (float(value), float(bound)) if not isinstance(bound, str) else (str(value), bound)
    except (TypeError, ValueError):
        return False
    return {"gt": a > b, "gte": a >= b, "lt": a < b, "lte": a <= b}[op]


def matches(doc: dict[str, Any], query: dict[str, Any] | None) -> bool:
    if not query:
        return True
    ((kind, spec),) = query.items()
    if kind == "match_all":
        return True
    if kind == "match_none":
        return False
    if kind == "term":
        ((field, value),) = spec.items()
        value = value.get("value") if isinstance(value, dict) else value
        return any(_eq(v, value) for v in _values(doc, field))
    if kind == "terms":
        spec = {k: v for k, v in spec.items() if k != "boost"}
        ((field, wanted),) = spec.items()
        return any(_eq(v, w) for v in _values(doc, field) for w in wanted)
    if kind == "range":
        ((field, bounds),) = spec.items()
        vals = _values(doc, field)
        return bool(vals) and all(
            any(_cmp(v, op, b) for v in vals)
            for op, b in bounds.items()
            if op in {"gt", "gte", "lt", "lte"}
        )
    if kind == "exists":
        return bool(_values(doc, spec["field"]))
    if kind in {"match", "match_phrase"}:
        ((field, value),) = spec.items()
        value = value.get("query") if isinstance(value, dict) else value
        return any(str(value).lower() in str(v).lower() for v in _values(doc, field))
    if kind == "wildcard":
        ((field, value),) = spec.items()
        value = value.get("value") if isinstance(value, dict) else value
        return any(fnmatch.fnmatch(str(v).lower(), str(value).lower()) for v in _values(doc, field))
    if kind == "prefix":
        ((field, value),) = spec.items()
        value = value.get("value") if isinstance(value, dict) else value
        return any(str(v).lower().startswith(str(value).lower()) for v in _values(doc, field))
    if kind == "ids":
        return doc.get("_id") in spec.get("values", [])
    if kind == "bool":

        def as_list(x: Any) -> list[dict[str, Any]]:
            return x if isinstance(x, list) else ([x] if x else [])

        if not all(
            matches(doc, q) for q in as_list(spec.get("must")) + as_list(spec.get("filter"))
        ):
            return False
        if any(matches(doc, q) for q in as_list(spec.get("must_not"))):
            return False
        should = as_list(spec.get("should"))
        if should:
            minimum = int(
                spec.get(
                    "minimum_should_match", 1 if not (spec.get("must") or spec.get("filter")) else 0
                )
            )
            if sum(matches(doc, q) for q in should) < minimum:
                return False
        return True
    raise ValueError(f"FakeOpenSearch: unsupported query {kind}")


def _aggregate(docs: list[dict[str, Any]], aggs: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name, spec in aggs.items():
        sub = spec.get("aggs") or spec.get("aggregations")
        ((kind, params),) = ((k, v) for k, v in spec.items() if k not in {"aggs", "aggregations"})
        if kind == "terms":
            counts: Counter[Any] = Counter()
            groups: dict[Any, list[dict[str, Any]]] = defaultdict(list)
            for d in docs:
                for v in set(_values(d, params["field"])):
                    counts[v] += 1
                    groups[v].append(d)
            buckets = []
            for key, n in counts.most_common(int(params.get("size", 10))):
                b: dict[str, Any] = {"key": key, "doc_count": n}
                if sub:
                    b.update(_aggregate(groups[key], sub))
                buckets.append(b)
            out[name] = {"buckets": buckets}
        elif kind == "histogram":
            interval = float(params["interval"])
            hist: Counter[float] = Counter()
            for d in docs:
                for v in _values(d, params["field"]):
                    hist[(float(v) // interval) * interval] += 1
            out[name] = {"buckets": [{"key": k, "doc_count": n} for k, n in sorted(hist.items())]}
        elif kind == "date_histogram":
            hist2: Counter[str] = Counter()
            for d in docs:
                for v in _values(d, params["field"]):
                    hist2[str(v)[:4] + "-01-01"] += 1
            out[name] = {
                "buckets": [{"key_as_string": k, "doc_count": n} for k, n in sorted(hist2.items())]
            }
        elif kind == "cardinality":
            out[name] = {"value": len({v for d in docs for v in _values(d, params["field"])})}
        elif kind == "value_count":
            out[name] = {"value": sum(len(_values(d, params["field"])) for d in docs)}
        elif kind in {"avg", "min", "max", "sum"}:
            nums = [float(v) for d in docs for v in _values(d, params["field"])]
            if not nums:
                out[name] = {"value": None}
            else:
                fn = {"avg": lambda x: sum(x) / len(x), "min": min, "max": max, "sum": sum}[kind]
                out[name] = {"value": fn(nums)}
        elif kind == "stats":
            nums = [float(v) for d in docs for v in _values(d, params["field"])]
            out[name] = {
                "count": len(nums),
                "min": min(nums) if nums else None,
                "max": max(nums) if nums else None,
                "avg": sum(nums) / len(nums) if nums else None,
                "sum": sum(nums),
            }
        elif kind == "filter":
            out[name] = {"doc_count": sum(matches(d, params) for d in docs)}
        else:
            raise ValueError(f"FakeOpenSearch: unsupported aggregation {kind}")
    return out


class _Indices:
    def __init__(self, parent: FakeOpenSearch) -> None:
        self.p = parent

    def exists(self, index: str, **_: Any) -> bool:
        return index in self.p.data

    def create(self, index: str, body: dict[str, Any] | None = None, **_: Any) -> dict[str, Any]:
        if index in self.p.data:
            raise Exception("resource_already_exists_exception")
        self.p.data[index] = {}
        self.p.mappings[index] = body or {}
        return {"acknowledged": True}

    def refresh(self, index: str | None = None, **_: Any) -> dict[str, Any]:
        self.p.refreshes += 1
        return {}

    def delete(self, index: str, **_: Any) -> dict[str, Any]:
        self.p.data.pop(index, None)
        return {"acknowledged": True}

    def get_mapping(self, index: str, **_: Any) -> dict[str, Any]:
        return {index: self.p.mappings.get(index, {})}


class _Transport:
    def __init__(self) -> None:
        from opensearchpy.serializer import JSONSerializer

        self.serializer = JSONSerializer()


class FakeOpenSearch:
    def __init__(self) -> None:
        self.transport = _Transport()
        self.data: dict[str, dict[str, dict[str, Any]]] = {}
        self.mappings: dict[str, dict[str, Any]] = {}
        self.indices = _Indices(self)
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.refreshes = 0
        self.fail_index: set[str] = set()
        self.reject_ids: set[str] = set()
        self._scrolls: dict[str, list[dict[str, Any]]] = {}
        self._ids = itertools.count(1)

    # ------------------------------------------------------------ helpers
    def docs(self, index: str) -> list[dict[str, Any]]:
        return [copy.deepcopy(d) for d in self.data.get(index, {}).values()]

    def _index_names(self, index: str | list[str] | None) -> list[str]:
        if index is None:
            return list(self.data)
        names = index if isinstance(index, list) else str(index).split(",")
        out: list[str] = []
        for n in names:
            out += [k for k in self.data if fnmatch.fnmatch(k, n)] if "*" in n else [n]
        return out

    def ping(self, **_: Any) -> bool:
        return True

    def info(self, **_: Any) -> dict[str, Any]:
        return {"version": {"number": "2.19.3", "distribution": "opensearch"}}

    # ------------------------------------------------------------ writes
    def index(
        self,
        index: str,
        body: dict[str, Any],
        id: str | None = None,
        op_type: str | None = None,
        **kw: Any,
    ) -> dict[str, Any]:
        self.calls.append(("index", {"index": index, "id": id, "op_type": op_type, **kw}))
        if index in self.fail_index:
            raise ConnectionError("simulated outage")
        store = self.data.setdefault(index, {})
        doc_id = id or f"auto{next(self._ids)}"
        if op_type == "create" and doc_id in store:
            raise ConflictError("version_conflict_engine_exception")
        store[doc_id] = copy.deepcopy(body)
        return {"_id": doc_id, "result": "created"}

    def update(self, index: str, id: str, body: dict[str, Any], **_: Any) -> dict[str, Any]:
        store = self.data.setdefault(index, {})
        if id in store:
            if "doc" in body:
                store[id].update(body["doc"])
            elif "script" in body:
                store[id]["attempts"] = int(store[id].get("attempts", 0)) + 1
                store[id].update(dict(body["script"].get("params", {}).items()))
            return {"_id": id, "result": "updated"}
        if "upsert" not in body:
            raise NotFoundError(id)
        store[id] = copy.deepcopy(body["upsert"])
        return {"_id": id, "result": "created"}

    def delete(self, index: str, id: str, **_: Any) -> dict[str, Any]:
        self.data.get(index, {}).pop(id, None)
        return {"result": "deleted"}

    def bulk(self, body: str | list[Any], **_: Any) -> dict[str, Any]:
        lines = (
            [json.loads(line) for line in body.splitlines() if line.strip()]
            if isinstance(body, str)
            else body
        )
        items = []
        it = iter(lines)
        for action in it:
            ((op, meta),) = action.items()
            index, doc_id = meta["_index"], meta.get("_id")
            source = next(it) if op in {"index", "create", "update"} else None
            if index in self.fail_index or (doc_id and doc_id in self.reject_ids):
                items.append(
                    {
                        op: {
                            "_index": index,
                            "_id": doc_id,
                            "status": 400,
                            "error": {"type": "mapper_parsing_exception"},
                        }
                    }
                )
                continue
            store = self.data.setdefault(index, {})
            doc_id = doc_id or f"auto{next(self._ids)}"
            if op == "delete":
                store.pop(doc_id, None)
            elif op == "update":
                assert source is not None
                store.setdefault(doc_id, {}).update(source.get("doc", {}))
            else:
                assert source is not None
                store[doc_id] = source
            items.append({op: {"_index": index, "_id": doc_id, "status": 201}})
        return {"errors": any(i[next(iter(i))]["status"] >= 300 for i in items), "items": items}

    # ------------------------------------------------------------ reads
    def _select(self, index: Any, body: dict[str, Any] | None) -> list[dict[str, Any]]:
        body = body or {}
        hits = []
        for name in self._index_names(index):
            for doc_id, src in self.data.get(name, {}).items():
                doc = {**src, "_id": doc_id}
                if matches(doc, body.get("query")):
                    hits.append({"_index": name, "_id": doc_id, "_source": copy.deepcopy(src)})
        return hits

    def search(
        self,
        body: dict[str, Any] | None = None,
        index: Any = None,
        scroll: str | None = None,
        size: int | None = None,
        **kw: Any,
    ) -> dict[str, Any]:
        self.calls.append(("search", {"index": index, "body": copy.deepcopy(body), **kw}))
        body = body or {}
        hits = self._select(index, body)
        total = len(hits)
        sources = [h["_source"] for h in hits]
        limit = int(size if size is not None else body.get("size", 10))
        resp: dict[str, Any] = {
            "took": 1,
            "timed_out": False,
            "_shards": {"total": 1, "successful": 1, "skipped": 0, "failed": 0},
            "hits": {"total": {"value": total, "relation": "eq"}, "hits": hits[:limit]},
        }
        if body.get("aggs") or body.get("aggregations"):
            resp["aggregations"] = _aggregate(sources, body.get("aggs") or body.get("aggregations"))
        if scroll:
            sid = f"scroll{next(self._ids)}"
            self._scrolls[sid] = hits[limit:]
            resp["_scroll_id"] = sid
        return resp

    def scroll(self, body: dict[str, Any] | None = None, **_: Any) -> dict[str, Any]:
        sid = (body or {}).get("scroll_id", "")
        remaining = self._scrolls.get(sid, [])
        page, self._scrolls[sid] = remaining[:500], remaining[500:]
        return {
            "_scroll_id": sid,
            "_shards": {"total": 1, "successful": 1, "skipped": 0},
            "hits": {"hits": page},
        }

    def clear_scroll(self, body: dict[str, Any] | None = None, **_: Any) -> dict[str, Any]:
        for sid in (body or {}).get("scroll_id", []):
            self._scrolls.pop(sid, None)
        return {}

    def count(
        self, index: Any = None, body: dict[str, Any] | None = None, **_: Any
    ) -> dict[str, Any]:
        return {"count": len(self._select(index, body))}
