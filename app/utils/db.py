import os
import oracledb
from pathlib import Path
from dotenv import load_dotenv

_ROOT = Path(__file__).resolve().parent.parent.parent
load_dotenv(_ROOT / ".env")

oracledb.defaults.fetch_lobs = False

DB_USER = os.getenv("DB_USER", "scott")
DB_PASS = os.getenv("DB_PASS", "tiger")
DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = os.getenv("DB_PORT", "1521")
DB_SID  = os.getenv("DB_SID", "xe")
ORACLE_CLIENT_PATH = os.getenv("ORACLE_CLIENT_PATH", "")

MIG_TABLE    = os.getenv("MAPPING_RULE_TABLE", "NEXT_MIG_INFO")
MIG_DTL_TABLE = os.getenv("MAPPING_RULE_DETAIL_TABLE", "NEXT_MIG_INFO_DTL").strip()
SQL_TABLE    = os.getenv("RESULT_TABLE", "NEXT_SQL_INFO")
AGENT_METRICS_TABLE = os.getenv("AGENT_METRICS_TABLE", "AG_AGENT_RUN_METRICS")
SQL_LOG_TABLE = os.getenv("SQL_LOG_TABLE", "NEXT_SQL_LOG")

_thick_done = False


def get_connection():
    global _thick_done
    if ORACLE_CLIENT_PATH and os.path.exists(ORACLE_CLIENT_PATH) and not _thick_done:
        try:
            oracledb.init_oracle_client(lib_dir=ORACLE_CLIENT_PATH)
        except oracledb.ProgrammingError:
            pass
        _thick_done = True
    dsn = DB_HOST if ("/" in DB_HOST or "(" in DB_HOST) else f"{DB_HOST}:{DB_PORT}/{DB_SID}"
    conn = oracledb.connect(user=DB_USER, password=DB_PASS, dsn=dsn)
    with conn.cursor() as cur:
        cur.execute("ALTER SESSION SET NLS_DATE_FORMAT='YYYY-MM-DD HH24:MI:SS'")
    return conn


def _s(val, default="") -> str:
    if val is None:
        return default
    if hasattr(val, "read"):
        val = val.read()
    if val is None:
        return default
    if isinstance(val, bytes):
        return val.decode("utf-8", errors="ignore")
    return str(val)


def _to_dicts(cur) -> list[dict]:
    cols = [d[0] for d in cur.description]
    return [{cols[i]: _s(row[i]) for i in range(len(cols))} for row in cur.fetchall()]


# ── Mig ──────────────────────────────────────────────────────────────────────

def get_mig_jobs() -> list[dict]:
    q = f"""
        SELECT MAP_ID, MAP_TYPE, FR_TABLE, TO_TABLE,
               USE_YN, TARGET_YN, PRIORITY, STATUS,
               MIG_SQL, VERIFY_SQL,
               BATCH_CNT, ELAPSED_SECONDS, RETRY_COUNT,
               TO_CHAR(CREATED_AT) AS CREATED_AT,
               TO_CHAR(UPD_TS) AS UPD_TS
        FROM {MIG_TABLE}
        ORDER BY PRIORITY ASC, MAP_ID ASC
    """
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(q)
        return _to_dicts(cur)


def get_mig_status_summary() -> dict[str, int]:
    q = f"SELECT NVL(TO_CHAR(STATUS),'NULL'), COUNT(*) FROM {MIG_TABLE} GROUP BY STATUS"
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(q)
        return {_s(r[0]) or "NULL": r[1] for r in cur.fetchall()}


def get_mig_dtl(map_id: int) -> list[dict]:
    q = f"""
        SELECT MAP_DTL, FR_COL, TO_COL
        FROM {MIG_DTL_TABLE}
        WHERE MAP_ID = :1
        ORDER BY MAP_DTL
    """
    try:
        with get_connection() as conn:
            cur = conn.cursor()
            cur.execute(q, (map_id,))
            return _to_dicts(cur)
    except Exception:
        return []


def get_mig_logs(map_id: int) -> list[dict]:
    q = """
        SELECT LOG_ID, MIG_KIND, LOG_TYPE, LOG_LEVEL,
               STEP_NAME, STATUS, MESSAGE, RETRY_COUNT
        FROM NEXT_MIG_LOG
        WHERE MAP_ID = :1
        ORDER BY LOG_ID ASC
    """
    try:
        with get_connection() as conn:
            cur = conn.cursor()
            cur.execute(q, (map_id,))
            return _to_dicts(cur)
    except Exception:
        return []


