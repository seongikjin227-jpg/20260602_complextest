"""NEXT_SQL_RULES 테이블 CRUD."""
from utils.db import get_connection, _s


def _has_column(cur, column_name: str) -> bool:
    cur.execute(
        "SELECT COUNT(*) FROM USER_TAB_COLUMNS "
        "WHERE TABLE_NAME = 'NEXT_SQL_RULES' AND COLUMN_NAME = :1",
        (column_name,),
    )
    return cur.fetchone()[0] > 0


def _has_rule_type_column(cur) -> bool:
    return _has_column(cur, "RULE_TYPE")


def get_all_rules() -> list[dict]:
    with get_connection() as conn:
        cur = conn.cursor()
        type_expr = "NVL(RULE_TYPE, 'SEARCH')" if _has_column(cur, "RULE_TYPE") else "'SEARCH'"
        hit_expr = "NVL(HIT_CNT, 0)" if _has_column(cur, "HIT_CNT") else "0"
        q = f"""
            SELECT RULE_ID, {type_expr} AS RULE_TYPE,
                   GUIDANCE, EXAMPLE_BAD_SQL, EXAMPLE_TUNED_SQL,
                   {hit_expr} AS HIT_CNT,
                   TO_CHAR(CREATED_AT, 'YYYY-MM-DD HH24:MI:SS') AS CREATED_AT,
                   TO_CHAR(UPDATED_AT, 'YYYY-MM-DD HH24:MI:SS') AS UPDATED_AT
            FROM NEXT_SQL_RULES
            ORDER BY CREATED_AT ASC
        """
        cur.execute(q)
        cols = [d[0] for d in cur.description]
        return [{cols[i]: _s(row[i]) for i in range(len(cols))} for row in cur.fetchall()]


def get_top_rules(limit: int = 5) -> list[dict]:
    """Return top tuning rules by HIT_CNT."""
    with get_connection() as conn:
        cur = conn.cursor()
        if not _has_column(cur, "HIT_CNT"):
            return []
        if _has_column(cur, "RULE_TYPE"):
            q = """
                SELECT RULE_ID, NVL(RULE_TYPE, 'SEARCH') AS RULE_TYPE,
                       GUIDANCE, EXAMPLE_BAD_SQL, EXAMPLE_TUNED_SQL,
                       NVL(HIT_CNT, 0) AS HIT_CNT
                FROM (
                    SELECT RULE_ID, RULE_TYPE, GUIDANCE, EXAMPLE_BAD_SQL,
                           EXAMPLE_TUNED_SQL, HIT_CNT
                    FROM NEXT_SQL_RULES
                    WHERE NVL(HIT_CNT, 0) > 0
                    ORDER BY HIT_CNT DESC NULLS LAST
                )
                WHERE ROWNUM <= :1
            """
        else:
            q = """
                SELECT RULE_ID, 'SEARCH' AS RULE_TYPE,
                       GUIDANCE, EXAMPLE_BAD_SQL, EXAMPLE_TUNED_SQL,
                       NVL(HIT_CNT, 0) AS HIT_CNT
                FROM (
                    SELECT RULE_ID, GUIDANCE, EXAMPLE_BAD_SQL,
                           EXAMPLE_TUNED_SQL, HIT_CNT
                    FROM NEXT_SQL_RULES
                    WHERE NVL(HIT_CNT, 0) > 0
                    ORDER BY HIT_CNT DESC NULLS LAST
                )
                WHERE ROWNUM <= :1
            """
        cur.execute(q, (limit,))
        cols = [d[0] for d in cur.description]
        return [{cols[i]: _s(row[i]) for i in range(len(cols))} for row in cur.fetchall()]


def add_rule(rule_id: str, guidance: str, bad_sql: str, tuned_sql: str = "", rule_type: str = "SEARCH") -> None:
    with get_connection() as conn:
        cur = conn.cursor()
        if _has_rule_type_column(cur):
            q = """
                INSERT INTO NEXT_SQL_RULES
                    (RULE_ID, RULE_TYPE, GUIDANCE, EXAMPLE_BAD_SQL, EXAMPLE_TUNED_SQL)
                VALUES (:1, :2, :3, :4, :5)
            """
            cur.execute(q, (rule_id, rule_type, guidance, bad_sql, tuned_sql))
        else:
            q = """
                INSERT INTO NEXT_SQL_RULES
                    (RULE_ID, GUIDANCE, EXAMPLE_BAD_SQL, EXAMPLE_TUNED_SQL)
                VALUES (:1, :2, :3, :4)
            """
            cur.execute(q, (rule_id, guidance, bad_sql, tuned_sql))
        conn.commit()


def update_rule(rule_id: str, guidance: str, bad_sql: str, tuned_sql: str = "", rule_type: str = "SEARCH") -> None:
    with get_connection() as conn:
        cur = conn.cursor()
        if _has_rule_type_column(cur):
            q = """
                UPDATE NEXT_SQL_RULES
                SET RULE_TYPE = :1, GUIDANCE = :2, EXAMPLE_BAD_SQL = :3,
                    EXAMPLE_TUNED_SQL = :4, UPDATED_AT = SYSTIMESTAMP
                WHERE RULE_ID = :5
            """
            cur.execute(q, (rule_type, guidance, bad_sql, tuned_sql, rule_id))
        else:
            q = """
                UPDATE NEXT_SQL_RULES
                SET GUIDANCE = :1, EXAMPLE_BAD_SQL = :2,
                    EXAMPLE_TUNED_SQL = :3, UPDATED_AT = SYSTIMESTAMP
                WHERE RULE_ID = :4
            """
            cur.execute(q, (guidance, bad_sql, tuned_sql, rule_id))
        conn.commit()


def delete_rule(rule_id: str) -> bool:
    q = "DELETE FROM NEXT_SQL_RULES WHERE RULE_ID = :1"
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(q, (rule_id,))
        deleted = cur.rowcount
        conn.commit()
    return deleted > 0


def rule_id_exists(rule_id: str) -> bool:
    q = "SELECT COUNT(*) FROM NEXT_SQL_RULES WHERE RULE_ID = :1"
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(q, (rule_id,))
        return cur.fetchone()[0] > 0


def get_next_rule_id() -> str:
    """USER_RULE_NNN 형식으로 다음 ID 생성."""
    q = """
        SELECT NVL(MAX(TO_NUMBER(REGEXP_SUBSTR(RULE_ID, '\\d+$'))), 0)
        FROM NEXT_SQL_RULES
        WHERE REGEXP_LIKE(RULE_ID, '^USER_RULE_\\d+$')
    """
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(q)
        n = cur.fetchone()[0] or 0
    return f"USER_RULE_{int(n) + 1:03d}"
