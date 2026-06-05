# 20260601 Migration Pipeline

Oracle/MyBatis 기반 DB migration, SQL conversion, SQL tuning, SQL formatting을 자동화하는 Streamlit 운영 콘솔과 Python batch agent입니다.

현재 코드는 `NEXT_MIG_INFO`, `NEXT_MIG_INFO_DTL`, `NEXT_SQL_COMPLEX_MAP`, `NEXT_SQL_INFO`, `NEXT_SQL_LOG`, `NEXT_SQL_RULES`를 중심으로 동작합니다.

## 주요 기능

- DB Migration: `NEXT_MIG_INFO` 기준 migration SQL 생성, 실행, 검증
- SQL Conversion: MyBatis SQL을 TO-BE SQL로 변환하고 source/target row count 검증
- SQL Tuning: 검증된 TO-BE SQL에 tuning rule RAG를 적용하고 tuned SQL 검증
- SQL Formatting: `sql_indent_format_prompt.json` 기준으로 최종 SQL을 정렬해 `FORMATTED_SQL` 저장
- Correct SQL 반영: `TOBE_CORRECT_SQL`, `BIND_CORRECT_SQL`, `TEST_CORRECT_SQL`이 있으면 해당 stage의 LLM 생성을 건너뛰고 correct SQL을 직접 사용
- Complex mapping 지원: `NEXT_SQL_COMPLEX_MAP`에 target table 룰이 있으면 `tobe_sql_complex_prompt.json` 사용
- LLM fallback: 치명적인 model access 오류 발생 시 설정된 fallback model 순서대로 재시도
- XML Export: namespace별/전체 MyBatis XML 다운로드, `FORMATTED_SQL` 기준 생성
- Streamlit Dashboard: agent 시작/중지/일시정지, 상태 카드, chatbot, monitor, settings, tuning rule manager 제공

## 실행 흐름

```text
python main.py
  -> Supervisor Agent
      -> DB Migration Agent
      -> SQL Conversion Agent
      -> SQL Tuning Agent
      -> SQL Formatting Agent
```

Supervisor는 한 cycle마다 대상 job을 조회하고 실행한 뒤 종료됩니다. `SupervisorAgent.run()`이 이 graph를 반복 호출하므로, 종료 신호가 없으면 다음 cycle로 계속 진행합니다.

현재 cycle당 agent별 실행 대상 수는 `server/agents/supervisor/graph.py`의 `JOB_BATCH_SIZE = 5`입니다. SQL row의 재처리 상한은 `.env`의 `JOB_MAX_BATCH_COUNT`로 관리합니다.

## Agent 선택 모드

`.env` 또는 Streamlit sidebar에서 아래 네 플래그를 조합해 실행 대상을 고를 수 있습니다.

```env
DB_MIGRATION_ONLY=false
SQL_CONVERSION_ONLY=true
SQL_TUNING_ONLY=false
SQL_FORMATTING_ONLY=false
```

- 네 값이 모두 `false`: 전체 agent 실행
- 하나 이상 `true`: 선택된 agent만 실행
- 예: `SQL_TUNING_ONLY=true`, `SQL_FORMATTING_ONLY=true`이면 tuning과 formatting 대상만 실행
- 예: `SQL_FORMATTING_ONLY=true`만 켜면 이미 tuning이 끝났지만 `FORMATTED_SQL IS NULL`인 건만 일괄 포맷팅

## SQL Conversion 흐름

```text
NEXT_SQL_INFO polling
  -> TO-BE SQL 생성
  -> Bind parameter 감지
  -> Bind SQL 생성/실행
  -> BIND_SET 생성
  -> Test SQL 생성/실행
  -> STATUS PASS/FAIL/NA 갱신
```

Conversion polling 대상:

- 포함: `URGENT`, `READY`, `FAIL`, `PENDING`, `SKIP`, `NULL`
- 제외: `NA`
- 이미 `STATUS='PASS'`이고 `TO_SQL_TEXT`가 있는 row는 conversion 대상에서 제외