def get_recent_fails(limit: int = 10) -> list[dict]:
    q = f"""
        SELECT * FROM (
            SELECT MAP_ID, FR_TABLE, TO_TABLE, STATUS,
                   TO_CHAR(UPD_TS) AS UPD_TS
            FROM {MIG_TABLE}
            WHERE UPPER(NVL(STATUS,'X')) = 'FAIL'
            ORDER BY UPD_TS DESC NULLS LAST
        ) WHERE ROWNUM <= {limit}
    """
    try:
        with get_connection() as conn:
            cur = conn.cursor()
            cur.execute(q)
            return _to_dicts(cur)
    except Exception:
        return []


# ── Tuning 전용 요약 ──────────────────────────────────────────────────────────

def get_tuning_status_summary() -> dict[str, int]:
    """TUNED_TEST 컬럼 기준 상태 요약 (SQL이 변환된 행만)."""
    q = f"""
        SELECT NVL(TO_CHAR(TUNED_TEST), 'NULL'), COUNT(*)
        FROM {SQL_TABLE}
        WHERE TO_SQL_TEXT IS NOT NULL
        GROUP BY TUNED_TEST
    """
    try:
        with get_connection() as conn:
            cur = conn.cursor()
            cur.execute(q)
            return {_s(r[0]) or "NULL": r[1] for r in cur.fetchall()}
    except Exception:
        return {}


def get_formatting_summary() -> dict[str, int]:
    """Return formatting guide application counts for completed tuning rows."""
    q = f"""
        SELECT
            COUNT(*) AS TOTAL,
            SUM(
                CASE
                    WHEN FORMATTED_SQL IS NOT NULL
                     AND DBMS_LOB.GETLENGTH(FORMATTED_SQL) > 0
                     AND LENGTH(TRIM(DBMS_LOB.SUBSTR(FORMATTED_SQL, 4000, 1))) > 0
                    THEN 1
                    ELSE 0
                END
            ) AS APPLIED
        FROM {SQL_TABLE}
        WHERE UPPER(TRIM(TUNED_TEST)) IN ('PASS', 'PASS_NON_SELECT')
    """
    try:
        with get_connection() as conn:
            cur = conn.cursor()
            cur.execute(q)
            row = cur.fetchone()
            total = int(row[0] or 0) if row else 0
            applied = int(row[1] or 0) if row else 0
            return {
                "TOTAL": total,
                "APPLIED": applied,
                "PENDING": max(total - applied, 0),
            }
    except Exception:
        return {}


# ── SQL / Tuning ──────────────────────────────────────────────────────────────

def get_sql_jobs() -> list[dict]:
    q = f"""
        SELECT ROWIDTOCHAR(ROWID) AS ROW_ID,
               TAG_KIND, SPACE_NM, SQL_ID,
               FR_SQL_TEXT, TO_SQL_TEXT, TUNED_SQL, TUNED_TEST, TUNED_RESULT,
               FORMATTED_SQL, BLOCK_RAG_CONTENT,
               STATUS, LOG, TO_CHAR(UPD_TS) AS UPD_TS
        FROM {SQL_TABLE}
        ORDER BY UPD_TS DESC NULLS LAST
    """
    try:
        with get_connection() as conn:
            cur = conn.cursor()
            cur.execute(q)
            return _to_dicts(cur)
    except Exception:
        return []


def get_sql_status_summary() -> dict[str, int]:
    q = f"SELECT NVL(TO_CHAR(STATUS),'NULL'), COUNT(*) FROM {SQL_TABLE} GROUP BY STATUS"
    try:
        with get_connection() as conn:
            cur = conn.cursor()
            cur.execute(q)
            return {_s(r[0]) or "NULL": r[1] for r in cur.fetchall()}
    except Exception:
        return {}


