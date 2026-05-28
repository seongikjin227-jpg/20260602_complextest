import json
import os
import re
from datetime import date, datetime
from pathlib import Path
from typing import Iterable

from dotenv import load_dotenv
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from server.core.exceptions import LLMRateLimitError
from server.services.sql.domain_models import MappingRuleItem, SqlInfoJob
from server.services.sql.prompt_service import build_prompt_messages
from server.services.sql.correct_sql_rag_service import correct_sql_hint_rag_service
from server.services.sql.tobe_sql_tuning_service import tobe_sql_tuning_service


# unified_agent/ 프로젝트 루트
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
load_dotenv(_PROJECT_ROOT / ".env")
_BIND_TOKEN_PATTERN = re.compile(r"[#$]\{\s*([^}]+?)\s*\}")
_SQL_START_KEYWORDS = r"SELECT|INSERT|UPDATE|DELETE|MERGE|CREATE|ALTER|WITH"
_MYBATIS_WRAPPER_TAGS = r"script|select|insert|update|delete"
_MYBATIS_DYNAMIC_TAGS = r"if|choose|when|otherwise|where|trim|foreach"


def _env_or_value(value: str | None, env_name: str) -> str:
    resolved = value or os.getenv(env_name)
    if not resolved:
        raise ValueError(f"Required environment variable '{env_name}' is not set.")
    return resolved


def _normalize_anthropic_base_url(raw_base_url: str) -> str:
    normalized = raw_base_url.strip().rstrip("/")
    if normalized.endswith("/v1/message"):
        return normalized[: -len("/v1/message")]
    if normalized.endswith("/v1/messages"):
        return normalized[: -len("/v1/messages")]
    if normalized.endswith("/v1"):
        return normalized[: -len("/v1")]
    return normalized


def _normalize_openai_base_url(raw_base_url: str) -> str:
    normalized = raw_base_url.strip().rstrip("/")
    for suffix in ("/chat/completions", "/responses", "/completions", "/models"):
        if normalized.endswith(suffix):
            return normalized[: -len(suffix)]
    return normalized


def _resolve_llm_provider(provider: str | None, base_url: str, model: str) -> str:
    resolved = (provider or os.getenv("LLM_PROVIDER") or "").strip().lower()
    if resolved:
        if resolved not in {"anthropic", "openai"}:
            raise ValueError("LLM_PROVIDER must be either 'anthropic' or 'openai'.")
        return resolved

    lowered_base = base_url.lower()
    lowered_model = model.lower()
    if "anthropic" in lowered_base or lowered_model.startswith("claude"):
        return "anthropic"
    return "openai"


def _serialize_mapping_rules(mapping_rules: list[MappingRuleItem]) -> str:
    if not mapping_rules:
        return "[MAPPING_RULES]\n- (empty)"

    rows: set[tuple[str, str, str, str]] = set()
    source_schema = _schema_env("ORACLE_SCHEMA_SRC")
    target_schema = _schema_env("ORACLE_SCHEMA_TGT")
    for rule in mapping_rules:
        fr_table = _qualify_mapping_table(rule.fr_table, source_schema)
        to_table = _qualify_mapping_table(rule.to_table, target_schema)
        fr_col = (rule.fr_col or "").strip()
        to_col = (rule.to_col or "").strip()
        if fr_table and to_table and fr_col and to_col:
            rows.add((fr_table, fr_col, to_table, to_col))

    lines = ["[MAPPING_RULES]"]
    if not rows:
        lines.append("- (empty)")
        return "\n".join(lines)

    for fr_table, fr_col, to_table, to_col in sorted(rows):
        lines.append(
            f"- FR_TABLE={fr_table} | FR_COL={fr_col} | "
            f"TO_TABLE={to_table} | TO_COL={to_col}"
        )
    return "\n".join(lines)


def _qualify_mapping_table(table_name: str, schema: str) -> str:
    table = (table_name or "").strip()
    if not table or "." in table or not schema:
        return table
    return f"{schema}.{table}"


def _normalize_table_token(token: str) -> str:
    value = (token or "").strip().strip("[]").strip().strip('"').strip("'").strip()
    if not value:
        return ""
    if "." in value:
        value = value.split(".")[-1]
    return value.strip("[]").strip().strip('"').strip("'").upper()