Conversion 성공 시:

- `STATUS='PASS'`
- `TUNED_TEST='READY'`
- SELECT가 아닌 SQL도 conversion validation 이후 tuning queue로 넘깁니다. 단, tuning validation은 non-SELECT에서 `SKIP` 처리됩니다.

## Correct SQL stage bypass

`NEXT_SQL_INFO`에 correct SQL 값이 있으면 해당 stage의 LLM 생성을 건너뜁니다.

| 컬럼 | 적용 stage | 동작 |
| --- | --- | --- |
| `TOBE_CORRECT_SQL` | TO-BE SQL 생성 | `TO_SQL_TEXT` 생성 대신 correct SQL 사용 |
| `BIND_CORRECT_SQL` | Bind SQL 생성 | bind SQL LLM 생성 대신 correct SQL 사용 후 bind set 추출 계속 진행 |
| `TEST_CORRECT_SQL` | Test SQL 생성 | test SQL LLM 생성 대신 correct SQL 사용 후 실행/판정 계속 진행 |

중요한 점은 correct SQL은 해당 stage의 생성만 대체한다는 것입니다. 예를 들어 `BIND_CORRECT_SQL`을 사용해도 bind query 실행, bind set 생성, test SQL 생성/검증은 계속 진행됩니다.

## Complex mapping

SQL conversion 단계에서 `NEXT_SQL_INFO.TARGET_TABLE`과 일치하는 활성 complex mapping rule이 `NEXT_SQL_COMPLEX_MAP`에 있으면 complex flow로 분류합니다.

- complex 판정: `NEXT_SQL_COMPLEX_MAP.USE_YN='Y' AND FR_TABLE=TARGET_TABLE`
- complex table이 없으면 simple fallback하지 않고 명시적으로 오류를 냅니다.
- complex flow에서는 `server/config/prompts/tobe_sql_complex_prompt.json` 사용
- target table 목록 중 complex table에 있는 것은 `NEXT_SQL_COMPLEX_MAP`에서 가져옵니다.
- target table 목록 중 complex table에 없는 것은 기존처럼 `NEXT_MIG_INFO` / `NEXT_MIG_INFO_DTL`에서 가져옵니다.
- complex flow의 `mapping_schema_text`는 `[SIMPLE_MAPPING_RULES]`, `[COMPLEX_GENERAL_RULES]`, `[COMPLEX_SEARCH_RULES_TOP_K]`를 함께 전달합니다.
- simple flow는 기존처럼 `NEXT_MIG_INFO` / `NEXT_MIG_INFO_DTL`의 `FROM_TABLE / FROM_COLUMN / TO_TABLE / TO_COLUMN` 반복 구조만 사용합니다.
- complex flow는 `NEXT_SQL_COMPLEX_MAP`의 `GENERAL` rule 전체와 `SEARCH` rule top-k를 `mapping_schema_text`에 전달합니다.
- LLM prompt에는 `MAP_ID`, `MAP_KIND`, 검색 점수 같은 내부 메타데이터를 전달하지 않습니다.
- `SEARCH` rule은 `EDIT_FR_SQL`이 있으면 해당 SQL, 없으면 `FR_SQL_TEXT`를 query로 사용하고 `FR_COL`만 embedding 대상으로 검색
- `COMPLEX_MAP_SEARCH_TOP_K`로 SEARCH rule 검색 건수를 조정합니다.
- complex prompt에는 `MAP_ID`, `MAP_KIND`, 검색 점수, `DESCRIPTION` 같은 내부/보조 메타데이터를 전달하지 않습니다.
- complex prompt에서는 `correct_sql_hint_json`을 제외합니다.

DDL 생성:

```bash
python scripts/create_sql_complex_map_table.py
```

## SQL Tuning 흐름

Tuning polling 대상:

- 조건: `STATUS='PASS'`, `TO_SQL_TEXT IS NOT NULL`
- 포함: `TUNED_TEST IN ('URGENT', 'READY', 'FAIL')`
- 제외: `NULL`, `PASS`, `SKIP`, `NA`

SELECT:

```text
TO-BE SQL
  -> tuning rule RAG 조회
  -> TUNED_SQL / TUNED_RESULT 생성
  -> tuned test SQL 생성/실행
  -> TUNED_TEST PASS/FAIL
  -> PASS이면 rule HIT_CNT 증가
  -> PASS이면 FORMATTED_SQL 생성
```

INSERT/UPDATE/DELETE:

```text
TO-BE SQL
  -> tuning rule RAG 조회
  -> TUNED_SQL / TUNED_RESULT 생성
  -> tuned test는 SKIP
  -> FORMATTED_SQL 생성
```

튜닝 agent 내부에서는 기존처럼 각 job이 `TUNED_TEST='PASS'` 또는 `TUNED_TEST='SKIP'`이 되면 바로 `FORMATTED_SQL`까지 생성합니다.

별도 SQL Formatting agent는 보정용입니다. `TUNED_TEST IN ('PASS', 'SKIP')`이지만 `FORMATTED_SQL IS NULL`인 row만 다시 포맷팅합니다.

튜닝 결과 컬럼:

- `TUNED_SQL`: 최종 튜닝 SQL
- `TUNED_RESULT`: 적용된 튜닝 가이드 요약
- 적용할 가이드가 없으면 `TUNED_RESULT='NO TUNING'`
- `BLOCK_RAG_CONTENT`: 화면에서 접고 펼칠 수 있는 RAG block 원문
- `FORMATTED_SQL`: XML export에 사용되는 최종 정렬 SQL

## LLM fallback

기본 모델은 `.env`의 `LLM_MODEL`입니다.

```env
LLM_MODEL=GLM-5.1
LLM_FALLBACK_MODELS=GLM-5.1,Qwen3.6-35B-A3B,Kimi-K2.5
```

`401 team not allowed to access model`, `model not allow`, `model not found` 같은 치명적인 model access 오류가 발생하면 `LLM_FALLBACK_MODELS` 순서대로 다음 모델을 시도합니다.

Timeout, loading delay, rate limit은 fallback 조건이 아닙니다.

한 cycle 중 fallback으로 성공한 모델은 그 cycle 동안 유지됩니다. 새 supervisor cycle이 시작되면 다시 기본 모델부터 시도합니다.

## Streamlit 화면

실행:

```bash
streamlit run app/app.py
```

주요 화면:

- Dashboard: 전체 현황, agent control, chatbot
- Mig Agent Monitor: migration job 모니터링
- SQL Agent Monitor: ASIS SQL / TOBE SQL 중심 확인, job detail에서 원하는 두 컬럼 비교
- Tuning Agent Monitor: TUNED_RESULT, tuning 전/후 비교, `BLOCK_RAG_CONTENT` 접기/펼치기
- Job Detail: SQL job별 상세 컬럼 선택 비교
- Tuning Rule Manager: tuning rule 관리
- XML Export: namespace별 XML 다운로드와 전체 일괄 다운로드
- System Health: DB/LLM/runtime 상태 확인
- Settings: LLM 설정과 fallback model 목록 관리

Dashboard 표시 기준:

- `URGENT`, `READY`는 진행 중(`RUNNING`)으로 합산 표시
- `NA`는 Dashboard status 카드와 총계에서 표시하지 않음
- SQL 진척률은 `PASS / 전체 대상`
- 성공률은 `PASS / 성공·실패 판정 대상`
- Tuning은 `SKIP`을 성공률 분모에서 제외

Chatbot UI는 질문을 입력하면 사용자 메시지가 먼저 채팅 기록에 남고, 같은 기록 영역에 assistant의 `입력중...` 메시지가 표시된 뒤 LLM 응답으로 교체됩니다.