def get_xml_export_sqls() -> list[dict]:
    """Return tuning rows used by XML export, including namespace status counts."""
    q = f"""
        SELECT SPACE_NM, TAG_KIND, SQL_ID, TUNED_TEST, FORMATTED_SQL
        FROM {SQL_TABLE}
        WHERE SPACE_NM IS NOT NULL
          AND SQL_ID IS NOT NULL
          AND (TUNED_TEST IS NOT NULL OR FORMATTED_SQL IS NOT NULL)
        ORDER BY SPACE_NM, SQL_ID
    """
    try:
        with get_connection() as conn:
            cur = conn.cursor()
            cur.execute(q)
            return _to_dicts(cur)
    except Exception:
        return []


def get_tuned_pass_sqls() -> list[dict]:
    """Backward-compatible alias for older XML export callers."""
    return get_xml_export_sqls()


# ── Agent operation metrics ─────────────────────────────────────────────

def get_recent_agent_run_metrics(limit: int = 200) -> list[dict]:
    q = f"""
        SELECT *
        FROM (
            SELECT RUN_ID, BATCH_NO, CYCLE_NO, AGENT_NAME,
                   JOB_COUNT, SUCCESS_COUNT, FAIL_COUNT, SKIP_COUNT,
                   TO_CHAR(STARTED_AT, 'YYYY-MM-DD HH24:MI:SS') AS STARTED_AT,
                   TO_CHAR(FINISHED_AT, 'YYYY-MM-DD HH24:MI:SS') AS FINISHED_AT,
                   ELAPSED_SECONDS
            FROM {AGENT_METRICS_TABLE}
            ORDER BY RUN_ID DESC
        )
        WHERE ROWNUM <= :1
    """
    try:
        with get_connection() as conn:
            cur = conn.cursor()
            cur.execute(q, (int(limit),))
            return _to_dicts(cur)
    except Exception:
        return []


def get_agent_batch_summary(limit: int = 50) -> list[dict]:
    q = f"""
        SELECT *
        FROM (
            SELECT BATCH_NO,
                   MIN(STARTED_AT) AS STARTED_AT,
                   MAX(FINISHED_AT) AS FINISHED_AT,
                   ROUND((MAX(FINISHED_AT) - MIN(STARTED_AT)) * 86400, 3) AS WALL_SECONDS,
                   SUM(NVL(ELAPSED_SECONDS, 0)) AS SUM_AGENT_SECONDS,
                   SUM(NVL(JOB_COUNT, 0)) AS JOB_COUNT,
                   SUM(NVL(SUCCESS_COUNT, 0)) AS SUCCESS_COUNT,
                   SUM(NVL(FAIL_COUNT, 0)) AS FAIL_COUNT,
                   SUM(NVL(SKIP_COUNT, 0)) AS SKIP_COUNT,
                   COUNT(*) AS AGENT_RUNS
            FROM {AGENT_METRICS_TABLE}
            GROUP BY BATCH_NO
            ORDER BY BATCH_NO DESC
        )
        WHERE ROWNUM <= :1
    """
    try:
        with get_connection() as conn:
            cur = conn.cursor()
            cur.execute(q, (int(limit),))
            rows = []
            for r in cur.fetchall():
                rows.append({
                    "BATCH_NO": _s(r[0]),
                    "STARTED_AT": _s(r[1]),
                    "FINISHED_AT": _s(r[2]),
                    "WALL_SECONDS": _s(r[3]),
                    "SUM_AGENT_SECONDS": _s(r[4]),
                    "JOB_COUNT": _s(r[5]),
                    "SUCCESS_COUNT": _s(r[6]),
                    "FAIL_COUNT": _s(r[7]),
                    "SKIP_COUNT": _s(r[8]),
                    "AGENT_RUNS": _s(r[9]),
                })
            return rows
    except Exception:
        return []


