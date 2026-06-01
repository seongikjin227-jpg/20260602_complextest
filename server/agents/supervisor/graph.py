"""Supervisor LangGraph with deterministic job execution.

The supervisor polls pending work and runs a fixed-size batch directly.
LLM calls remain inside the DB migration, SQL conversion, and SQL tuning
agents. The supervisor itself no longer asks an LLM which job to run.
"""

import os
import threading
import time
from pathlib import Path

from langgraph.graph import END, StateGraph

from server.agents.supervisor.state import SupervisorState
import server.tools as supervisor_tools

# SQL_TUNING_ONLY takes precedence if both flags are enabled.
_SQL_CONVERSION_ONLY = os.getenv("SQL_CONVERSION_ONLY", "false").lower() == "true"
_SQL_TUNING_ONLY = os.getenv("SQL_TUNING_ONLY", "false").lower() == "true"
_RUN_MIGRATION = not _SQL_CONVERSION_ONLY and not _SQL_TUNING_ONLY
_RUN_SQL_CONVERSION = not _SQL_TUNING_ONLY
_RUN_SQL_TUNING = _SQL_TUNING_ONLY or not _SQL_CONVERSION_ONLY

_RUNTIME_DIR = Path(__file__).resolve().parent.parent.parent.parent / "runtime"
PAUSE_FLAG = _RUNTIME_DIR / "agent.pause"
POLL_INTERVAL_SEC = 5
JOB_BATCH_SIZE = 20

_stop_event = threading.Event()


def request_stop() -> None:
    _stop_event.set()


def is_stop_requested() -> bool:
    return _stop_event.is_set()


def build_supervisor_graph(
    get_migration_jobs,
    get_sql_jobs,
    get_tuning_jobs,
    mig_increment_batch,
    mig_process_job,
    sql_increment_batch,
    sql_process_job,
    tune_process_job,
    logger,
):
    mig_registry: dict = {}
    sql_registry: dict = {}
    tuning_registry: dict = {}

    supervisor_tools.init_callbacks(
        mig_inc=mig_increment_batch,
        mig_proc=mig_process_job,
        sql_inc=sql_increment_batch,
        sql_proc=sql_process_job,
        tune_proc=tune_process_job,
        logger=logger,
    )
    mig_registry, sql_registry, tuning_registry = supervisor_tools.get_registries()

    def poll_node(state: SupervisorState) -> dict:
        """Poll pending jobs and refresh current batch registries."""
        if _stop_event.is_set():
            return {"stop_requested": True, "cycle": state.get("cycle", 0) + 1}

        cycle = state.get("cycle", 0) + 1
        logger.info(f"\n{'=' * 50}")
        logger.info(f"[Supervisor] Batch loop {cycle} 시작")
        supervisor_tools.start_cycle_metrics(cycle)

        mig_jobs, sql_jobs, tuning_jobs = [], [], []
        if _RUN_MIGRATION:
            try:
                mig_jobs = get_migration_jobs()
            except Exception as exc:
                logger.error(f"[Supervisor] DataMigration polling error: {exc}")
        try:
            if _RUN_SQL_CONVERSION:
                sql_jobs = get_sql_jobs()
            if _RUN_SQL_TUNING:
                tuning_jobs = get_tuning_jobs()
        except Exception as exc:
            logger.error(f"[Supervisor] SQL/Tuning polling error: {exc}")

        mig_registry.clear()
        sql_registry.clear()
        tuning_registry.clear()
        for job in mig_jobs[:JOB_BATCH_SIZE]:
            mig_registry[job.map_id] = job
        for job in sql_jobs[:JOB_BATCH_SIZE]:
            sql_registry[str(job.row_id)] = job
        for job in tuning_jobs[:JOB_BATCH_SIZE]:
            tuning_registry[str(job.row_id)] = job

        if mig_jobs:
            logger.info(
                f"[Supervisor] DataMigration 대기: {len(mig_jobs)}건 "
                f"/ 실행 대상: {len(mig_registry)}건"
            )
        if sql_jobs:
            logger.info(
                f"[Supervisor] SqlConversion 대기: {len(sql_jobs)}건 "
                f"/ 실행 대상: {len(sql_registry)}건"
            )
        if tuning_jobs:
            logger.info(
                f"[Supervisor] SqlTuning 대기: {len(tuning_jobs)}건 "
                f"/ 실행 대상: {len(tuning_registry)}건"
            )
        if not mig_jobs and not sql_jobs and not tuning_jobs:
            logger.info("[Supervisor] 대기 중인 작업 없음")

        return {
            "cycle": cycle,
            "stop_requested": False,
        }

    def execute_node(state: SupervisorState) -> dict:
        """Run up to 20 jobs for each agent from the current poll result."""
        if not mig_registry and not sql_registry and not tuning_registry:
            return {"stop_requested": _stop_event.is_set() or state.get("stop_requested", False)}

        logger.info("[Supervisor] 작업 실행 시작")

        if _RUN_MIGRATION:
            for job in list(mig_registry.values()):
                retry = getattr(job, "retry_count", 0) or 0
                if retry >= 3:
                    logger.warning(
                        f"[Supervisor] DataMigration map_id={job.map_id} skip "
                        f"(retry={retry} >= 3)"
                    )
                    continue
                supervisor_tools.run_data_migration.invoke({"map_id": job.map_id})

        if _RUN_SQL_CONVERSION:
            for job in list(sql_registry.values()):
                supervisor_tools.run_sql_conversion.invoke({"row_id": str(job.row_id)})

        if _RUN_SQL_TUNING:
            tuning_row_ids = [str(job.row_id) for job in tuning_registry.values()]
            if tuning_row_ids:
                supervisor_tools.run_sql_tuning.invoke({"row_ids": tuning_row_ids})

        return {"stop_requested": _stop_event.is_set() or state.get("stop_requested", False)}

    def wait_node(_state: SupervisorState) -> dict:
        """Flush metrics, respect pause flag, and wait before next poll."""
        supervisor_tools.finish_cycle_metrics(logger=logger)
        paused_logged = False
        while PAUSE_FLAG.exists():
            if _stop_event.is_set():
                return {"stop_requested": True}
            if not paused_logged:
                logger.info("[Supervisor] 일시정지 중... (runtime/agent.pause 감지)")
                paused_logged = True
            time.sleep(0.5)
        if paused_logged:
            logger.info("[Supervisor] 일시정지 해제, 재개합니다.")

        elapsed = 0.0
        step = 0.2
        while elapsed < POLL_INTERVAL_SEC:
            if _stop_event.is_set() or PAUSE_FLAG.exists():
                break
            time.sleep(step)
            elapsed += step
        return {"stop_requested": _stop_event.is_set()}

    workflow = StateGraph(SupervisorState)

    workflow.add_node("poll", poll_node)
    workflow.add_node("execute", execute_node)
    workflow.add_node("wait", wait_node)

    workflow.set_entry_point("poll")
    workflow.add_edge("poll", "execute")
    workflow.add_edge("execute", "wait")
    workflow.add_edge("wait", END)

    return workflow.compile()