def _load_target_tables(job: SqlInfoJob) -> set[str]:
    raw = (job.target_table or "").strip()
    if not raw:
        return set()

    tokens: list[str] = []
    if raw.startswith("[") or raw.startswith("{"):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                tokens = [str(item) for item in parsed]
            elif isinstance(parsed, str):
                tokens = [parsed]
        except Exception:
            tokens = []

    if not tokens:
        tokens = re.split(r"[,\s;|]+", raw)

    return {normalized for token in tokens if (normalized := _normalize_table_token(token))}


def _extract_referenced_fr_tables_from_source_sql(source_sql: str, candidate_fr_tables: set[str]) -> set[str]:
    if not source_sql or not candidate_fr_tables:
        return set()

    text = re.sub(r"/\*.*?\*/", " ", source_sql, flags=re.DOTALL)
    text = re.sub(r"--[^\n]*", " ", text)
    text = re.sub(r"'(?:''|[^'])*'", " ", text)
    scan = text.upper()

    matched: set[str] = set()
    for table in candidate_fr_tables:
        pattern = rf"(?<![A-Z0-9_$#]){re.escape(table)}(?![A-Z0-9_$#])"
        if re.search(pattern, scan):
            matched.add(table)
    return matched


def _select_mapping_rules_for_job(job: SqlInfoJob, mapping_rules: list[MappingRuleItem]) -> list[MappingRuleItem]:
    if not mapping_rules:
        return []

    target_tables = _load_target_tables(job)
    if target_tables:
        return [
            rule
            for rule in mapping_rules
            # SqlInfoJob.target_table stores source tables parsed from FROM SQL.
            if _fr_table_contains_any_target(rule.fr_table, target_tables)
        ]

    rules_by_fr: dict[str, list[MappingRuleItem]] = {}
    for rule in mapping_rules:
        fr_norm = _normalize_table_token(rule.fr_table)
        if fr_norm:
            rules_by_fr.setdefault(fr_norm, []).append(rule)

    selected_fr_tables = _extract_referenced_fr_tables_from_source_sql(job.source_sql, set(rules_by_fr.keys()))

    if not selected_fr_tables:
        return mapping_rules

    filtered: list[MappingRuleItem] = []
    for fr_table in sorted(selected_fr_tables):
        filtered.extend(rules_by_fr.get(fr_table, []))
    return filtered


def _fr_table_contains_any_target(fr_table: str, target_tables: set[str]) -> bool:
    fr_text = (fr_table or "").upper()
    if not fr_text or not target_tables:
        return False

    for table in target_tables:
        pattern = rf"(?<![A-Z0-9_$#]){re.escape(table)}(?![A-Z0-9_$#])"
        if re.search(pattern, fr_text):
            return True
    return False


def serialize_tuning_examples_for_prompt(tuning_examples: list[dict[str, str]]) -> str:
    if not tuning_examples:
        return "[]"

    compact_examples: list[dict[str, object]] = []
    for block in tuning_examples:
        if not isinstance(block, dict):
            continue

        matched_rules: list[dict[str, object]] = []
        for rule_match in block.get("top_rule_matches", []):
            if isinstance(rule_match, dict):
                matched_rules.append(
                    {
                        "rule_id": rule_match.get("rule_id", ""),
                        "score": rule_match.get("score", 0),
                        "guidance": rule_match.get("guidance", []),
                        "example_bad_sql": rule_match.get("example_bad_sql", ""),
                        "example_tuned_sql": rule_match.get("example_tuned_sql", ""),
                    }
                )

        compact_examples.append(
            {
                "block_id": block.get("block_id", ""),
                "block_type": block.get("block_type", ""),
                "source_sql": block.get("source_sql", block.get("from_sql", "")),
                "search_method": block.get("search_method", ""),
                "embedding_model": block.get("embedding_model", ""),
                "top_rule_matches": matched_rules,
            }
        )

    return json.dumps(compact_examples, ensure_ascii=False, indent=2)


def serialize_correct_sql_hints_for_prompt(hints: list[dict[str, object]]) -> str:
    correct_sqls = [
        str(hint.get("correct_sql", "")).strip()
        for hint in hints
        if isinstance(hint, dict) and str(hint.get("correct_sql", "")).strip()
    ]
    return json.dumps(correct_sqls, ensure_ascii=False, indent=2)


