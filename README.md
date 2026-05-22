# onlysqlconv

Oracle/MyBatis SQL 변환, 바인드 값 추출, row count 검증, TO-BE SQL 튜닝을 자동화하는 Supervisor 기반 멀티 에이전트 파이프라인입니다.

현재 버전은 개발/테스트용입니다. 프롬프트와 검증 방식은 실행 결과에 따라 계속 조정될 수 있습니다.

## 전체 구조

```text
main.py
  -> Supervisor Agent
      -> Migration Agent
      -> SQL Conversion Agent
      -> SQL Tuning Agent
```

Supervisor는 DB 대기열을 polling하고, 각 에이전트에 작업을 dispatch합니다.

- Migration Agent: `NEXT_MIG_INFO` 기반 데이터 이관 SQL 생성/실행/검증
- SQL Conversion Agent: MyBatis 원본 SQL을 Oracle TO-BE SQL로 변환하고 row count 검증
- SQL Tuning Agent: 검증을 통과한 TO-BE SQL을 튜닝 규칙/RAG 기반으로 개선하고 재검증

## 실행 흐름

### 1. Migration Agent

1. `NEXT_MIG_INFO`에서 이관 대상 작업 조회
2. 소스/타겟 DDL 및 매핑 정보 조회
3. LLM으로 migration SQL 생성
4. Oracle에서 SQL 실행
5. row count 검증
6. 성공 시 `NEXT_SQL_INFO` 작업 생성

### 2. SQL Conversion Agent

1. `NEXT_SQL_INFO`에서 변환 대상 SQL 조회
2. `from_sql` 기준으로 TO-BE SQL 생성
3. 원본 SQL에서 bind parameter 추출
4. bind 후보 SQL 생성
5. bind SQL 실행 결과로 최대 3개 bind case 구성
6. source SQL과 target SQL을 count subquery로 감싸 검증 SQL 생성
7. 검증 SQL 실행 후 `PASS`/`FAIL` 판단
8. 성공 시 `TUNED_TEST=READY`로 튜닝 대상화

### 3. SQL Tuning Agent

1. `TUNED_TEST != PASS`인 TO-BE SQL 조회
2. universal tuning rules와 RAG 검색 결과를 프롬프트에 주입
3. LLM으로 tuned SQL 생성
4. 기존 TO-BE SQL과 tuned SQL row count 비교
5. `TUNED_SQL`, `TUNED_TEST` 저장

## Bind SQL 단계 최신 동작

Bind SQL 단계는 실제 테스트에 사용할 bind 값 조합을 DB에서 추출하는 단계입니다.

현재 기준은 다음과 같습니다.

- bind parameter와 후보 컬럼은 `from_sql` 기준으로 판단합니다.
- `bind_sql_prompt.json`에는 `tobe_sql`과 `mapping_schema_text`를 전달하지 않습니다.
- `from_sql`에 없는 테이블, 컬럼, 조인, SELECT list를 외부 매핑 정보 기준으로 추론하거나 재구성하지 않습니다.
- 최상위 SELECT는 `SELECT DISTINCT <컬럼> AS "<bind_param>" ...` 형태를 우선합니다.
- 복잡한 조인과 필터는 내부 row source에서 처리하고, 최상위 SELECT는 bind parameter alias 반환에 집중합니다.
- `SYSDATE`, `CURRENT_DATE`, `SYSTIMESTAMP`처럼 실행 시점에 의존하는 조건은 bind 후보 추출 과정에서 제거합니다.
- `SELECT DISTINCT`와 `ORDER BY`를 함께 쓸 경우 Oracle 제약상 ORDER BY 컬럼이 SELECT list에 그대로 있어야 하므로, bind SQL에서는 가능하면 ORDER BY를 제거합니다.
- 생성된 bind SQL에는 MyBatis XML 태그나 placeholder가 남으면 안 됩니다.

### Bind SQL 입력

`bind_sql_prompt.json`에 전달되는 입력은 다음뿐입니다.

- `from_sql`: 실제 bind 후보 row를 뽑을 기준 SQL
- `bind_param_metadata_json`: 추출해야 할 bind parameter 목록과 조건부 그룹 정보
- `bind_target_hints_json`: `param -> 후보 컬럼` 힌트
- `last_error`: 이전 실행 오류

`mapping_schema_text`와 `tobe_sql`은 bind prompt에 노출하지 않습니다.

