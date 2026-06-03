# onlysqlconv

`onlysqlconv`는 Oracle/MyBatis 기반 SQL 전환과 검증을 자동화하는 파이프라인입니다. `NEXT_SQL_INFO`의 원본 MyBatis SQL을 읽고, LLM으로 TO-BE SQL, Bind SQL, Test SQL을 생성한 뒤 Oracle에서 실제 실행한 row count 결과로 `PASS`/`FAIL`을 판단합니다.

현재 구조는 SQL 변환, bind 후보 추출, validation SQL 생성, 선택적 튜닝을 분리합니다. 예전 `bind_param_metadata_json`, `bind_target_hints_json` 방식은 제거됐고, bind parameter 판단과 alias 설계는 프롬프트와 LLM이 담당합니다.

## 전체 흐름

```text
main.py
  -> Supervisor Agent
      -> Migration Agent
      -> SQL Conversion Agent
      -> SQL Tuning Agent
```

- Migration Agent: `NEXT_MIG_INFO` 기반으로 migration SQL을 생성/실행합니다.
- SQL Conversion Agent: `NEXT_SQL_INFO`의 MyBatis SQL을 TO-BE SQL로 변환하고 source/target row count를 검증합니다.
- SQL Tuning Agent: baseline TO-BE SQL이 검증된 뒤, tuning rule RAG를 사용해 SQL을 개선하고 다시 검증합니다.

## 패키지 구조

```text
onlysqlconv-master/
  README.md
  requirements.txt
  main.py
  app/
  scripts/
  server/
  tests/
```

### 루트

```text
main.py
  Supervisor Agent를 시작하는 실행 진입점입니다.

requirements.txt
  Python 의존성 목록입니다.

README.md
  현재 코드 구조, SQL Conversion 흐름, prompt 입력, 실행 방법을 설명합니다.
```

### app

Streamlit 기반 운영/모니터링 화면입니다.

```text
app/
  app.py
  pages/
    dashboard.py
    mig_monitor.py
    sql_monitor.py
    tuning_monitor.py
    job_detail.py
    rag_manager_page.py
    system_health.py
    settings_page.py
  utils/
    agent_control.py
    db.py
    env_manager.py
    rag_db.py
    rag_manager.py
```

- `app/app.py`: Streamlit 앱 진입점입니다.
- `app/pages/dashboard.py`: 전체 작업 현황을 보여줍니다.
- `app/pages/mig_monitor.py`: Migration Agent 작업 상태를 모니터링합니다.
- `app/pages/sql_monitor.py`: SQL Conversion 결과와 상태를 확인합니다.
- `app/pages/tuning_monitor.py`: SQL Tuning 결과와 상태를 확인합니다.
- `app/pages/job_detail.py`: 개별 job의 SQL, bind set, test SQL, 로그를 확인합니다.
- `app/pages/rag_manager_page.py`: Tuning Rule Manager 화면에서 tuning rule 데이터를 관리합니다.
- `app/pages/system_health.py`: DB, LLM, runtime 상태를 확인합니다.
- `app/pages/settings_page.py`: 환경 설정 값을 확인/관리합니다.
- `app/utils/*`: 화면에서 사용하는 DB, 환경, agent 제어, tuning rule 관리 유틸입니다.

### scripts

운영 보조 스크립트입니다.

```text
scripts/
  _bootstrap.py
  init_db.py
  create_sql_log_table.py
  create_sql_rules_table.py
  add_sql_info_classification_columns.py
  add_formatted_sql_column.py
  add_tuned_result_column.py
  seed_mig_rules.py
  list_mapping_rules.py
  generate_diagrams.py
```

- `_bootstrap.py`: 스크립트 실행 시 import path를 맞춥니다.
- `init_db.py`: 초기 DB object 또는 기본 데이터를 준비합니다.
- `create_sql_log_table.py`: `NEXT_SQL_LOG` append-only 로그 테이블을 생성/보정합니다.
- `create_sql_rules_table.py`: SQL/tuning rule 관련 테이블을 생성합니다.
- `add_sql_info_classification_columns.py`: `NEXT_SQL_INFO.SQL_LENGTH`, `MAP_TYPE` 컬럼을 추가합니다.
- `add_formatted_sql_column.py`: `NEXT_SQL_INFO.FORMATTED_SQL` 컬럼을 추가합니다.
- `add_tuned_result_column.py`: `NEXT_SQL_INFO.TUNED_RESULT` 컬럼을 추가합니다.
- `seed_mig_rules.py`: migration rule seed 데이터를 적재합니다.
- `list_mapping_rules.py`: 현재 mapping rule을 조회합니다.
- `generate_diagrams.py`: 문서/분석용 다이어그램을 생성합니다.