def _build_sql_messages(template_name: str, **payload: str) -> list[dict[str, str]]:
    return build_prompt_messages(template_name, **payload)


def _extract_sql_text(response_text: str) -> str:
    text = response_text.strip()
    code_block_match = re.search(r"```(?:sql)?\s*(.*?)```", text, re.IGNORECASE | re.DOTALL)
    if code_block_match:
        text = code_block_match.group(1).strip()
    if not text:
        raise ValueError("LLM returned an empty response.")

    text = _strip_leading_non_sql_text(text)
    text = _strip_mybatis_wrapper_tags(text)

    if not _starts_with_sql_or_dynamic_tag(text):
        raise ValueError("LLM response does not start with executable SQL.")
    return _normalize_oracle_sql(text)


def _strip_leading_non_sql_text(text: str) -> str:
    start_patterns = [
        rf"\b(?:{_SQL_START_KEYWORDS})\b",
        rf"<\s*(?:{_MYBATIS_WRAPPER_TAGS})\b",
        rf"<\s*(?:{_MYBATIS_DYNAMIC_TAGS})\b",
    ]
    matches = [match for pattern in start_patterns if (match := re.search(pattern, text, re.IGNORECASE))]
    if not matches:
        return text.strip()
    first = min(matches, key=lambda match: match.start())
    return text[first.start() :].strip()


def _strip_mybatis_wrapper_tags(text: str) -> str:
    stripped = text.strip()
    while True:
        open_match = re.match(
            rf"^<\s*({_MYBATIS_WRAPPER_TAGS})\b[^>]*>",
            stripped,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if not open_match:
            return stripped
        tag_name = open_match.group(1)
        stripped = stripped[open_match.end() :].strip()
        stripped = re.sub(
            rf"</\s*{re.escape(tag_name)}\s*>\s*$",
            "",
            stripped,
            flags=re.IGNORECASE,
        ).strip()


def _starts_with_sql_or_dynamic_tag(text: str) -> bool:
    return bool(
        re.match(rf"^(?:{_SQL_START_KEYWORDS})\b", text, re.IGNORECASE)
        or re.match(rf"^<\s*(?:{_MYBATIS_DYNAMIC_TAGS})\b", text, re.IGNORECASE)
    )


def _normalize_bind_name(token: str) -> str:
    cleaned = (token or "").strip()
    if not cleaned:
        return ""
    return cleaned.split(".")[-1].strip()


def _sql_literal(value) -> str:
    if value is not None and hasattr(value, "read"):
        value = value.read()
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, datetime):
        return f"TO_DATE('{value.strftime('%Y-%m-%d')}', 'YYYY-MM-DD')"
    if isinstance(value, date):
        return f"TO_DATE('{value.isoformat()}', 'YYYY-MM-DD')"

    text = str(value)
    iso_match = re.match(r"^(\d{4}-\d{2}-\d{2})(?:[T\s].*)?$", text)
    if iso_match:
        return f"TO_DATE('{iso_match.group(1)}', 'YYYY-MM-DD')"
    return "'" + text.replace("'", "''") + "'"


def _strip_sqlplus_terminator_lines(lines: Iterable[str]) -> list[str]:
    return [line for line in lines if line.strip() != "/"]


def _replace_limit_with_fetch_first(text: str) -> str:
    return re.sub(r"\s+LIMIT\s+(\d+)\s*$", r" FETCH FIRST \1 ROWS ONLY", text, flags=re.IGNORECASE)