## XML Export

XML export는 `FORMATTED_SQL` 기준으로 MyBatis mapper XML을 생성합니다.

- namespace별 다운로드 가능
- 전체 일괄 다운로드 가능
- namespace 목록에는 PASS/FAIL 건수가 함께 표시
- namespace에 fail이 있으면 해당 namespace 다운로드는 `not available`로 비활성화
- 전체 일괄 다운로드는 pass이고 `FORMATTED_SQL`이 있는 SQL만 포함

## 주요 디렉터리

```text
app/
  app.py
  pages/
  utils/

server/
  agents/
    migration/
    sql_conversion/
    sql_tuning/
    sql_formatting/
    supervisor/
  config/
    prompts/
  core/
  repositories/
  services/
  tools/

scripts/
```

## 주요 prompt

```text
server/config/prompts/tobe_sql_prompt.json
server/config/prompts/tobe_sql_complex_prompt.json
server/config/prompts/bind_sql_prompt.json
server/config/prompts/bind_sql_final_retry_prompt.json
server/config/prompts/test_sql_prompt.json
server/config/prompts/test_sql_final_retry_prompt.json
server/config/prompts/tobe_sql_tuning_prompt.json
server/config/prompts/tuned_test_sql_prompt.json
server/config/prompts/sql_indent_format_prompt.json
```

## DB 보정 스크립트

현재 기능 사용 전 필요한 컬럼/테이블 보정:

```bash
python scripts/create_sql_log_table.py
python scripts/create_sql_rules_table.py
python scripts/create_sql_complex_map_table.py
python scripts/add_sql_info_classification_columns.py
python scripts/add_formatted_sql_column.py
python scripts/add_tuned_result_column.py
```

초기 DB 준비:

```bash
python scripts/init_db.py
```

## 환경 설정

`.env.example`을 복사해 `.env`를 만들고 값을 채웁니다.

```powershell
Copy-Item .env.example .env
```

핵심 값:

```env
DB_USER=
DB_PASS=
DB_HOST=localhost
DB_PORT=1521
DB_SID=xe

ORACLE_SCHEMA=
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
RAG_EMBED_MODEL=bge-m3
RAG_EMBED_TIMEOUT_SEC=30
COMPLEX_MAP_SEARCH_TOP_K=3
TOBE_SQL_TUNING_TOP_K=3
TOBE_SQL_TUNING_MAX_ITERATIONS=1

JOB_MAX_BATCH_COUNT=30
SUPERVISOR_RECURSION_LIMIT=10000

DB_MIGRATION_ONLY=false
SQL_CONVERSION_ONLY=true
SQL_TUNING_ONLY=false
SQL_FORMATTING_ONLY=false
```

## 실행

의존성 설치:

```bash
pip install -r requirements.txt
```

Batch agent 실행:

```bash
python main.py
```

Streamlit UI 실행:

```bash
streamlit run app/app.py
```

MyBatis XML parser:

```bash
python -m server.services.sql.xml_parser_service all
```

## 검증

Python 문법 확인:

```bash
python -m compileall app server
```

Prompt JSON 확인:

```bash
python -m json.tool server/config/prompts/tobe_sql_prompt.json
python -m json.tool server/config/prompts/tobe_sql_complex_prompt.json
python -m json.tool server/config/prompts/sql_indent_format_prompt.json
```

## 운영 메모

- README와 prompt JSON은 UTF-8 no BOM 저장을 권장합니다.
- `NEXT_SQL_INFO.LOG`는 row의 최신 요약이고, 상세 이력은 `NEXT_SQL_LOG`에 append-only로 저장됩니다.
- `FORMATTED_SQL`은 XML export의 기준 SQL입니다.
- `NA`는 conversion/test 대상에서 제외할 때 사용합니다.
- `SKIP`은 재시도 가능한 보류 또는 의도적 검증 생략 상태로 사용합니다.
