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

from server.core.llm_fallback import reset_active_model
from server.agents.supervisor.state import SupervisorState
import server.tools as supervisor_tools

# If any *_ONLY flag is enabled, run the selected agents only.
# If none are enabled, run the full pipeline.
_DB_MIGRATION_ONLY = os.getenv("DB_MIGRATION_ONLY", "false").lower() == "true"
_SQL_CONVERSION_ONLY = os.getenv("SQL_CONVERSION_ONLY", "false").lower() == "true"
_SQL_TUNING_ONLY = os.getenv("SQL_TUNING_ONLY", "false").lower() == "true"
_SQL_FORMATTING_ONLY = os.getenv("SQL_FORMATTING_ONLY", "false").lower() == "true"
_HAS_AGENT_SELECTION = _DB_MIGRATION_ONLY or _SQL_CONVERSION_ONLY or _SQL_TUNING_ONLY or _SQL_FORMATTING_ONLY
_RUN_MIGRATION = _DB_MIGRATION_ONLY or not _HAS_AGENT_SELECTION
_RUN_SQL_CONVERSION = _SQL_CONVERSION_ONLY or not _HAS_AGENT_SELECTION
_RUN_SQL_TUNING = _SQL_TUNING_ONLY or not _HAS_AGENT_SELECTION
_RUN_SQL_FORMATTING = _SQL_FORMATTING_ONLY or not _HAS_AGENT_SELECTION

_RUNTIME_DIR = Path(__file__).resolve().parent.parent.parent.parent / "runtime"
PAUSE_FLAG = _RUNTIME_DIR / "agent.pause"
POLL_INTERVAL_SEC = 5
JOB_BATCH_SIZE = 5

_stop_event = threading.Event()


def request_stop() -> None:
    _stop_event.set()


def is_stop_requested() -> bool:
    return _stop_event.is_set()


def _wait_while_paused(logger) -> bool:
    """Return True when a stop was requested while paused."""
    paused_logged = False
    while PAUSE_FLAG.exists():
        if _stop_event.is_set():
            return True
        if not paused_logged:
            logger.info("[Supervisor] 일시정지 중... (runtime/agent.pause 감지)")
            paused_logged = True
        time.sleep(0.5)
    if paused_logged:
        logger.info("[Supervisor] 일시정지 해제, 재개합니다.")
    return False


def build_supervisor_graph(
    get_migration_jobs,
    get_sql_jobs,
    get_tuning_jobs,
    get_formatting_jobs,
    mig_increment_batch,
    mig_process_job,
    sql_increment_batch,
    sql_process_job,
    tune_process_job,
    format_process_job,
    logger,
):
    mig_registry: dict = {}
    sql_registry: dict = {}
    tuning_registry: dict = {}
    formatting_registry: dict = {}

    supervisor_tools.init_callbacks(
        mig_inc=mig_increment_batch,
        mig_proc=mig_process_job,
        sql_inc=sql_increment_batch,
        sql_proc=sql_process_job,
        tune_proc=tune_process_job,
        format_proc=format_process_job,
        logger=logger,
    )
    mig_registry, sql_registry, tuning_registry, formatting_registry = supervisor_tools.get_registries()

    def poll_node(state: SupervisorState) -> dict:
        """Poll pending jobs and refresh current batch registries."""
        if _stop_event.is_set():
            return {"stop_requested": True, "cycle": state.get("cycle", 0) + 1}
        if _wait_while_paused(logger):
            return {"stop_requested": True, "cycle": state.get("cycle", 0) + 1}

        cycle = state.get("cycle", 0) + 1
        logger.info(f"\n{'=' * 50}")
        logger.info(f"[Supervisor] Batch loop {cycle} 시작")
        previous_model = reset_active_model()
        if previous_model:
            logger.info(
                f"[Supervisor] LLM active model reset for new cycle "
                f"(previous={previous_model})"
            )
        supervisor_tools.start_cycle_metrics(cycle)

        mig_jobs, sql_jobs, tuning_jobs, formatting_jobs = [], [], [], []
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
            if _RUN_SQL_FORMATTING:
                formatting_jobs = get_formatting_jobs()
        except Exception as exc:
            logger.error(f"[Supervisor] SQL/Tuning/Formatting polling error: {exc}")

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
        if formatting_jobs:
            logger.info(
                f"[Supervisor] SqlFormatting 대기: {len(formatting_jobs)}건 "
                f"/ 실행 대상: {len(formatting_registry)}건"
            )
        if not mig_jobs and not sql_jobs and not tuning_jobs and not formatting_jobs:
            logger.info("[Supervisor] 대기 중인 작업 없음")

        return {
            "cycle": cycle,
            "stop_requested": False,
        }

    def execute_node(state: SupervisorState) -> dict:
        """Run up to JOB_BATCH_SIZE jobs for each agent from the current poll result."""
        if not mig_registry and not sql_registry and not tuning_registry and not formatting_registry:
            return {"stop_requested": _stop_event.is_set() or state.get("stop_requested", False)}

        logger.info("[Supervisor] 작업 실행 시작")

        if _RUN_MIGRATION:
            for job in list(mig_registry.values()):
                if _stop_event.is_set():
                    break
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
                if _stop_event.is_set():
                    break
                supervisor_tools.run_sql_conversion.invoke({"row_id": str(job.row_id)})

        if _RUN_SQL_TUNING:
            tuning_row_ids = []
            for job in list(tuning_registry.values()):
                if _stop_event.is_set():
                    break
                tuning_row_ids.append(str(job.row_id))
            if tuning_row_ids:
                supervisor_tools.run_sql_tuning.invoke({"row_ids": tuning_row_ids})

        if _RUN_SQL_FORMATTING:
            formatting_row_ids = []
            for job in list(formatting_registry.values()):
                if _stop_event.is_set():
                    break
                formatting_row_ids.append(str(job.row_id))
            if formatting_row_ids:
                supervisor_tools.run_sql_formatting.invoke({"row_ids": formatting_row_ids})

        return {"stop_requested": _stop_event.is_set() or state.get("stop_requested", False)}

    def wait_node(_state: SupervisorState) -> dict:
        """Flush metrics, respect pause flag, and wait before next poll."""
        supervisor_tools.finish_cycle_metrics(logger=logger)
        if _wait_while_paused(logger):
            return {"stop_requested": True}

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