def _normalize_oracle_sql(sql_text: str) -> str:
    text = sql_text.replace("﻿", "").replace("​", "").replace(" ", " ")
    text = "\n".join(_strip_sqlplus_terminator_lines(text.splitlines())).strip()
    text = _replace_limit_with_fetch_first(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\s+\n", "\n", text)
    text = text.strip().rstrip(";").strip()

    if not text:
        raise ValueError("LLM returned an empty SQL statement after normalization.")
    return text


def _to_langchain_messages(messages: list[dict[str, str]]):
    converted = []
    for message in messages:
        if message.get("role") == "system":
            converted.append(SystemMessage(content=message.get("content", "")))
        else:
            converted.append(HumanMessage(content=message.get("content", "")))
    return converted


def _ensure_anthropic_message_requirements(messages: list[dict[str, str]]) -> list[dict[str, str]]:
    safe = list(messages or [])
    has_user_or_assistant = any((message.get("role") or "").lower() in {"user", "assistant"} for message in safe)
    if not has_user_or_assistant:
        safe.append(
            {
                "role": "user",
                "content": "Generate one executable Oracle SQL statement only. Do not end the SQL with a semicolon.",
            }
        )
    return safe


def call_llm_api(
    api_key: str | None,
    model: str | None,
    base_url: str | None,
    messages: list[dict[str, str]],
    provider: str | None = None,
) -> str:
    resolved_api_key = _env_or_value(api_key, "LLM_API_KEY")
    resolved_model = _env_or_value(model, "LLM_MODEL")
    raw_base_url = _env_or_value(base_url, "LLM_BASE_URL")
    resolved_provider = _resolve_llm_provider(provider=provider, base_url=raw_base_url, model=resolved_model)

    try:
        if resolved_provider == "anthropic":
            llm = ChatAnthropic(
                api_key=resolved_api_key,
                model_name=resolved_model,
                anthropic_api_url=_normalize_anthropic_base_url(raw_base_url),
                max_tokens_to_sample=int(os.getenv("LLM_MAX_TOKENS", "4096")),
                temperature=0,
            )
            safe_messages = _ensure_anthropic_message_requirements(messages)
        else:
            llm = ChatOpenAI(
                api_key=resolved_api_key,
                model=resolved_model,
                base_url=_normalize_openai_base_url(raw_base_url),
                temperature=0,
            )
            safe_messages = list(messages or [])

        response = llm.invoke(_to_langchain_messages(safe_messages))
        content = getattr(response, "content", response)
        if isinstance(content, list):
            text = "".join(item.get("text", "") if isinstance(item, dict) else str(item) for item in content)
        else:
            text = str(content)
        return _extract_sql_text(text)
    except Exception as exc:
        message = str(exc)
        lowered = message.lower()
        if "429" in message or "rate limit" in lowered or "504" in message or "gateway timeout" in lowered or "timed out" in lowered:
            raise LLMRateLimitError(message) from exc
        raise


def generate_tobe_sql(
    job: SqlInfoJob,
    mapping_rules: list[MappingRuleItem],
    last_error: str | None = None,
) -> str:
    scoped_rules = _select_mapping_rules_for_job(job=job, mapping_rules=mapping_rules)
    correct_sql_hints = correct_sql_hint_rag_service.retrieve_correct_sql_hints(
        sql_text=job.source_sql,
        correct_kind="TOBE",
        current_row_id=job.row_id,
    )
    return call_llm_api(
        api_key=None,
        model=None,
        base_url=None,
        messages=_build_sql_messages(
            "tobe_sql_prompt.json",
            from_sql=job.source_sql,
            mapping_schema_text=_serialize_mapping_rules(scoped_rules),
            target_schema=_schema_env("ORACLE_SCHEMA_TGT") or "UNKNOWN",
            correct_sql_hint_json=serialize_correct_sql_hints_for_prompt(correct_sql_hints),
            last_error=last_error or "None",
        ),
    )



def generate_bind_tuned_sql(
    job: SqlInfoJob,
    last_error: str | None = None,
) -> str:
    return call_llm_api(
        api_key=None,
        model=None,
        base_url=None,
        messages=_build_sql_messages(
            "bind_tuned_sql_prompt.json",
            from_sql=job.source_sql,
            from_schema=_schema_env("ORACLE_SCHEMA_SRC") or "UNKNOWN",
            last_error=last_error or "None",
        ),
    )

def generate_bind_sql(
    job: SqlInfoJob,
    last_error: str | None = None,
    bind_source_sql: str | None = None,
) -> str:
    template_name = "bind_sql_final_retry_prompt.json" if _is_final_retry_mode(last_error) else "bind_sql_prompt.json"
    source_sql = bind_source_sql or job.source_sql
    correct_sql_hints = correct_sql_hint_rag_service.retrieve_correct_sql_hints(
        sql_text=job.source_sql,
        correct_kind="BIND",
        current_row_id=job.row_id,
    )
    return call_llm_api(
        api_key=None,
        model=None,
        base_url=None,
        messages=_build_sql_messages(
            template_name,
            from_sql=source_sql,
            from_schema=_schema_env("ORACLE_SCHEMA_SRC") or "UNKNOWN",
            correct_sql_hint_json=serialize_correct_sql_hints_for_prompt(correct_sql_hints),
            last_error=last_error or "None",
        ),
    )


def tune_tobe_sql(
    current_tobe_sql: str,
    tuning_examples: list[dict[str, str]] | None = None,
    last_error: str | None = None,
) -> str:
    return call_llm_api(
        api_key=None,
        model=None,
        base_url=None,
        messages=_build_sql_messages(
            "tobe_sql_tuning_prompt.json",
            current_tobe_sql=current_tobe_sql,
            universal_tuning_rules=json.dumps(
                tobe_sql_tuning_service.load_universal_tuning_rules(),
                ensure_ascii=False,
                indent=2,
            ),
            tuning_examples_json=serialize_tuning_examples_for_prompt(tuning_examples or []),
            last_error=last_error or "None",
        ),
    )


def _load_bind_sets_json(bind_set_json: str | None) -> str:
    try:
        bind_sets = json.loads(bind_set_json or "[]")
    except Exception:
        bind_sets = []
    if not isinstance(bind_sets, list):
        bind_sets = []
    if not bind_sets:
        bind_sets = [{}]
    return json.dumps(bind_sets, ensure_ascii=False, default=str)


def _schema_env(name: str) -> str:
    return (os.getenv(name) or "").strip().upper()


def _is_final_retry_mode(last_error: str | None) -> bool:
    text = (last_error or "").upper()
    return "FINAL_RETRY_MODE=ON" in text or "ATTEMPT=3/3" in text


def _generate_validation_test_sql(
    source_sql: str,
    target_sql: str,
    bind_set_json: str | None,
    source_schema: str,
    target_schema: str,
    comparison_mode: str,
    last_error: str | None = None,
    final_retry_mode: bool = False,
    correct_sql_hint_json: str = "[]",
) -> str:
    template_name = "test_sql_final_retry_prompt.json" if final_retry_mode else "test_sql_prompt.json"
    return call_llm_api(
        api_key=None,
        model=None,
        base_url=None,
        messages=_build_sql_messages(
            template_name,
            source_sql=source_sql,
            target_sql=target_sql,
            source_schema=source_schema or "UNKNOWN",
            target_schema=target_schema or "UNKNOWN",
            bind_set_json=_load_bind_sets_json(bind_set_json),
            comparison_mode=comparison_mode,
            correct_sql_hint_json=correct_sql_hint_json,
            last_error=last_error or "None",
        ),
    )


def generate_test_sql(
    job: SqlInfoJob,
    tobe_sql: str,
    bind_set_json: str,
    last_error: str | None = None,
) -> str:
    correct_sql_hints = correct_sql_hint_rag_service.retrieve_correct_sql_hints(
        sql_text=job.source_sql,
        correct_kind="TEST",
        current_row_id=job.row_id,
    )
    return _generate_validation_test_sql(
        source_sql=job.source_sql,
        target_sql=tobe_sql,
        bind_set_json=bind_set_json,
        source_schema=_schema_env("ORACLE_SCHEMA_SRC"),
        target_schema=_schema_env("ORACLE_SCHEMA_TGT"),
        comparison_mode="SOURCE_TO_TARGET",
        last_error=last_error,
        final_retry_mode=_is_final_retry_mode(last_error),
        correct_sql_hint_json=serialize_correct_sql_hints_for_prompt(correct_sql_hints),
    )


def generate_sql_comparison_test_sql(
    baseline_sql: str,
    candidate_sql: str,
    bind_set_json: str | None = None,
    last_error: str | None = None,
) -> str:
    target_schema = _schema_env("ORACLE_SCHEMA_TGT")
    return _generate_validation_test_sql(
        source_sql=baseline_sql,
        target_sql=candidate_sql,
        bind_set_json=bind_set_json,
        source_schema=target_schema,
        target_schema=target_schema,
        comparison_mode="TARGET_TO_TARGET",
        last_error=last_error,
        correct_sql_hint_json="[]",
    )