## Bind 단계의 MyBatis 동적 태그 처리

Bind 단계에서 MyBatis 동적 태그는 크게 두 곳에서 처리됩니다.

### 1. bind metadata 생성

파일: `server/services/sql/binding_service.py`

`build_bind_param_metadata(sql_text)`는 SQL 안의 placeholder와 조건부 태그를 분석합니다.

처리 대상:

- `#{param}`
- `${param}`
- `<if test="...">...</if>`
- `<when test="...">...</when>`
- `<otherwise>...</otherwise>`

반환 구조:

```json
{
  "required_bind_params": ["id"],
  "conditional_bind_params": [
    {
      "tag": "if",
      "test": "name != null",
      "params": ["name"]
    }
  ],
  "all_bind_params": ["id", "name"]
}
```

의미:

- `all_bind_params`: 최종 bind SQL이 SELECT alias로 반환해야 하는 전체 파라미터
- `required_bind_params`: 조건부 태그 바깥에서 사용되는 필수 파라미터
- `conditional_bind_params`: `<if>`/`<when>`/`<otherwise>` 내부에서만 쓰이는 조건부 파라미터 그룹

현재 `generate_bind_sql()`은 `job.source_sql` 기준으로 metadata를 먼저 만들고, source SQL에 bind가 전혀 없을 때만 TO-BE SQL로 fallback합니다.

### 2. bind set 구성 시 조건부 분기 커버

파일: `server/services/sql/binding_service.py`

`build_bind_sets(tobe_sql, source_sql, bind_query_rows, max_cases=3)`는 bind SQL 실행 결과 row를 최대 3개 bind case로 줄입니다.

동작 방식:

1. parameter 이름은 `source_sql`에서 먼저 추출합니다.
2. 없으면 `tobe_sql`에서 fallback 추출합니다.
3. `<if>`/`<when>` test expression에서 조건 제어 파라미터를 추출합니다.
4. 각 bind row에 대해 조건부 그룹이 활성/비활성인지 signature를 계산합니다.
5. 가능한 한 서로 다른 조건부 분기 패턴을 대표하는 bind case를 먼저 선택합니다.
6. 부족하면 중복되지 않는 값 조합을 추가합니다.
7. 그래도 없으면 모든 parameter가 `None`인 fallback case를 만듭니다.

주의점:

- Bind SQL 자체가 MyBatis 태그를 실행하는 것은 아닙니다.
- Bind SQL 생성 프롬프트는 조건부 parameter를 활성화할 수 있는 실제 row가 있으면 값을 반환하고, 조건을 평가할 수 없으면 해당 블록을 비활성으로 간주하도록 지시합니다.
- 최종 검증 SQL 단계에서 `bind_set_json`을 기준으로 MyBatis 동적 태그를 실제 SQL로 materialize합니다.

## Test SQL 단계 최신 동작

Test SQL 단계는 이미 만들어진 source SQL과 target SQL을 비교하는 역할만 합니다.

현재 기준:

- `test_sql_prompt.json`에는 `mapping_schema_text`를 전달하지 않습니다.
- source SQL과 target SQL은 이미 비교 대상 SQL이므로 재작성하지 않습니다.
- 각 SQL은 가능한 한 원본 형태를 유지하고 `SELECT COUNT(*) FROM (<sql>)` 형태로 감싸 비교합니다.
- 수행 작업은 bind 값 치환, MyBatis 동적 태그 해석, ORDER BY 제거로 제한합니다.
- 스키마 판단은 `source_schema`, `target_schema`만 사용합니다.
- 매핑룰 기반으로 컬럼, 테이블, 조인, SELECT list를 재구성하지 않습니다.
- 반환 SQL은 `CASE_NO`, `FROM_COUNT`, `TO_COUNT` 세 컬럼만 출력합니다.
- bind case는 최대 3개만 사용합니다.

## Prompt 구성

프롬프트 파일 위치:

```text
server/config/prompts/
  bind_sql_prompt.json
  test_sql_prompt.json
  tobe_sql_prompt.json
  tobe_sql_tuning_prompt.json
  migration_prompt.json
  planner_prompt.json
```

프롬프트 렌더링 파일:

```text
server/services/sql/prompt_service.py
server/services/migration/prompt_service.py
```

