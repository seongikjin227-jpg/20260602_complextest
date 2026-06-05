"""Repository for NEXT_SQL_COMPLEX_MAP based complex SQL conversion rules."""

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
            "NEXT_SQL_COMPLEX_MAP table is required for SQL conversion map-type "
            "classification. Run scripts/create_sql_complex_map_table.py first."
        )


def _normalize_table_name(value: str | None) -> str:
    text = _to_text(value).strip().strip('"').strip("'")
    if "." in text:
        text = text.split(".")[-1]
    return text.strip().strip('"').strip("'").upper()


def has_complex_mapping_rules(target_table: str | None) -> bool:
    """Return True when NEXT_SQL_COMPLEX_MAP has active rules for target table."""
    _ensure_complex_map_table_exists()
    target = _normalize_table_name(target_table)
    if not target:
        return False

    table = _complex_map_table()
    query = f"""
        SELECT COUNT(*)
        FROM {table}
        WHERE UPPER(TRIM(USE_YN)) = 'Y'
          AND UPPER(TRIM(FR_TABLE)) = :1
    """
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(query, [target])
        return int(cursor.fetchone()[0] or 0) > 0


def get_complex_target_tables(target_tables: set[str]) -> set[str]:
    """Return target tables that have active NEXT_SQL_COMPLEX_MAP rules."""
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
) -> tuple[list[ComplexMappingRuleItem], list[ComplexMappingRuleItem]]:
    targets = {
        normalized
        for target in (target_tables or {job.target_table or ""})
        if (normalized := _normalize_table_name(target))
    }
    if not targets:
        return [], []

    general_rules: list[ComplexMappingRuleItem] = []
    search_candidates: list[ComplexMappingRuleItem] = []
    for target in sorted(targets):
        general_rules.extend(_fetch_complex_rules(target_table=target, map_kind="GENERAL"))
        search_candidates.extend(_fetch_complex_rules(target_table=target, map_kind="SEARCH"))

    search_rules = _select_search_rules(
        query_sql=job.source_sql,
        candidates=search_candidates,
        top_k=top_k or _complex_search_top_k(),
    )
    logger.info(
        "[ComplexMapperRepository] complex rules loaded "
        f"(target_tables={','.join(sorted(targets))}, general={len(general_rules)}, "
        f"search_candidates={len(search_candidates)}, search_selected={len(search_rules)})"
    )
    return general_rules, search_rules


def _fetch_complex_rules(target_table: str, map_kind: str) -> list[ComplexMappingRuleItem]:
    _ensure_complex_map_table_exists()
    table = _complex_map_table()
    query = f"""
        SELECT MAP_ID, MAP_KIND, FR_TABLE, FR_COL, TO_TABLE, TO_COL, DESCRIPTION
        FROM {table}
        WHERE UPPER(TRIM(USE_YN)) = 'Y'
          AND UPPER(TRIM(MAP_KIND)) = :1
          AND UPPER(TRIM(FR_TABLE)) = :2
        ORDER BY MAP_ID
    """

    rules: list[ComplexMappingRuleItem] = []
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(query, [map_kind.strip().upper(), target_table])
        for row in cursor.fetchall():
            rules.append(
                ComplexMappingRuleItem(
                    map_id=int(row[0]),
                    map_kind=_to_text(row[1]).strip().upper(),
                    fr_table=_to_text(row[2]),
                    fr_col=_to_text(row[3]),
                    to_table=_to_text(row[4]),
                    to_col=_to_text(row[5]),
                    description=_to_text(row[6]),
                )
            )
    return rules


def _complex_search_top_k() -> int:
    try:
        return max(1, int(os.getenv("COMPLEX_MAP_SEARCH_TOP_K", "3")))
    except ValueError:
        return 3


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
                map_kind=rule.map_kind,
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
                map_kind=rule.map_kind,
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