## server 구조

```text
server/
  agents/
  config/
  core/
  repositories/
  services/
  tools/
```

### server/agents

Agent 단위의 graph, orchestrator, scheduler, state가 있는 영역입니다.

```text
server/agents/
  supervisor/
    agent.py
    graph.py
    state.py
  migration/
    orchestrator.py
    graph.py
    scheduler.py
    executor.py
    verifier.py
    sql_utils.py
    state.py
  sql_conversion/
    agent.py
  sql_tuning/
    agent.py
```

- `supervisor/agent.py`: 전체 agent 실행을 총괄합니다.
- `supervisor/graph.py`: Supervisor workflow graph를 구성합니다.
- `migration/orchestrator.py`: migration 작업 흐름을 조율합니다.
- `migration/scheduler.py`: migration 대상 작업을 선택합니다.
- `migration/executor.py`: 생성된 migration SQL을 실행합니다.
- `migration/verifier.py`: migration 결과를 검증합니다.
- `sql_conversion/agent.py`: SQL Conversion agent wrapper입니다.
- `sql_tuning/agent.py`: SQL Tuning agent wrapper입니다.

### server/config

환경 설정과 prompt template이 있습니다.

```text
server/config/
  settings.py
  prompts/
    migration_prompt.json
    planner_prompt.json
    tobe_sql_prompt.json
    tobe_sql_tuning_prompt.json
    bind_sql_prompt.json
    bind_sql_final_retry_prompt.json
    test_sql_prompt.json
    test_sql_final_retry_prompt.json
```

- `settings.py`: `.env` 기반 설정을 로드합니다.
- `tobe_sql_prompt.json`: 원본 MyBatis SQL을 TO-BE SQL로 변환합니다.
- `bind_sql_prompt.json`: 일반 시도에서 bind 후보 값을 조회하는 SQL을 생성합니다.
- `bind_sql_final_retry_prompt.json`: 마지막 재시도에서 동적 태그를 제거하고 정적 bind만 추출합니다.
- `test_sql_prompt.json`: 일반 validation SQL을 생성합니다.
- `test_sql_final_retry_prompt.json`: 마지막 재시도에서 동적 태그를 제거한 validation SQL을 생성합니다.
- `tobe_sql_tuning_prompt.json`: TO-BE SQL tuning rule을 적용합니다.

### server/core

공통 infrastructure 코드입니다.

```text
server/core/
  db.py
  db_migration.py
  exceptions.py
  llm.py
  logger.py
```

- `db.py`, `db_migration.py`: Oracle DB 연결과 실행 보조 로직입니다.
- `exceptions.py`: 공통 예외 타입입니다.
- `llm.py`: LLM 공통 설정/호출 보조 로직입니다.
- `logger.py`: 공통 logger 설정입니다.

### server/repositories

DB 접근 계층입니다.

```text
server/repositories/
  migration/
    repository.py
    history_repository.py
  sql/
    mapper_repository.py
    result_repository.py
  supervisor/
    metrics_repository.py
```

- `migration/repository.py`: migration 대상과 rule 정보를 조회/갱신합니다.
- `migration/history_repository.py`: migration 실행 이력을 관리합니다.
- `sql/mapper_repository.py`: SQL Conversion mapping rule과 skip 조건을 조회합니다.
- `sql/result_repository.py`: `NEXT_SQL_INFO`의 TO-BE SQL, BIND_SQL, BIND_SET, TEST_SQL, 상태, correct SQL corpus를 관리합니다.
- `supervisor/metrics_repository.py`: Agent 실행 지표를 저장합니다.

### server/services/sql

SQL Conversion과 SQL Tuning의 핵심 로직입니다.