프롬프트 JSON은 `utf-8-sig`로 읽습니다. 따라서 UTF-8 BOM이 있는 JSON과 BOM이 없는 JSON 모두 로드할 수 있습니다.

## 주요 서비스 파일

```text
server/services/sql/agents.py
  SQL Conversion Agent와 Tuning Agent 실행 흐름

server/services/sql/llm_service.py
  TO-BE SQL, bind SQL, test SQL, tuned SQL 생성을 위한 LLM 호출 wrapper

server/services/sql/binding_service.py
  bind parameter 추출, 조건부 bind metadata 생성, bind set 구성

server/services/sql/mybatis_materializer_service.py
  MyBatis 동적 SQL을 bind case 기준 실행 SQL로 materialize

server/services/sql/validation_service.py
  bind SQL/test SQL 실행 및 검증 결과 판단

server/services/sql/tobe_sql_tuning_service.py
  tuning rule 로딩, FAISS/RAG 검색, tuning context 생성

server/services/sql/xml_parser_service.py
  MyBatis mapper XML 파싱 및 NEXT_SQL_INFO 적재 보조
```

## MyBatis XML Parser

MyBatis mapper XML을 `NEXT_SQL_INFO`로 적재하기 위한 보조 서비스입니다.

```bash
python -m server.services.sql.xml_parser_service all
python -m server.services.sql.xml_parser_service stage1 --source-dir C:\path\to\mapper --output-dir C:\path\to\xml-json
python -m server.services.sql.xml_parser_service stage2 --output-dir C:\path\to\xml-json
python -m server.services.sql.xml_parser_service stage3
python -m server.services.sql.xml_parser_service stage4
```

환경 변수:

```env
MAPPER_XML_SOURCE_DIR=C:\path\to\mapper-xml
XML_PARSER_DATA_DIR=server/services/sql/DATA
ACTIVE_SQL_ID_TABLE=
ACTIVE_SQL_ID_COLUMN=SQL_ID
```

## MyBatis Materializer

파일: `server/services/sql/mybatis_materializer_service.py`

`materialize_sql(sql_text, bind_case)`는 MyBatis 동적 SQL을 특정 bind case 기준의 실행 가능한 SQL 문자열로 변환합니다.

지원 대상:

- `<if>`
- `<choose>` / `<when>` / `<otherwise>`
- `<where>`
- `<trim>`
- `<foreach>`
- `#{param}` / `${param}`

예시:

```python
from server.services.sql.mybatis_materializer_service import materialize_sql

sql = """
SELECT *
FROM USERS
<where>
  <if test="userId != null">AND USER_ID = #{userId}</if>
</where>
"""

print(materialize_sql(sql, {"userId": 100}))
```

## 환경 변수

`.env.example`을 복사해 `.env`를 만들고 실행 환경에 맞게 채웁니다.

```powershell
Copy-Item .env.example .env
```

주요 항목:

```env
DB_USER=
DB_PASS=
DB_HOST=localhost
DB_PORT=1521
DB_SID=xe
ORACLE_CLIENT_PATH=
ORACLE_SCHEMA=
ORACLE_SCHEMA_SRC=
ORACLE_SCHEMA_TGT=

MAPPING_RULE_TABLE=NEXT_MIG_INFO
MAPPING_RULE_DETAIL_TABLE=NEXT_MIG_INFO_DTL
RESULT_TABLE=NEXT_SQL_INFO

LLM_PROVIDER=openai
LLM_API_KEY=
LLM_MODEL=gpt-4o-mini
LLM_BASE_URL=
LLM_MAX_TOKENS=4096

RAG_EMBED_BASE_URL=
RAG_EMBED_API_KEY=
RAG_EMBED_MODEL=BAAI/bge-m3
RAG_EMBED_TIMEOUT_SEC=30
TOBE_RULE_CATALOG_PATH=server/services/sql/data/rag/tobe_rule_catalog.json
UNIVERSAL_TUNING_RULES_PATH=server/services/sql/data/rules/universal_tuning_rules.json
TOBE_SQL_TUNING_TOP_K=3
TOBE_SQL_TUNING_MAX_ITERATIONS=1

MAPPER_XML_SOURCE_DIR=
XML_PARSER_DATA_DIR=server/services/sql/DATA
ACTIVE_SQL_ID_TABLE=
ACTIVE_SQL_ID_COLUMN=SQL_ID

PLANNER_ENABLED=true
PLANNER_MAX_MIG_PER_CYCLE=5
SUPERVISOR_RECURSION_LIMIT=10000
MIG_KIND=DB_MIG
```

