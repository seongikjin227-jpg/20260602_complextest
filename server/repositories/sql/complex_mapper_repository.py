"""Repository for NEXT_SQL_COMPLEX_MAP SQL conversion supplemental rules."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv

from server.core.logger import logger
from server.services.sql.db_runtime import get_connection, qualify_table_name
from server.services.sql.domain_models import ComplexMappingRuleItem, SqlInfoJob


_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
load_dotenv(_PROJECT_ROOT / ".env")


def _to_text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if hasattr(value, "read"):
        value = value.read()
    if value is None:
        return default
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="ignore")
    return str(value)


def _complex_map_table() -> str:
    return qualify_table_name("NEXT_SQL_COMPLEX_MAP")


def _split_table_owner_and_name(table: str) -> tuple[str | None, str]:
    value = (table or "").strip().upper()
    if "." in value:
        owner, table_name = value.split(".", 1)
        return owner.strip('"'), table_name.strip('"')
    return None, value.strip('"')


def _assert_safe_table_name(table: str) -> None:
    for part in (table or "").split("."):
        if not re.fullmatch(r"[A-Z][A-Z0-9_$#]*", part.strip().upper()):
            raise ValueError(f"Invalid complex map table identifier: {table}")


def _ensure_complex_map_table_exists() -> None:
    table = _complex_map_table()
    _assert_safe_table_name(table)
    owner, table_name = _split_table_owner_and_name(table)
    if owner:
        query = """
            SELECT COUNT(*)
            FROM ALL_TABLES
            WHERE OWNER = :1
              AND TABLE_NAME = :2
        """
        params = [owner, table_name]
    else:
        query = """
            SELECT COUNT(*)
            FROM USER_TABLES
            WHERE TABLE_NAME = :1
        """
        params = [table_name]

    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(query, params)
        exists = int(cursor.fetchone()[0] or 0) > 0

    if not exists:
        raise RuntimeError(
            "NEXT_SQL_COMPLEX_MAP table is required for SQL conversion supplemental "
            "mapping rules. Run scripts/create_sql_complex_map_table.py first."
        )


def _normalize_table_name(value: str | None) -> str:
    text = _to_text(value).strip().strip('"').strip("'")
    if "." in text:
        text = text.split(".")[-1]
    return text.strip().strip('"').strip("'").upper()


def has_complex_mapping_rules(target_table: str | None) -> bool:
    """Return True when NEXT_SQL_COMPLEX_MAP has active rules for FR_TABLE."""
    _ensure_complex_map_table_exists()
    target = _normalize_table_name(target_table)
    if not target:
        return False

    table = _complex_map_table()
    query = f"""
        SELECT COUNT(*)
        FROM {table}
        WHERE USE_YN = 'Y'
          AND FR_TABLE = :1
    """
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(query, [target])
        return int(cursor.fetchone()[0] or 0) > 0


def get_complex_target_tables(target_tables: set[str]) -> set[str]:
    """Return target tables that have active NEXT_SQL_COMPLEX_MAP FR_TABLE rules."""
    _ensure_complex_map_table_exists()
    normalized_targets = {
        normalized
        for target in target_tables
        if (normalized := _normalize_table_name(target))
    }
    if not normalized_targets:
        return set()

    return {
        target
        for target in normalized_targets
        if has_complex_mapping_rules(target)
    }


def get_complex_mapping_rules_for_job(
    job: SqlInfoJob,
    target_tables: set[str] | None = None,
    top_k: int | None = None,
) -> list[ComplexMappingRuleItem]:
    targets = {
        normalized
        for target in (target_tables or {job.target_table or ""})
        if (normalized := _normalize_table_name(target))
    }
    if not targets:
        return []

    selected_rules: list[ComplexMappingRuleItem] = []
    exact_rules: list[ComplexMappingRuleItem] = []
    for target in sorted(targets):
        exact_rules.extend(_fetch_complex_rules(target_table=target))

    if _include_exact_match_rules():
        selected_rules.extend(exact_rules)

    other_top_k = top_k or _complex_other_top_k()
    other_candidates = _fetch_complex_rules_excluding(target_tables=targets)
    if other_top_k > 0:
        selected_rules.extend(
            _select_search_rules(
                query_sql=job.source_sql,
                candidates=other_candidates,
                top_k=other_top_k,
            )
        )

    logger.info(
        "[ComplexMapperRepository] supplemental rules loaded "
        f"(target_tables={','.join(sorted(targets))}, exact={len(exact_rules)}, "
        f"other_candidates={len(other_candidates)}, other_top_k={other_top_k}, "
        f"selected={len(selected_rules)})"
    )
    return _dedupe_rules(selected_rules)


def _fetch_complex_rules(target_table: str) -> list[ComplexMappingRuleItem]:
    _ensure_complex_map_table_exists()
    table = _complex_map_table()
    query = f"""
        SELECT MAP_ID, FR_TABLE, FR_COL, TO_TABLE, TO_COL, DESCRIPTION
        FROM {table}
        WHERE USE_YN = 'Y'
          AND FR_TABLE = :1
        ORDER BY MAP_ID
    """

    rules: list[ComplexMappingRuleItem] = []
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(query, [target_table])
        for row in cursor.fetchall():
            rules.append(
                ComplexMappingRuleItem(
                    map_id=int(row[0]),
                    fr_table=_to_text(row[1]),
                    fr_col=_to_text(row[2]),
                    to_table=_to_text(row[3]),
                    to_col=_to_text(row[4]),
                    description=_to_text(row[5]),
                )
            )
    return rules


def _fetch_complex_rules_excluding(target_tables: set[str]) -> list[ComplexMappingRuleItem]:
    _ensure_complex_map_table_exists()
    table = _complex_map_table()
    query = f"""
        SELECT MAP_ID, FR_TABLE, FR_COL, TO_TABLE, TO_COL, DESCRIPTION
        FROM {table}
        WHERE USE_YN = 'Y'
        ORDER BY MAP_ID
    """

    excluded = {_normalize_table_name(target) for target in target_tables if target}
    rules: list[ComplexMappingRuleItem] = []
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(query)
        for row in cursor.fetchall():
            fr_table = _to_text(row[1])
            if _normalize_table_name(fr_table) in excluded:
                continue
            rules.append(
                ComplexMappingRuleItem(
                    map_id=int(row[0]),
                    fr_table=fr_table,
                    fr_col=_to_text(row[2]),
                    to_table=_to_text(row[3]),
                    to_col=_to_text(row[4]),
                    description=_to_text(row[5]),
                )
            )
    return rules


def _include_exact_match_rules() -> bool:
    return os.getenv("COMPLEX_MAP_EXACT_MATCH_INCLUDE_ALL", "true").strip().lower() in {
        "1",
        "true",
        "yes",
        "y",
    }


def _complex_other_top_k() -> int:
    try:
        return max(0, int(os.getenv("COMPLEX_MAP_OTHER_TOP_K", "2")))
    except ValueError:
        return 2


def _dedupe_rules(rules: list[ComplexMappingRuleItem]) -> list[ComplexMappingRuleItem]:
    deduped: list[ComplexMappingRuleItem] = []
    seen: set[int] = set()
    for rule in rules:
        if rule.map_id in seen:
            continue
        deduped.append(rule)
        seen.add(rule.map_id)
    return deduped


def _select_search_rules(
    query_sql: str,
    candidates: list[ComplexMappingRuleItem],
    top_k: int,
) -> list[ComplexMappingRuleItem]:
    query = (query_sql or "").strip()
    candidates = [rule for rule in candidates if (rule.fr_col or "").strip()]
    if not query or not candidates:
        return []

    try:
        return _select_by_vector_search(query, candidates, top_k)
    except Exception as exc:
        logger.warning(
            "[ComplexMapperRepository] vector search fallback to token search "
            f"(reason={type(exc).__name__}: {exc})"
        )
        return _select_by_lexical_search(query, candidates, top_k)


def _select_by_vector_search(
    query_sql: str,
    candidates: list[ComplexMappingRuleItem],
    top_k: int,
) -> list[ComplexMappingRuleItem]:
    embed_base_url = os.getenv("RAG_EMBED_BASE_URL", "").strip()
    if not embed_base_url:
        raise RuntimeError("RAG_EMBED_BASE_URL is not set")

    try:
        import faiss
        import numpy as np
    except Exception as exc:
        raise RuntimeError("faiss-cpu and numpy are required for vector search") from exc

    candidate_texts = [_normalize_sql_shape(rule.fr_col) for rule in candidates]
    query_text = _normalize_sql_shape(query_sql)
    embeddings = _embed_texts(candidate_texts + [query_text])
    if len(embeddings) != len(candidate_texts) + 1:
        raise RuntimeError("embedding response count does not match request count")

    candidate_vectors = np.asarray(embeddings[: len(candidate_texts)], dtype="float32")
    query_vector = np.asarray(embeddings[len(candidate_texts) :], dtype="float32")
    if candidate_vectors.ndim != 2 or query_vector.ndim != 2:
        raise RuntimeError("embedding vectors must be 2-dimensional")

    faiss.normalize_L2(candidate_vectors)
    faiss.normalize_L2(query_vector)
    index = faiss.IndexFlatIP(candidate_vectors.shape[1])
    index.add(candidate_vectors)

    safe_k = min(max(1, top_k), len(candidates))
    scores, indices = index.search(query_vector, safe_k)
    selected: list[ComplexMappingRuleItem] = []
    for score, candidate_idx in zip(scores[0], indices[0]):
        if candidate_idx < 0:
            continue
        rule = candidates[int(candidate_idx)]
        selected.append(
            ComplexMappingRuleItem(
                map_id=rule.map_id,
                fr_table=rule.fr_table,
                fr_col=rule.fr_col,
                to_table=rule.to_table,
                to_col=rule.to_col,
                description=rule.description,
                search_score=round(float(score), 6),
                search_method="faiss_vector",
            )
        )
    return selected


def _select_by_lexical_search(
    query_sql: str,
    candidates: list[ComplexMappingRuleItem],
    top_k: int,
) -> list[ComplexMappingRuleItem]:
    normalized_query = _normalize_sql_shape(query_sql)
    scored = [
        (rule, _lexical_similarity(normalized_query, _normalize_sql_shape(rule.fr_col)))
        for rule in candidates
    ]
    scored.sort(key=lambda item: item[1], reverse=True)
    selected: list[ComplexMappingRuleItem] = []
    for rule, score in scored[: min(max(1, top_k), len(scored))]:
        selected.append(
            ComplexMappingRuleItem(
                map_id=rule.map_id,
                fr_table=rule.fr_table,
                fr_col=rule.fr_col,
                to_table=rule.to_table,
                to_col=rule.to_col,
                description=rule.description,
                search_score=round(float(score), 6),
                search_method="token_fallback",
            )
        )
    return selected


def _embed_texts(texts: list[str]) -> list[list[float]]:
    endpoint = _embedding_endpoint(os.getenv("RAG_EMBED_BASE_URL", ""))
    headers = {"Content-Type": "application/json"}
    api_key = os.getenv("RAG_EMBED_API_KEY", "").strip()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    payload = {"model": os.getenv("RAG_EMBED_MODEL", "bge-m3").strip(), "input": texts}
    timeout_sec = int(os.getenv("RAG_EMBED_TIMEOUT_SEC", "30"))

    response = requests.post(endpoint, headers=headers, json=payload, timeout=timeout_sec)
    if response.status_code >= 400:
        raise RuntimeError(f"embedding HTTP {response.status_code}: {response.text[:300]}")
    vectors = _extract_embedding_vectors(response.json())
    if not vectors:
        raise RuntimeError("embedding response did not contain vectors")
    return vectors


def _extract_embedding_vectors(body: Any) -> list[list[float]]:
    if isinstance(body, dict):
        data = body.get("data")
        if isinstance(data, list):
            vectors = []
            for item in data:
                if isinstance(item, dict) and isinstance(item.get("embedding", None), list):
                    vectors.append([float(value) for value in item["embedding"]])
            if vectors:
                return vectors

        embeddings = body.get("embeddings")
        if isinstance(embeddings, list):
            vectors = []
            for item in embeddings:
                if isinstance(item, list):
                    vectors.append([float(value) for value in item])
            if vectors:
                return vectors

        embedding = body.get("embedding")
        if isinstance(embedding, list):
            return [[float(value) for value in embedding]]
    return []


def _embedding_endpoint(base_url: str) -> str:
    normalized = base_url.strip().rstrip("/")
    if normalized.endswith("/embeddings"):
        return normalized
    if normalized.endswith("/v1"):
        return f"{normalized}/embeddings"
    return f"{normalized}/v1/embeddings"


def _normalize_sql_shape(sql_text: str) -> str:
    text = re.sub(r"/\*.*?\*/", " ", sql_text or "", flags=re.DOTALL)
    text = re.sub(r"--[^\n]*", " ", text)
    text = re.sub(r"'(?:''|[^'])*'", " STR ", text)
    text = re.sub(r"\b\d+(?:\.\d+)?\b", " NUM ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text.upper()


def _lexical_similarity(left: str, right: str) -> float:
    left_tokens = set(re.findall(r"[A-Z_][A-Z0-9_]*", left or ""))
    right_tokens = set(re.findall(r"[A-Z_][A-Z0-9_]*", right or ""))
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / len(left_tokens | right_tokens)