```text
server/services/sql/
  agents.py
  batch_scheduler.py
  binding_service.py
  correct_sql_rag_service.py
  db_runtime.py
  domain_models.py
  llm_service.py
  prompt_service.py
  sql_formatting_service.py
  tobe_sql_tuning_service.py
  validation_service.py
  xml_parser_service.py
  workflow/
    graph.py
    state.py
  data/
    rag/
      tobe_rule_catalog.json
    rules/
      universal_tuning_rules.json
  PROMPT_DEBUG_SNIPPET.md
  SQL_FORMATTING_GUIDE.md
```

- `agents.py`: TO-BE SQL 생성, Bind SQL 실행, Test SQL 검증, Tuning 검증 흐름을 조율합니다.
- `batch_scheduler.py`: SQL conversion batch 실행을 스케줄링합니다.
- `binding_service.py`: bind parameter 존재 여부 감지와 `BIND_SET` JSON 생성을 담당합니다.
- `correct_sql_rag_service.py`: `NEXT_SQL_INFO`의 `TOBE_CORRECT_SQL`, `BIND_CORRECT_SQL`, `TEST_CORRECT_SQL`을 FAISS/token fallback으로 검색해 prompt hint를 만듭니다.
- `db_runtime.py`: SQL Conversion 실행 중 필요한 DB helper입니다.
- `domain_models.py`: SQL job과 mapping rule 데이터 모델입니다.
- `llm_service.py`: TO-BE SQL, Bind SQL, Test SQL, Tuning SQL 생성을 위한 LLM 호출 wrapper입니다.
- `prompt_service.py`: prompt JSON을 로드하고 message payload로 렌더링합니다.
- `sql_formatting_service.py`: SQL 저장 전 formatting 보조 로직입니다.
- `tobe_sql_tuning_service.py`: tuning rule RAG 검색과 tuning context 생성을 담당합니다.
- `validation_service.py`: Bind SQL과 Test SQL을 Oracle에서 실행하고 검증 결과를 판정합니다.
- `xml_parser_service.py`: MyBatis mapper XML을 파싱해 `NEXT_SQL_INFO`에 적재합니다. WITH 절 CTE 이름은 실제 테이블이 아니므로 `TARGET_TABLE`에서 제외합니다.
- `workflow/graph.py`: SQL Conversion/Tuning workflow graph를 구성합니다.
- `workflow/state.py`: SQL workflow 실행 상태 모델입니다.

### server/tools

Agent graph에서 호출할 수 있는 tool wrapper 영역입니다.

```text
server/tools/
  context.py
  migration.py
  sql_conversion.py
  sql_tuning.py
```

## SQL Conversion 흐름

1. `NEXT_SQL_INFO`에서 변환 대상 SQL job을 읽습니다.
2. `tobe_sql_prompt.json`으로 TO-BE SQL을 생성합니다.
3. 원본 SQL 또는 TO-BE SQL에서 bind parameter 존재 여부를 감지합니다.
4. bind parameter가 없으면 Bind SQL 단계는 건너뛰고 `bind_set_json_for_test = "[{}]"`로 둡니다.
5. bind parameter가 있으면 `bind_sql_prompt.json`으로 Bind SQL을 생성합니다.
6. Bind SQL을 Oracle에서 실행하고 최대 3개의 bind case로 `BIND_SET`을 구성합니다.
7. `test_sql_prompt.json`으로 source/target row count 비교용 Test SQL을 생성합니다.
8. Test SQL 실행 결과의 `CASE_NO`, `FROM_COUNT`, `TO_COUNT`로 검증합니다.
9. 통과하면 `PASS`, 실패하면 retry 또는 `FAIL`로 처리합니다.

### SQL job status

`NEXT_SQL_INFO.STATUS` polling target:

- Included: `URGENT`, `FAIL`, `READY`, `PENDING`, `SKIP`, `NULL`
- Excluded: `NA`

`SKIP` is treated as a retryable hold status. The scheduler can pick it up again when mapping readiness changes.
Use `NA` for SQL rows that must be excluded from conversion/test targets entirely.

### SQL append-only log