## 설치 및 실행

### 1. 의존성 설치

```bash
pip install -r requirements.txt
```

### 2. 사전 점검

```bash
python scripts/init_db.py
```

### 3. 에이전트 실행

```bash
python main.py
```

### 4. Streamlit 대시보드 실행

```bash
streamlit run app/app.py
```

## Streamlit 대시보드

```text
app/app.py
app/pages/dashboard.py
app/pages/mig_monitor.py
app/pages/sql_monitor.py
app/pages/tuning_monitor.py
app/pages/job_detail.py
app/pages/rag_manager_page.py
app/pages/system_health.py
app/pages/settings_page.py
```

기능:

- 전체 작업 현황 조회
- Migration/SQL/Tuning Agent 모니터링
- 개별 job 상세 조회
- RAG rule 관리
- DB/LLM/system health 확인
- agent process start/pause/resume/stop 제어

## DB 상태 컬럼

주요 컬럼:

| 컬럼 | 설명 |
| --- | --- |
| `STATUS` | SQL Conversion 상태. `URGENT`, `READY`, `PASS`, `FAIL`, `SKIP`, `PENDING` 등을 사용 |
| `TO_SQL_TEXT` | 생성된 TO-BE SQL |
| `BIND_SQL` | bind 후보 값을 추출하기 위해 생성된 SQL |
| `BIND_SET` | 검증에 사용할 bind case JSON |
| `TEST_SQL` | source SQL과 target SQL count 비교 SQL |
| `TUNED_SQL` | 튜닝된 SQL |
| `TUNED_TEST` | 튜닝 검증 상태. `READY`, `PASS`, `FAIL` 등 |
| `BLOCK_RAG_CONTENT` | 튜닝 프롬프트에 사용된 RAG context |
| `BATCH_CNT` | 처리/재시도 횟수 |

## 실행 시간 집계

Supervisor는 에이전트 실행 시간을 `AG_AGENT_RUN_METRICS`에 기록합니다.

```sql
CREATE TABLE AG_AGENT_RUN_METRICS (
    RUN_ID              NUMBER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    BATCH_NO            NUMBER,
    CYCLE_NO            NUMBER,
    AGENT_NAME          VARCHAR2(50) NOT NULL,
    JOB_COUNT           NUMBER DEFAULT 0 NOT NULL,
    SUCCESS_COUNT       NUMBER DEFAULT 0 NOT NULL,
    FAIL_COUNT          NUMBER DEFAULT 0 NOT NULL,
    SKIP_COUNT          NUMBER DEFAULT 0 NOT NULL,
    STARTED_AT          TIMESTAMP,
    FINISHED_AT         TIMESTAMP,
    ELAPSED_SECONDS     NUMBER,
    CREATED_AT          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

## 보조 스크립트

```bash
python scripts/create_sql_rules_table.py
python scripts/list_mapping_rules.py --format table
python scripts/list_mapping_rules.py --fr-table EMPLOYEES --format json
python scripts/seed_mig_rules.py
python scripts/init_db.py
```

## 프로젝트 구조

```text
.
├── main.py
├── requirements.txt
├── app/
│   ├── app.py
│   ├── pages/
│   └── utils/
├── scripts/
├── server/
│   ├── agents/
│   │   ├── migration/
│   │   ├── sql_conversion/
│   │   ├── sql_tuning/
│   │   └── supervisor/
│   ├── config/
│   │   └── prompts/
│   ├── core/
│   ├── repositories/
│   ├── services/
│   │   ├── migration/
│   │   └── sql/
│   └── tools/
└── tests/
```

## 현재 설계상 주의사항

- Bind SQL은 source/from SQL 기준입니다. target SQL이나 mapping rule 기준으로 후보 값을 재구성하지 않습니다.
- Test SQL은 source SQL과 target SQL을 count subquery로 감싸 비교합니다. 매핑룰 기반 재작성은 하지 않습니다.
- 프롬프트 JSON에 BOM이 있어도 `utf-8-sig`로 읽기 때문에 로드 오류가 나지 않아야 합니다.
- LLM 출력 SQL은 MyBatis 태그, placeholder, markdown, 세미콜론이 남지 않도록 프롬프트와 후처리에서 제한합니다.