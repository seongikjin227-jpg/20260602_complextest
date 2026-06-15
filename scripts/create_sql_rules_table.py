"""NEXT_SQL_RULES 테이블 생성 + tobe_rule_catalog.json 데이터 마이그레이션.

실행:
  python tools/create_sql_rules_table.py
"""
import sys, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts._bootstrap import ROOT_DIR
from dotenv import load_dotenv
load_dotenv(ROOT_DIR / ".env")

from server.services.sql.db_runtime import get_connection

DDL = """
CREATE TABLE NEXT_SQL_RULES (
    RULE_ID           VARCHAR2(100)  NOT NULL,
    RULE_TYPE         VARCHAR2(20)   DEFAULT 'SEARCH' NOT NULL,
    USE_YN            VARCHAR2(1)    DEFAULT 'Y' NOT NULL,
    GUIDANCE          VARCHAR2(4000) NOT NULL,
    EXAMPLE_BAD_SQL   CLOB,
    EXAMPLE_TUNED_SQL CLOB,
    CREATED_AT        TIMESTAMP DEFAULT SYSTIMESTAMP NOT NULL,
    UPDATED_AT        TIMESTAMP DEFAULT SYSTIMESTAMP NOT NULL,
    CONSTRAINT CK_NEXT_SQL_RULES_TYPE CHECK (RULE_TYPE IN ('GENERAL', 'SEARCH')),
    CONSTRAINT CK_NEXT_SQL_RULES_USE_YN CHECK (USE_YN IN ('Y', 'N')),
    CONSTRAINT PK_NEXT_SQL_RULES PRIMARY KEY (RULE_ID)
)
"""

RAG_PATH = (
    ROOT_DIR / "server" / "services" / "sql" / "data" / "rag" / "tobe_rule_catalog.json"
)


def table_exists(cur, table_name: str) -> bool:
    cur.execute(
        "SELECT COUNT(*) FROM USER_TABLES WHERE TABLE_NAME = :1",
        (table_name.upper(),),
    )
    return cur.fetchone()[0] > 0


def column_exists(cur, table_name: str, column_name: str) -> bool:
    cur.execute(
        """
        SELECT COUNT(*)
        FROM USER_TAB_COLUMNS
        WHERE TABLE_NAME = :1 AND COLUMN_NAME = :2
        """,
        (table_name.upper(), column_name.upper()),
    )
    return cur.fetchone()[0] > 0


def load_rules(path: Path, rule_type: str) -> list[dict]:
    if not path.exists():
        return []
    raw = json.loads(path.read_text(encoding="utf-8"))
    rows = raw.get("rules", raw if isinstance(raw, list) else [])
    return [{**row, "rule_type": rule_type} for row in rows if isinstance(row, dict)]


def main():
    rules = load_rules(RAG_PATH, "SEARCH")

    with get_connection() as conn:
        cur = conn.cursor()

        # ── 테이블 생성 ───────────────────────────────────────────────────────
        if table_exists(cur, "NEXT_SQL_RULES"):
            print("⚠️  NEXT_SQL_RULES 테이블이 이미 존재합니다. 데이터만 업서트합니다.")
            if not column_exists(cur, "NEXT_SQL_RULES", "RULE_TYPE"):
                cur.execute("ALTER TABLE NEXT_SQL_RULES ADD (RULE_TYPE VARCHAR2(20) DEFAULT 'SEARCH' NOT NULL)")
                print("✅ RULE_TYPE 컬럼 추가 완료")
            if not column_exists(cur, "NEXT_SQL_RULES", "USE_YN"):
                cur.execute("ALTER TABLE NEXT_SQL_RULES ADD (USE_YN VARCHAR2(1) DEFAULT 'Y' NOT NULL)")
                print("✅ USE_YN 컬럼 추가 완료")
        else:
            cur.execute(DDL)
            print("✅ NEXT_SQL_RULES 테이블 생성 완료")

        # ── 데이터 삽입 ───────────────────────────────────────────────────────
        inserted = 0
        skipped = 0
        for r in rules:
            rule_id = r.get("rule_id", "")
            rule_type = (r.get("rule_type") or "SEARCH").upper()
            guidance_lines = r.get("guidance", [])
            guidance = "\n".join(guidance_lines) if isinstance(guidance_lines, list) else str(guidance_lines)
            bad_sql = r.get("example_bad_sql", "") or ""
            tuned_sql = r.get("example_tuned_sql", "") or ""

            # 이미 존재하면 UPDATE, 없으면 INSERT
            cur.execute(
                "SELECT COUNT(*) FROM NEXT_SQL_RULES WHERE RULE_ID = :1",
                (rule_id,),
            )
            exists = cur.fetchone()[0] > 0

            if exists:
                cur.execute(
                    """UPDATE NEXT_SQL_RULES
                       SET RULE_TYPE = :1, GUIDANCE = :2, EXAMPLE_BAD_SQL = :3,
                           EXAMPLE_TUNED_SQL = :4, UPDATED_AT = SYSTIMESTAMP
                       WHERE RULE_ID = :5""",
                    (rule_type, guidance, bad_sql, tuned_sql, rule_id),
                )
                skipped += 1
                print(f"  ↻ UPDATED  {rule_id} ({rule_type})")
            else:
                cur.execute(
                    """INSERT INTO NEXT_SQL_RULES
                           (RULE_ID, RULE_TYPE, USE_YN, GUIDANCE, EXAMPLE_BAD_SQL, EXAMPLE_TUNED_SQL)
                       VALUES (:1, :2, 'Y', :3, :4, :5)""",
                    (rule_id, rule_type, guidance, bad_sql, tuned_sql),
                )
                inserted += 1
                print(f"  ✓ INSERTED {rule_id} ({rule_type})")

        conn.commit()
        print(f"\n완료 — 삽입: {inserted}건, 업데이트: {skipped}건 (총 {len(rules)}건)")


if __name__ == "__main__":
    main()