`NEXT_SQL_INFO.LOG` stores the latest summary for the row. Detailed generation/execution history is append-only in `NEXT_SQL_LOG`.

Create or update the table with:

```bash
python scripts/create_sql_log_table.py
```

`NEXT_SQL_LOG` stores generated intermediate retry SQL and execution/error status with `SPACE_NM`, `SQL_ID`, `SQL_KIND`, `SQL_CONTENT`, `STATUS`, `PROMPT_NAME`, `MODEL_NAME`, `BATCH_NO`, `CYCLE_NO`, `ELAPSED_SECONDS`, `ATTEMPT_NO`, `STAGE_NAME`, and `ERROR_MESSAGE`.

`NEXT_SQL_INFO.FORMATTED_SQL` stores the final SQL after TO-BE tuning passes and the indent formatter is applied.

`NEXT_SQL_INFO.TUNED_RESULT` stores the LLM's short natural-language summary of which tuning guidance was applied. `TUNED_SQL` stores only the SQL statement.

Create the column with:

```bash
python scripts/add_formatted_sql_column.py
python scripts/add_tuned_result_column.py
```

### SQL classification columns

`NEXT_SQL_INFO.SQL_LENGTH` and `NEXT_SQL_INFO.MAP_TYPE` are filled when a SQL conversion job starts.

Create the columns with:

```bash
python scripts/add_sql_info_classification_columns.py
```

- `SQL_LENGTH`: `SHORT` when `FR_SQL_TEXT` is 5000 chars or less and `EDIT_FR_SQL`, if present, is also 5000 chars or less. Otherwise `LONG`.
- `MAP_TYPE`: `COMPLEX` when any matched mapping row for the SQL target tables has `NEXT_MIG_INFO.MAP_TYPE = 'COMPLEX'`. Otherwise `SIMPLE` when all matched mappings are simple.

## Current SQL Conversion/Tuning workflow

Supervisor mode flags:

- `DB_MIGRATION_ONLY=true`: include DB Migration.
- `SQL_CONVERSION_ONLY=true`: include SQL Conversion.
- `SQL_TUNING_ONLY=true`: include SQL Tuning.
- If all three flags are false, the Supervisor runs all agents.
- If one or more flags are true, the Supervisor runs only the selected agents.
- Examples:
  - `DB_MIGRATION_ONLY=true`, `SQL_TUNING_ONLY=true`, `SQL_CONVERSION_ONLY=false`: run DB Migration and SQL Tuning only.
  - all three flags `true`: run all three agents.

Supervisor runtime behavior:

- The Supervisor graph handles one cycle at a time: `poll -> execute -> wait -> END`.
- `SupervisorAgent.run()` owns the long-running loop and invokes the graph again unless a stop signal is requested.
- `runtime/agent.pause` is respected before polling and while waiting between cycles.
- On `SIGINT`/`SIGTERM`, the current in-flight job is allowed to finish, then remaining jobs in the same cycle are skipped.

SQL Conversion polling uses `NEXT_SQL_INFO.STATUS`:

- Included: `URGENT`, `READY`, `FAIL`, `SKIP`, `PENDING`, `NULL`
- Excluded: `NA`
- Ordering: `URGENT` -> `READY` -> `FAIL` -> `SKIP` -> `PENDING` -> `NULL`, then `UPD_TS`, SQL length, `SPACE_NM`, `SQL_ID`.
- `SKIP` is retryable. `NA` is excluded from conversion/test targets.
- Rows that already have `STATUS='PASS'` and `TO_SQL_TEXT IS NOT NULL` are not conversion targets. Tuning is handled by the separate tuning queue.

SQL Tuning polling uses `NEXT_SQL_INFO.TUNED_TEST` while requiring `STATUS='PASS'` and `TO_SQL_TEXT IS NOT NULL`:

- Included: `URGENT`, `READY`, `FAIL`
- Excluded: `NULL`, `PASS`, `SKIP`, `NA`
- Ordering: `URGENT` -> `READY` -> `FAIL`, then `UPD_TS`, `SPACE_NM`, `SQL_ID`.
- `TUNED_TEST IS NULL` is not a tuning target. A SQL conversion success must explicitly set `TUNED_TEST='READY'` before the separate tuning queue can pick it up.