def get_agent_name_summary(limit: int = 500) -> list[dict]:
    q = f"""
        SELECT AGENT_NAME,
               COUNT(*) AS RUN_COUNT,
               SUM(NVL(JOB_COUNT, 0)) AS JOB_COUNT,
               ROUND(AVG(NVL(ELAPSED_SECONDS, 0)), 3) AS AVG_SECONDS,
               ROUND(MIN(NVL(ELAPSED_SECONDS, 0)), 3) AS MIN_SECONDS,
               ROUND(MAX(NVL(ELAPSED_SECONDS, 0)), 3) AS MAX_SECONDS,
               SUM(NVL(SUCCESS_COUNT, 0)) AS SUCCESS_COUNT,
               SUM(NVL(FAIL_COUNT, 0)) AS FAIL_COUNT,
               SUM(NVL(SKIP_COUNT, 0)) AS SKIP_COUNT
        FROM (
            SELECT *
            FROM {AGENT_METRICS_TABLE}
            ORDER BY RUN_ID DESC
        )
        WHERE ROWNUM <= :1
        GROUP BY AGENT_NAME
        ORDER BY AVG_SECONDS DESC
    """
    try:
        with get_connection() as conn:
            cur = conn.cursor()
            cur.execute(q, (int(limit),))
            return _to_dicts(cur)
    except Exception:
        return []


def get_sql_stage_summary(limit: int = 100) -> list[dict]:
    q = f"""
        SELECT NVL(STAGE_NAME, SQL_KIND) AS STAGE_NAME,
               COUNT(*) AS LOG_COUNT,
               ROUND(AVG(NVL(ELAPSED_SECONDS, 0)), 3) AS AVG_SECONDS,
               ROUND(MIN(NVL(ELAPSED_SECONDS, 0)), 3) AS MIN_SECONDS,
               ROUND(MAX(NVL(ELAPSED_SECONDS, 0)), 3) AS MAX_SECONDS,
               SUM(CASE WHEN UPPER(NVL(STATUS, '')) IN ('PASS', 'SUCCESS') THEN 1 ELSE 0 END) AS PASS_COUNT,
               SUM(CASE WHEN UPPER(NVL(STATUS, '')) = 'FAIL' THEN 1 ELSE 0 END) AS FAIL_COUNT,
               SUM(CASE WHEN ERROR_MESSAGE IS NOT NULL THEN 1 ELSE 0 END) AS ERROR_COUNT
        FROM (
            SELECT *
            FROM {SQL_LOG_TABLE}
            ORDER BY LOG_ID DESC
        )
        WHERE ROWNUM <= :1
        GROUP BY NVL(STAGE_NAME, SQL_KIND)
        ORDER BY AVG_SECONDS DESC, LOG_COUNT DESC
    """
    try:
        with get_connection() as conn:
            cur = conn.cursor()
            cur.execute(q, (int(limit),))
            return _to_dicts(cur)
    except Exception:
        return []


def get_recent_sql_stage_logs(limit: int = 100) -> list[dict]:
    q = f"""
        SELECT *
        FROM (
            SELECT LOG_ID,
                   TO_CHAR(CREATED_AT, 'YYYY-MM-DD HH24:MI:SS') AS CREATED_AT,
                   SPACE_NM, SQL_ID, SQL_KIND, STATUS, STAGE_NAME,
                   PROMPT_NAME, MODEL_NAME, BATCH_NO, CYCLE_NO,
                   ELAPSED_SECONDS, ATTEMPT_NO, ERROR_MESSAGE
            FROM {SQL_LOG_TABLE}
            ORDER BY LOG_ID DESC
        )
        WHERE ROWNUM <= :1
    """
    try:
        with get_connection() as conn:
            cur = conn.cursor()
            cur.execute(q, (int(limit),))
            return _to_dicts(cur)
    except Exception:
        return []


def get_sql_job_full(row_id: str) -> dict | None:
    q = f"""
        SELECT ROWIDTOCHAR(ROWID) AS ROW_ID,
               TAG_KIND, SPACE_NM, SQL_ID,
               FR_SQL_TEXT, EDIT_FR_SQL, TARGET_TABLE,
               TO_SQL_TEXT, TUNED_SQL, TUNED_TEST, TUNED_RESULT,
               BIND_SQL, BIND_SET, TEST_SQL,
               FORMATTED_SQL, BLOCK_RAG_CONTENT,
               STATUS, LOG, TO_CHAR(UPD_TS) AS UPD_TS, EDITED_YN
        FROM {SQL_TABLE}
        WHERE ROWIDTOCHAR(ROWID) = :1
    """
    try:
        with get_connection() as conn:
            cur = conn.cursor()
            cur.execute(q, (row_id,))
            row = cur.fetchone()
            if row:
                cols = [d[0] for d in cur.description]
                return {cols[i]: _s(row[i]) for i in range(len(cols))}
    except Exception:
        pass
    return None
