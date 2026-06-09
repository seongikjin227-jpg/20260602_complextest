"""poll_jobs 도구: DB에서 대기 중인 작업 목록을 조회하고 레지스트리를 갱신합니다."""

from __future__ import annotations

import json
from typing import Callable

from langchain_core.tools import tool

from server.tools.context import (
    callbacks,
    formatting_registry,
    mig_registry,
    sql_registry,
    tuning_registry,
)

JOB_BATCH_SIZE = 20


def build_poll_jobs_tool(
    get_migration_jobs: Callable,
    get_sql_jobs: Callable,
    get_tuning_jobs: Callable,
    get_formatting_jobs: Callable,
) -> Callable:
    """poll_jobs 도구를 클로저로 생성합니다. Supervisor 초기화 시 한 번만 호출합니다."""

    @tool
    def poll_jobs() -> str:
        """DB에서 대기 중인 작업 목록을 조회하고 현재 사이클의 처리 대상을 등록합니다.
        사이클 시작 시 반드시 먼저 호출해야 합니다.
        반환값: migration_jobs, sql_jobs, tuning_jobs, formatting_jobs 목록과 summary를 담은 JSON 문자열."""
        logger = callbacks.get("logger")

        mig_jobs, sql_jobs, tuning_jobs, formatting_jobs = [], [], [], []
        try:
            mig_jobs = get_migration_jobs()
        except Exception as exc:
            if logger:
                logger.error(f"[poll_jobs] DataMigration 조회 오류: {exc}")
        try:
            sql_jobs = get_sql_jobs()
            tuning_jobs = get_tuning_jobs()
            formatting_jobs = get_formatting_jobs()
        except Exception as exc:
            if logger:
                logger.error(f"[poll_jobs] SQL/Tuning/Formatting 조회 오류: {exc}")

        mig_registry.clear()
        sql_registry.clear()
        tuning_registry.clear()
        formatting_registry.clear()

        for job in mig_jobs[:JOB_BATCH_SIZE]:
            mig_registry[job.map_id] = job
        for job in sql_jobs[:JOB_BATCH_SIZE]:
            sql_registry[str(job.row_id)] = job
        for job in tuning_jobs[:JOB_BATCH_SIZE]:
            tuning_registry[str(job.row_id)] = job
        for job in formatting_jobs[:JOB_BATCH_SIZE]:
            formatting_registry[str(job.row_id)] = job

        result = {
            "migration_jobs": [
                {
                    "map_id": job.map_id,
                    "map_type": job.map_type,
                    "fr_table": job.fr_table,
                    "to_table": job.to_table,
                    "priority": job.priority,
                    "retry_count": getattr(job, "retry_count", 0) or 0,
                    "status": job.status,
                    "batch_cnt": getattr(job, "batch_cnt", 0) or 0,
                }
                for job in mig_registry.values()
            ],
            "sql_jobs": [
                {
                    "row_id": job.row_id,
                    "status": job.status,
                    "tag_kind": job.tag_kind,
                    "space_nm": job.space_nm,
                    "sql_id": job.sql_id,
                }
                for job in sql_registry.values()
            ],
            "tuning_jobs": [
                {
                    "row_id": job.row_id,
                    "tuned_test": job.tuned_test,
                }
                for job in tuning_registry.values()
            ],
            "formatting_jobs": [
                {
                    "row_id": job.row_id,
                    "space_nm": job.space_nm,
                    "sql_id": job.sql_id,
                }
                for job in formatting_registry.values()
            ],
            "summary": {
                "migration_total": len(mig_jobs),
                "migration_in_batch": len(mig_registry),
                "sql_total": len(sql_jobs),
                "sql_in_batch": len(sql_registry),
                "tuning_total": len(tuning_jobs),
                "tuning_in_batch": len(tuning_registry),
                "formatting_total": len(formatting_jobs),
                "formatting_in_batch": len(formatting_registry),
                "sql_by_status": _count_by_status(
                    [j.status for j in sql_registry.values()]
                ),
            },
        }

        if logger:
            s = result["summary"]
            if s["migration_total"] or s["sql_total"] or s["tuning_total"] or s["formatting_total"]:
                logger.info(
                    f"[poll_jobs] "
                    f"Mig={s['migration_in_batch']}/{s['migration_total']}, "
                    f"Sql={s['sql_in_batch']}/{s['sql_total']}, "
                    f"Tuning={s['tuning_in_batch']}/{s['tuning_total']}, "
                    f"Formatting={s['formatting_in_batch']}/{s['formatting_total']}"
                )
            else:
                logger.info("[poll_jobs] 대기 중인 작업 없음")

        return json.dumps(result, ensure_ascii=False, default=str)

    return poll_jobs


def _count_by_status(statuses: list) -> dict:
    counts: dict = {}
    for s in statuses:
        key = (s or "NULL").strip().upper()
        counts[key] = counts.get(key, 0) + 1
    return counts