SQL Conversion SELECT flow:

```text
TO-BE SQL generation
  -> source/target validation
  -> if validation PASS: STATUS='PASS', TUNED_TEST='READY'
  -> if validation FAIL: retry or STATUS='FAIL'
```

SQL Conversion INSERT/UPDATE/DELETE flow:

```text
TO-BE SQL generation
  -> validation skip
  -> STATUS='PASS', TUNED_TEST='READY'
```

SQL Tuning SELECT flow:

```text
TUNED_TEST in ('URGENT', 'READY', 'FAIL') and STATUS='PASS'
  -> TO-BE tuning
  -> tuned SQL validation
  -> if tuned validation PASS: TUNED_TEST='PASS', rule HIT_CNT update, indent formatting
  -> if tuned validation FAIL: TUNED_TEST='FAIL'
```

SQL Tuning INSERT/UPDATE/DELETE flow:

```text
TUNED_TEST in ('URGENT', 'READY', 'FAIL') and STATUS='PASS'
  -> TO-BE tuning
  -> tuned SQL validation skip
  -> TUNED_TEST='SKIP'
  -> indent formatting
```

Dashboard rate calculations:

- General progress rate: `PASS / (all statuses except NA)`.
- General success rate: `PASS / (PASS + FAIL)`.
- Tuning progress rate: `(PASS + SKIP) / (all TUNED_TEST statuses except NA and NULL)`.
- Tuning success rate: `PASS / (PASS + FAIL)`. `SKIP` is excluded from both numerator and denominator because tuning SQL was generated but validation was intentionally skipped.

Tuning LLM output is required to be one JSON object:

```json
{
  "tuned_sql": "SELECT ...",
  "tuned_result": "적용한 튜닝 가이드 요약 한두 문장"
}
```

- `TUNED_SQL` stores only `tuned_sql`.
- `TUNED_RESULT` stores only `tuned_result`.
- If there is no applicable tuning guide, `TUNED_RESULT` is stored as `NO TUNING`.
- `FORMATTED_SQL` stores the final SQL after indent formatting.
- `NEXT_SQL_LOG` stores generation history separately as `TUNED_SQL`, `TUNED_RESULT`, `TUNED_TEST_SQL`, `FORMATTED_SQL`, and other SQL kinds.

Tuning RAG prompt input is intentionally compact. The prompt receives only:

- `source_sql`
- `guidance`
- `example_bad_sql`
- `example_tuned_sql`

`BLOCK_RAG_CONTENT` still stores the full retrieved RAG payload, including metadata such as search method, model, score, rule id, and block information.

`NEXT_SQL_RULES.HIT_CNT` is updated only after the tuned result reaches `TUNED_TEST='PASS'`. Non-SELECT rows with `TUNED_TEST='SKIP'` are formatted but do not increment rule hit counts. Within one tuning prompt, duplicate SEARCH rule ids are counted once.

Required optional migration scripts for current SQL output columns:

```bash
python scripts/create_sql_log_table.py
python scripts/add_sql_info_classification_columns.py
python scripts/add_formatted_sql_column.py
python scripts/add_tuned_result_column.py
```

## Correct SQL RAG hint

TO-BE, Bind, Test SQL 생성 단계는 과거에 사람이 고친 correct SQL을 hint로 받을 수 있습니다.

검색 대상은 `NEXT_SQL_INFO`입니다.

| 생성 단계 | correct_kind | 검색 대상 컬럼 | prompt 입력 |
| --- | --- | --- | --- |
| TO-BE SQL | `TOBE` | `TOBE_CORRECT_SQL` | `correct_sql_hint_json` |
| Bind SQL | `BIND` | `BIND_CORRECT_SQL` | `correct_sql_hint_json` |
| Test SQL | `TEST` | `TEST_CORRECT_SQL` | `correct_sql_hint_json` |

검색 방식:

- 현재 job의 `source_sql`을 query로 사용합니다.
- corpus row는 `EDIT_FR_SQL`이 있으면 `EDIT_FR_SQL`, 없으면 `FR_SQL_TEXT`를 embedding 기준 SQL로 사용합니다.
- correct SQL 컬럼이 비어 있는 row는 검색 대상에서 제외합니다.
- 현재 처리 중인 row는 검색 대상에서 제외합니다.
- FAISS vector search top k 기본값은 `2`입니다.
- embedding/FAISS 사용이 불가능하면 token fallback 검색을 사용합니다.

프롬프트에는 검색 메타데이터를 넣지 않습니다. LLM에는 최대 2개의 correct SQL 문자열 배열만 전달됩니다.

```json
[
  "SELECT ...",
  "SELECT ..."
]
```

환경 변수:

```env
CORRECT_SQL_HINT_TOP_K=2
CORRECT_SQL_HINT_CORPUS_LIMIT=2000
RAG_EMBED_BASE_URL=
RAG_EMBED_API_KEY=
RAG_EMBED_MODEL=BAAI/bge-m3
RAG_EMBED_TIMEOUT_SEC=30
```

## Bind SQL 설계

Bind SQL은 검증에 필요한 bind 후보 값을 DB에서 조회하는 SQL입니다.

현재 구조에서는 `build_bind_param_metadata()`나 `bind_target_hints_json`을 사용하지 않습니다. 코드에서는 bind parameter 존재 여부만 감지하고, 실제 어떤 컬럼을 어떤 alias로 뽑을지는 `bind_sql_prompt.json`이 LLM에게 판단시킵니다.

감지 대상:

- `#{param}`
- `${param}`
- `<foreach collection="ids">`의 `collection`
- `<if test="...">`의 parameter
- `<when test="...">`의 parameter

`<foreach>`에서 실제 외부 입력은 `item`/`index`가 아니라 `collection` 값입니다.

bind parameter가 없으면:

```text
bind_sql = ""
bind_set_for_db = None
bind_set_json_for_test = "[{}]"
```

즉 Bind SQL을 생성/실행하지 않고 바로 Test SQL 단계로 넘어갑니다.

## Test SQL 설계

Test SQL은 source SQL과 target SQL의 count를 bind case별로 비교합니다.

책임:

- source/target SQL의 MyBatis 바인딩 파라미터 태그를 `bind_set_json` 값으로 치환
- MyBatis 동적 태그를 bind case 기준으로 해석
- source/target SQL을 count query로 감싸서 `FROM_COUNT`, `TO_COUNT` 생성
- 이미 `SELECT COUNT(...)` 형태인 SQL은 다시 count wrapper로 감싸지 않음
- `ORDER BY` 제거
- `SYSDATE`, `CURRENT_DATE`, `SYSTIMESTAMP`, `TRUNC(SYSDATE)`, `ADD_MONTHS(SYSDATE, ...)`, `TO_DATE(...)`, `DATE 'YYYY-MM-DD'` 같은 날짜/기간 조건 제거

`mapping_schema_text`는 Test SQL prompt에 전달하지 않습니다. Test SQL 단계는 mapping rule로 의미를 재구성하지 않고 이미 생성된 source/target SQL을 검증 가능한 형태로 만드는 데 집중합니다.

## Retry 정책

SQL Conversion은 최대 3번 시도합니다.

```text
RETRY_CONTEXT: attempt=2/3; FINAL_RETRY_MODE=OFF; last_error=...
RETRY_CONTEXT: attempt=3/3; FINAL_RETRY_MODE=ON; last_error=...
```

마지막 시도인 `attempt=3/3`에서는 `FINAL_RETRY_MODE=ON`이 됩니다.

- Bind SQL: `bind_sql_final_retry_prompt.json` 사용
- Test SQL: `test_sql_final_retry_prompt.json` 사용

final retry에서는 동적 태그 조건을 만족시키기 위한 join, EXISTS, CASE, subquery를 만들지 않습니다. 동적 태그와 그 내부 SQL fragment가 제거된 것처럼 처리합니다.

## Prompt 입력

### TO-BE SQL Prompt

파일:

```text
server/config/prompts/tobe_sql_prompt.json
```

입력:

- `from_sql`
- `mapping_schema_text`
- `target_schema`
- `correct_sql_hint_json`
- `last_error`

### Bind SQL Prompt

파일:

```text
server/config/prompts/bind_sql_prompt.json
server/config/prompts/bind_sql_final_retry_prompt.json
```

입력:

- `from_sql`
- `from_schema`
- `correct_sql_hint_json`
- `last_error`

전달하지 않는 값:

- `bind_param_metadata_json`
- `bind_target_hints_json`
- `tobe_sql`
- `mapping_schema_text`

### Test SQL Prompt

파일:

```text
server/config/prompts/test_sql_prompt.json
server/config/prompts/test_sql_final_retry_prompt.json
```

입력:

- `source_sql`
- `target_sql`
- `source_schema`
- `target_schema`
- `bind_set_json`
- `comparison_mode`
- `correct_sql_hint_json`
- `last_error`

## XML Parser

MyBatis mapper XML을 `NEXT_SQL_INFO`로 적재하는 명령입니다.

```bash
python -m server.services.sql.xml_parser_service all
python -m server.services.sql.xml_parser_service stage1 --source-dir C:\path\to\mapper --output-dir C:\path\to\xml-json
python -m server.services.sql.xml_parser_service stage2 --output-dir C:\path\to\xml-json
python -m server.services.sql.xml_parser_service stage3
python -m server.services.sql.xml_parser_service stage4
```

환경 변수:

```env
MAPPER_XML_SOURCE_DIR=
XML_PARSER_DATA_DIR=server/services/sql/DATA
ACTIVE_SQL_ID_TABLE=
ACTIVE_SQL_ID_COLUMN=SQL_ID
```

## 환경 변수

`.env.example`을 복사해서 `.env`를 만든 뒤 실행 환경에 맞게 채웁니다.

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
ORACLE_SCHEMA_SRC=
ORACLE_SCHEMA_TGT=

MAPPING_RULE_TABLE=NEXT_MIG_INFO
MAPPING_RULE_DETAIL_TABLE=NEXT_MIG_INFO_DTL
RESULT_TABLE=NEXT_SQL_INFO

LLM_PROVIDER=openai
LLM_API_KEY=
LLM_MODEL=GLM-5.1
LLM_BASE_URL=
LLM_MAX_TOKENS=4096
LLM_FALLBACK_MODELS=GLM-5.1,Qwen3.6-35B-A3B,Kimi-K2.5

RAG_EMBED_BASE_URL=
RAG_EMBED_API_KEY=
RAG_EMBED_MODEL=BAAI/bge-m3
RAG_EMBED_TIMEOUT_SEC=30
TOBE_RULE_CATALOG_PATH=server/services/sql/data/rag/tobe_rule_catalog.json
UNIVERSAL_TUNING_RULES_PATH=server/services/sql/data/rules/universal_tuning_rules.json
TOBE_SQL_TUNING_TOP_K=3
TOBE_SQL_TUNING_MAX_ITERATIONS=1

JOB_MAX_BATCH_COUNT=30

CORRECT_SQL_HINT_TOP_K=2
CORRECT_SQL_HINT_CORPUS_LIMIT=2000

MAPPER_XML_SOURCE_DIR=
XML_PARSER_DATA_DIR=server/services/sql/DATA
```

## 실행

의존성 설치:

```bash
pip install -r requirements.txt
```

초기 DB 준비:

```bash
python scripts/init_db.py
```

Agent 실행:

```bash
python main.py
```

Streamlit dashboard:

```bash
streamlit run app/app.py
```

## 검증 체크리스트

Prompt JSON 확인:

```bash
python -m json.tool server/config/prompts/bind_sql_prompt.json
python -m json.tool server/config/prompts/test_sql_prompt.json
python -m json.tool server/config/prompts/tobe_sql_prompt.json
```

Python 문법 확인:

```bash
python -m compileall server/services/sql
```

## 인코딩 기준

- README와 prompt JSON은 UTF-8 no BOM을 권장합니다.
- prompt loader는 `utf-8-sig`로 읽기 때문에 BOM이 있어도 로드는 가능하지만, 운영 중 `unexpected UTF-8 BOM` 혼선을 막기 위해 no BOM으로 저장하는 것이 좋습니다.
- 한글이 깨져 보이면 한글 자체 문제가 아니라 저장/표시 과정에서 잘못된 인코딩으로 변환된 것입니다.
