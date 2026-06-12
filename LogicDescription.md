# Logic Description

이 문서는 `python main.py`와 Streamlit UI에서 실제로 동작하는 agent, tool, repository, LLM 호출 흐름을 순서 중심으로 정리한 로직 설명서입니다.

기준 코드는 현재 `20260601` 디렉터리의 소스입니다.

## 1. 전체 실행 구조

```text
main.py
  -> SupervisorAgent.run()
      -> build_supervisor_graph()
          -> poll
          -> execute
          -> wait
          -> END
      -> while not stop_requested:
          graph.invoke(state) 반복
```

핵심 원칙:

- Supervisor graph 자체는 한 cycle만 처리합니다.
- graph가 `END`에 도달해도 process가 끝나는 것이 아니라 `SupervisorAgent.run()`의 while loop가 다음 cycle을 다시 호출합니다.
- 실제 종료는 `_stop_event=True`일 때만 발생합니다.
- `_stop_event=True`는 `SIGINT`, `SIGTERM`, UI stop, 내부 `request_stop()` 호출 등으로 설정됩니다.
- cycle 시작마다 LLM active model fallback 상태를 reset합니다.

## 2. Supervisor Agent 상세 흐름

파일:

```text
server/agents/supervisor/agent.py
server/agents/supervisor/graph.py
server/tools/context.py
```

### 2.1 SupervisorAgent 초기화

```text
SupervisorAgent.__init__()
  -> MigrationOrchestrator 생성
  -> SqlConversionAgent 생성
  -> SqlTuningAgent 생성
  -> SqlFormattingAgent 생성
  -> build_supervisor_graph(...) 호출
```

graph에 주입되는 callback:

| callback | 실제 함수 |
| --- | --- |
| `get_migration_jobs` | `server.repositories.migration.repository.get_pending_jobs` |
| `get_sql_jobs` | `server.repositories.sql.result_repository.get_pending_jobs` |
| `get_tuning_jobs` | `server.repositories.sql.result_repository.get_tuning_jobs` |
| `get_formatting_jobs` | `server.repositories.sql.result_repository.get_formatting_jobs` |
| `mig_increment_batch` | `migration.repository.increment_batch_count` |
| `sql_increment_batch` | `sql.result_repository.increment_batch_count` |
| `mig_process_job` | `MigrationOrchestrator.process_job` |
| `sql_process_job` | `SqlConversionAgent.process_job` |
| `tune_process_job` | `SqlTuningAgent.process_job` |
| `format_process_job` | `SqlFormattingAgent.process_job` |

### 2.2 Agent 선택 플래그

환경 변수:

```env
DB_MIGRATION_ONLY=false
SQL_CONVERSION_ONLY=true
SQL_TUNING_ONLY=false
SQL_FORMATTING_ONLY=false
```

결정 로직:

```text
HAS_AGENT_SELECTION =
  DB_MIGRATION_ONLY
  OR SQL_CONVERSION_ONLY
  OR SQL_TUNING_ONLY
  OR SQL_FORMATTING_ONLY

RUN_MIGRATION      = DB_MIGRATION_ONLY      OR NOT HAS_AGENT_SELECTION
RUN_SQL_CONVERSION = SQL_CONVERSION_ONLY    OR NOT HAS_AGENT_SELECTION
RUN_SQL_TUNING     = SQL_TUNING_ONLY        OR NOT HAS_AGENT_SELECTION
RUN_SQL_FORMATTING = SQL_FORMATTING_ONLY    OR NOT HAS_AGENT_SELECTION
```

해석:

- 네 플래그가 모두 `false`: 전체 agent 실행
- 하나라도 `true`: true인 agent만 실행
- 세 개 이상 true여도 우선순위 없음. 선택된 것 모두 실행

### 2.3 Supervisor.run()

```text
SupervisorAgent.run()
  -> signal handler 등록
  -> batch_no = 현재시각 YYYYMMDDHHMMSS
  -> start_batch_metrics(batch_no)
  -> state = {"messages": [], "cycle": 0, "stop_requested": False}
  -> while not is_stop_requested():
        state = graph.invoke(state)
        if state.stop_requested:
            break
  -> finally:
        정상 종료 로그
```

중요:

- `graph.invoke()`가 끝났다고 process 종료가 아닙니다.
- graph는 매 cycle `poll -> execute -> wait -> END`만 수행합니다.
- 바깥 while이 다음 cycle을 다시 시작합니다.

## 3. Supervisor Graph 상세 흐름

### 3.1 poll node

```text
poll_node(state)
  -> stop_event 확인
      Y: stop_requested=True 반환
  -> runtime/agent.pause 확인
      pause 파일 존재:
        stop_event가 set될 때까지 대기
        pause 해제되면 계속 진행
  -> cycle = state.cycle + 1
  -> reset_active_model()
      이전 cycle에서 fallback으로 잡힌 active model 제거
  -> start_cycle_metrics(cycle)
  -> agent별 job 조회
      RUN_MIGRATION      -> get_migration_jobs()
      RUN_SQL_CONVERSION -> get_sql_jobs()
      RUN_SQL_TUNING     -> get_tuning_jobs()
      RUN_SQL_FORMATTING -> get_formatting_jobs()
  -> registry 초기화
      mig_registry.clear()
      sql_registry.clear()
      tuning_registry.clear()
      formatting_registry.clear()
  -> 각 job list에서 JOB_BATCH_SIZE=5개까지만 registry에 저장
  -> 대기/실행 대상 건수 로그
  -> {"cycle": cycle, "stop_requested": False}
```

registry key:

| registry | key |
| --- | --- |
| `mig_registry` | `job.map_id` |
| `sql_registry` | `job.row_id` |
| `tuning_registry` | `job.row_id` |
| `formatting_registry` | `job.row_id` |

### 3.2 execute node

```text
execute_node(state)
  -> 모든 registry가 비어 있으면 wait로 이동
  -> RUN_MIGRATION이면:
       for job in mig_registry:
         stop_event 확인
         retry_count >= 3이면 skip
         run_data_migration.invoke({"map_id": job.map_id})
  -> RUN_SQL_CONVERSION이면:
       for job in sql_registry:
         stop_event 확인
         run_sql_conversion.invoke({"row_id": job.row_id})
  -> RUN_SQL_TUNING이면:
       tuning row_id 목록 구성
       run_sql_tuning.invoke({"row_ids": tuning_row_ids})
  -> RUN_SQL_FORMATTING이면:
       formatting row_id 목록 구성
       run_sql_formatting.invoke({"row_ids": formatting_row_ids})
  -> stop_requested 반환
```

실행 순서:

```text
1. DB Migration
2. SQL Conversion
3. SQL Tuning
4. SQL Formatting
```

### 3.3 wait node

```text
wait_node()
  -> finish_cycle_metrics()
      AG_AGENT_RUN_METRICS 저장
  -> pause 파일 확인
  -> POLL_INTERVAL_SEC=5초 대기
      stop_event 또는 pause 파일 생기면 즉시 break
  -> stop_requested 반환
```

## 4. Tool Wrapper 상세

파일:

```text
server/tools/migration.py
server/tools/sql_conversion.py
server/tools/sql_tuning.py
server/tools/sql_formatting.py
server/tools/context.py
```

### 4.1 공통 구조

모든 tool은 다음 패턴을 가집니다.

```text
tool 호출
  -> registry에서 job 조회
  -> batch count 증가
  -> 실제 agent callback 실행
  -> record_agent_run(agent_name, elapsed, final_status)
  -> 성공/실패 로그 반환
```

metrics 분류:

```text
status in ("SUCCESS", "PASS", "PASS_NON_SELECT") -> success_count
status in ("SKIP", "NA")      -> skip_count
그 외                         -> fail_count
```

### 4.2 run_data_migration

```text
run_data_migration(map_id)
  -> mig_registry[map_id] 조회
  -> 없으면 ERROR 반환
  -> mig_inc(map_id)
      NEXT_MIG_INFO.BATCH_CNT + 1
  -> mig_proc(job)
      MigrationOrchestrator.process_job(job)
  -> record_agent_run("DB_MIGRATION", elapsed, final_status)
```

### 4.3 run_sql_conversion

```text
run_sql_conversion(row_id)
  -> sql_registry[row_id] 조회
  -> 없으면 ERROR 반환
  -> sql_inc(row_id)
      NEXT_SQL_INFO.BATCH_CNT + 1
  -> sql_proc(job)
      SqlConversionAgent.process_job(job)
  -> record_agent_run("SQL_MIGRATION", elapsed, final_status)
```

### 4.4 run_sql_tuning

```text
run_sql_tuning(row_ids)
  -> row_ids 반복
  -> tuning_registry[row_id] 조회
  -> sql_inc(row_id)
  -> tune_proc(job)
      SqlTuningAgent.process_job(job)
  -> record_agent_run("SQL_TUNING", elapsed, final_status)
```

### 4.5 run_sql_formatting

```text
run_sql_formatting(row_ids)
  -> row_ids 반복
  -> formatting_registry[row_id] 조회
  -> sql_inc(row_id)
  -> format_proc(job)
      SqlFormattingAgent.process_job(job)
  -> record_agent_run("SQL_FORMATTING", elapsed, final_status)
```

## 5. Repository Polling Logic

### 5.1 Migration job polling

파일:

```text
server/repositories/migration/repository.py
```

```text
get_pending_jobs()
  -> NEXT_MIG_INFO R
  -> LEFT JOIN NEXT_MIG_INFO_DTL D
  -> WHERE R.USE_YN = 'Y'
     AND R.TARGET_YN IS NOT NULL
  -> ORDER BY R.PRIORITY ASC, D.FR_COL ASC
  -> MAP_ID 단위 MappingRule로 묶음
  -> detail row는 MappingRule.details에 append
```

Migration job 조회 결과에는 아래 값이 포함됩니다.

- `MAP_ID`
- `MAP_TYPE`
- `FR_TABLE`
- `TO_TABLE`
- `USE_YN`
- `TARGET_YN`
- `PRIORITY`
- `MIG_SQL`
- `VERIFY_SQL`
- `STATUS`
- `CORRECT_SQL`
- `USER_EDITED`
- `BATCH_CNT`
- `ELAPSED_SECONDS`
- `RETRY_COUNT`
- detail: `MAP_DTL`, `FR_COL`, `TO_COL`

### 5.2 SQL Conversion job polling

파일:

```text
server/repositories/sql/result_repository.py
```

```text
get_pending_jobs()
  -> RESULT_TABLE 기본값 NEXT_SQL_INFO
  -> STATUS 조건:
       UPPER(TRIM(STATUS)) IN ('URGENT', 'FAIL', 'READY', 'PENDING')
       OR STATUS IS NULL
  -> TO_SQL_TEXT 조건:
       TO_SQL_TEXT IS NULL OR UPPER(TRIM(STATUS)) <> 'PASS'
  -> BATCH_CNT 조건:
       NVL(BATCH_CNT, 0) < JOB_MAX_BATCH_COUNT
       단, BATCH_CNT 컬럼 없거나 JOB_MAX_BATCH_COUNT <= 0이면 제외
  -> ORDER BY:
       URGENT
       READY
       FAIL
       PENDING
       NULL
       UPD_TS NULLS FIRST
       SQL 길이 ASC
       SPACE_NM
       SQL_ID
```

SQL conversion job 조회 시 포함되는 주요 컬럼:

- `ROWIDTOCHAR(ROWID)` as `row_id`
- `TAG_KIND`
- `SPACE_NM`
- `SQL_ID`
- `FR_SQL_TEXT`
- `TARGET_TABLE`
- `EDIT_FR_SQL`
- `TO_SQL_TEXT`
- `TUNED_SQL`
- `TUNED_TEST`
- `BIND_SQL`
- `BIND_SET`
- `TEST_SQL`
- `STATUS`
- `LOG`
- `EDITED_YN`
- `FR_BINDTUNED_SQL`
- `TOBE_CORRECT_SQL`
- `BIND_CORRECT_SQL`
- `TEST_CORRECT_SQL`
- `SQL_LENGTH`
- `MAP_TYPE`
- `FORMATTED_SQL`
- `TUNED_RESULT`

### 5.3 SQL Tuning job polling

```text
get_tuning_jobs()
  -> TUNED_TEST 컬럼 없으면 []
  -> WHERE UPPER(TRIM(TUNED_TEST)) IN ('URGENT', 'READY', 'FAIL')
     AND TO_SQL_TEXT IS NOT NULL
     AND UPPER(TRIM(STATUS)) = 'PASS'
     AND NVL(BATCH_CNT, 0) < JOB_MAX_BATCH_COUNT
  -> ORDER BY:
       TUNED_TEST URGENT
       TUNED_TEST READY
       TUNED_TEST FAIL
       UPD_TS NULLS FIRST
       SPACE_NM
       SQL_ID
```

즉 SQL conversion이 성공해 `STATUS='PASS'`, `TUNED_TEST='READY'`가 된 row만 tuning queue에 올라옵니다.

### 5.4 SQL Formatting job polling

```text
get_formatting_jobs()
  -> FORMATTED_SQL 또는 TUNED_TEST 컬럼 없으면 []
  -> WHERE UPPER(TRIM(TUNED_TEST)) IN ('PASS', 'PASS_NON_SELECT')
     AND (FORMATTED_SQL IS NULL OR FORMATTED_SQL is empty CLOB/blank)
     AND NVL(BATCH_CNT, 0) < JOB_MAX_BATCH_COUNT
  -> ORDER BY:
       UPD_TS NULLS FIRST
       SPACE_NM
       SQL_ID
```

이 queue는 보정용입니다.

When tuning agent creates FORMATTED_SQL after PASS or PASS_NON_SELECT, the row does not enter the formatting queue.

## 6. DB Migration Agent 상세 흐름

파일:

```text
server/agents/migration/orchestrator.py
server/agents/migration/graph.py
server/agents/migration/executor.py
server/agents/migration/verifier.py
```

### 6.1 MigrationOrchestrator.process_job

```text
process_job(NEXT_SQL_INFO)
  -> initial_state 구성
      next_sql_info
      source_ddl=None
      target_ddl=None
      last_error=None
      last_sql=None
      db_attempts=1
      max_attempts=3
      llm_retry_count=0
      current_ddl_sql=None
      current_migration_sql=None
      current_v_sql=None
      error_type=None
      status="RUNNING"
      elapsed_time=0
      job_start_time=time.time()
  -> migration_graph.invoke(initial_state)
  -> final_state.status 반환
  -> 예외 발생:
       update_job_status(map_id, "FAIL")
       return "FAIL"
```

### 6.2 Migration graph

```text
fetch_ddl
  -> check_dependency
      -> generate
          -> execute
              -> verify
                  -> finalize
```

retry가 있으면:

```text
generate LLM_RETRY
  -> llm_retry_wait
  -> generate

execute BIZ_RETRY
  -> biz_retry_prepare
  -> generate

verify BIZ_RETRY
  -> biz_retry_prepare
  -> generate
```

### 6.3 fetch_ddl_node

```text
fetch_ddl_node(state)
  -> job.fr_table에서 실제 table명 추출
      JOIN, ON 기준으로 분해
  -> 각 source table에 qualify_fr_table 적용
  -> fetch_table_ddl(source_table)
  -> job.to_table에 qualify_to_table 적용
  -> fetch_table_ddl(target_table)
  -> source_ddl, target_ddl 반환
```

### 6.4 check_dependency_node

```text
check_dependency_node(state)
  -> check_dependencies(map_id, to_table, priority)
      같은 TO_TABLE 중 priority가 더 낮은 선행 job 조회
      선행 job이 없으면 READY
      선행 job 중 STATUS != PASS가 있으면 해당 status 반환
  -> dep_status != READY:
       status="SKIP"
       error_type="DEPENDENCY_FAIL"
       last_error="선행 작업 상태: ..."
  -> dep_status == READY:
       error_type=None
```

### 6.5 generate_sql_node

```text
generate_sql_node(state)
  -> job.retry_count = db_attempts - 1
  -> is_append = not is_first_job_for_target(map_id, to_table, priority)
  -> generate_sqls(job, last_error, last_sql, source_ddl, target_ddl, is_append)
       LLM으로 ddl_sql, migration_sql, verification_sql 생성
  -> log_generated_sql(map_id, migration_sql, v_sql)
  -> current_ddl_sql, current_migration_sql, current_v_sql 저장
```

LLM fatal 예외:

- `LLMAuthenticationError`
- `LLMTokenLimitError`
- `LLMInvalidRequestError`

위 예외는 `BatchAbortError`로 전환되어 전체 migration batch 중단을 요청합니다.

### 6.6 execute_sql_node

```text
execute_sql_node(state)
  -> execute_migration(current_migration_sql)
      split_sql_script()
      clean_sql_statement()
      SQL 또는 PL/SQL 실행
      ORA-00955는 이미 존재하는 객체로 보고 skip
      전체 commit
  -> 성공:
       status="EXECUTED"
       error_type=None
  -> 실패:
       error_type="BIZ_RETRY"
       last_error=DBSqlError message
```

### 6.7 verify_sql_node

```text
verify_sql_node(state)
  -> current_v_sql 없으면 PASS
  -> execute_verification(v_sql)
      SQL script 분리
      각 SELECT 실행
      마지막 result set 확인
      row가 없으면 성공
      모든 col 값이 "0"이면 성공
      NULL 또는 0이 아닌 값이 있으면 실패
  -> 성공:
       status="PASS"
       error_type=None
  -> 실패:
       error_type="BIZ_RETRY"
       last_error=verification message
```

### 6.8 biz_retry_prepare_node

```text
biz_retry_prepare_node(state)
  -> log_business_history(... ROW_ERROR ...)
  -> is_first_job_for_target(...)이면 truncate_table(job.to_table)
  -> sleep(1)
  -> db_attempts += 1
  -> error_type=None
  -> status=None
```

### 6.9 finalize_node

```text
finalize_node(state)
  -> elapsed 계산
  -> status == PASS:
       update_job_status(map_id, "PASS", elapsed, db_attempts)
       log_business_history(... VERIFY PASS ...)
  -> status == SKIP:
       update_job_status(map_id, "SKIP", elapsed, db_attempts)
       log_business_history(... DEP_CHECK SKIP ...)
  -> 그 외:
       update_job_status(map_id, "FAIL", elapsed, db_attempts)
       log_business_history(... FINAL FAIL ...)
```

`update_job_status()`는 `STATUS`, `USE_YN='N'`, `UPD_TS`, `ELAPSED_SECONDS`, `RETRY_COUNT`를 갱신합니다.

## 7. SQL Conversion Agent 상세 흐름

파일:

```text
server/agents/sql_conversion/agent.py
server/services/sql/agents.py
server/services/sql/workflow/graph.py
```

### 7.1 SqlConversionAgent.process_job

```text
SqlConversionAgent.__init__()
  -> TobeMultiAgentCoordinator(
       mapping_rule_provider=MappingRuleProvider(),
       generation_agent=TobeSqlGenerationAgent(),
       tuning_agent=SqlTuningAgent(max_iterations=0)
     )

process_job(job)
  -> coordinator.process_job(job)
```

SQL conversion agent는 tuning을 실행하지 않습니다. `SqlTuningAgent(max_iterations=0)`으로 비활성화된 상태입니다.

### 7.2 TobeMultiAgentCoordinator.process_job 전체 흐름

```text
process_job(job)
  -> job_key = SPACE_NM.SQL_ID
  -> retry_count = 0
  -> max_retries = 3
  -> state = _build_state(job, last_error=None)
  -> sql_length = classify_sql_length(FR_SQL_TEXT, EDIT_FR_SQL)
  -> map_type = get_sql_map_type(TARGET_TABLE)
  -> update_job_classification(SQL_LENGTH, MAP_TYPE)
  -> job.status == FAIL이면 reset_tuning_state()
  -> get_unready_target_tables(TARGET_TABLE)
      있으면:
        update_job_na(reason)
        insert_sql_log(ERROR, status=NA, stage=CHECK_TARGET_MAPPING)
        return "NA"
  -> while retry_count < 3:
       state = _build_state(job, previous_last_error)
       state.last_error = RETRY_CONTEXT or None
       graph.invoke({"execution": state, "terminal_action": None})
       tag_kind 확인
       if tag_kind != SELECT:
          _complete_non_select_job()
          return "PASS"
       if state.status != PASS:
          retry_count += 1
          state.last_error = TEST_VALIDATION_FAIL + row summary
          backoff 후 retry
       else:
          _persist_success()
          return "PASS"
  -> _persist_failure()
  -> return "FAIL"
```

### 7.3 SQL_LENGTH 분류

```text
classify_sql_length(fr_sql_text, edit_fr_sql)
  -> FR_SQL_TEXT 길이 <= 5000
     AND EDIT_FR_SQL이 있으면 EDIT_FR_SQL 길이 <= 5000
       => SHORT
  -> 그 외 LONG
```

### 7.4 MAP_TYPE 분류

```text
get_sql_map_type(target_table)
  -> target_table 문자열을 table token으로 파싱
  -> NEXT_SQL_COMPLEX_MAP 테이블 존재 확인
       없으면 RuntimeError 발생
  -> NEXT_SQL_COMPLEX_MAP에서
       USE_YN='Y'
       AND FR_TABLE = target_table
     인 mapping 존재 여부 확인
  -> active complex mapping이 있으면:
       COMPLEX
  -> active complex mapping이 없으면:
       SIMPLE
  -> target_table 자체가 없으면:
       None
```

중요:

- complex 여부는 더 이상 `NEXT_MIG_INFO.MAP_TYPE`으로 판단하지 않습니다.
- `NEXT_SQL_INFO.MAP_TYPE`에는 위 판정 결과인 `COMPLEX` 또는 `SIMPLE`이 저장됩니다.
- `NEXT_SQL_COMPLEX_MAP` 테이블이 없으면 simple flow로 fallback하지 않습니다.

### 7.5 Target mapping readiness check

```text
get_unready_target_tables(target_table)
  -> MAP_TYPE == COMPLEX이면:
       target table 목록을 다시 분리
       NEXT_SQL_COMPLEX_MAP에 있는 target table:
         complex mapping readiness는 NEXT_SQL_COMPLEX_MAP 존재/룰 조회로 판단
       NEXT_SQL_COMPLEX_MAP에 없는 target table:
         기존 NEXT_MIG_INFO readiness check 수행
  -> MAP_TYPE != COMPLEX이면:
  -> target table token 파싱
  -> NEXT_MIG_INFO TARGET_YN='Y' mapping 조회
  -> FR_TABLE에 target table token이 들어간 mapping의 STATUS 확인
  -> 매칭 mapping이 없거나 하나라도 STATUS != PASS:
       해당 target table을 unready로 반환
```

unready가 있으면:

```text
STATUS = "NA"
LOG = "NA reason=TARGET_MAPPING_NOT_READY: ..."
NEXT_SQL_LOG에 ERROR/NA 기록
conversion 종료
```

### 7.6 SQL workflow graph

파일:

```text
server/services/sql/workflow/graph.py
```

graph:

```text
START
  -> tobe_generation.generate
      -> route_after_generation()
          TAG_KIND != SELECT -> END
          TAG_KIND == SELECT -> tobe_generation.validate
  -> END
```

즉:

- SELECT: TO-BE 생성 후 bind/test validation까지 수행
- non-SELECT: TO-BE 생성만 수행하고 validation은 conversion coordinator에서 skip 처리

### 7.7 TO-BE SQL generation stage

```text
TobeSqlGenerationAgent.generate(state)
  -> TOBE_CORRECT_SQL 확인
      있으면:
        state.tobe_sql = TOBE_CORRECT_SQL
        NEXT_SQL_LOG:
          SQL_KIND=TOBE_SQL
          STATUS=SUCCESS
          STAGE_NAME=USE_TOBE_CORRECT_SQL
        return
  -> 없으면:
        generate_tobe_sql(job, mapping_rules, last_error)
        state.tobe_sql = LLM result
```

### 7.8 generate_tobe_sql 내부

```text
generate_tobe_sql(job, mapping_rules, last_error)
  -> template = tobe_sql_prompt.json
  -> target_tables = TARGET_TABLE token 목록
  -> _select_mapping_rules_for_job(job, mapping_rules)
       NEXT_MIG_INFO / NEXT_MIG_INFO_DTL 중 TARGET_TABLE과 FR_TABLE이 매칭되는 기본 mapping 선택
  -> get_complex_mapping_rules_for_job(job, target_tables)
       for each target_table:
          NEXT_SQL_COMPLEX_MAP
          WHERE USE_YN='Y'
            AND FR_TABLE = target_table
          후보 조회
          query SQL = EDIT_FR_SQL if present else FR_SQL_TEXT
          embedding target = FR_COL only
          top-k = COMPLEX_MAP_SEARCH_TOP_K, default 3
  -> mapping_schema_text =
       [MIGRATION_MAPPING_RULES]
       [SQL_CONVERSION_SUPPLEMENTAL_RULES_TOP_3_BY_FR_TABLE]
  -> correct_sql_hints = correct_sql_hint_rag_service.retrieve_correct_sql_hints(kind=TOBE)
  -> correct_sql_hint_json = serialize_correct_sql_hints_for_prompt(correct_sql_hints)
  -> prompt payload:
       from_sql
       mapping_schema_text
       target_schema
       correct_sql_hint_json
       last_error
  -> _call_llm_for_job(SQL_KIND=TOBE_SQL, PROMPT_NAME=template)
```

Complex mapping 처리:

```text
NEXT_SQL_COMPLEX_MAP:
  SQL Conversion 전용 보조 매핑룰 저장소
  MAP_KIND / GENERAL / SEARCH 구분 없음
  FR_TABLE = NEXT_SQL_INFO.TARGET_TABLE과 비교되는 실제 table명
  FR_COL = 단일 source column 또는 AS-IS SQL pattern
  TO_TABLE = TO-BE table명
  TO_COL = 단일 TO-BE column 또는 TO-BE SQL pattern
  SQL_CONVERSION_SUPPLEMENTAL_RULES_TOP_3_BY_FR_TABLE =
    TARGET_TABLE별 FR_TABLE 일치 후보 안에서 FR_COL 기준 vector search top-k
  MAP_ID, 검색 점수, DESCRIPTION은 prompt에 전달하지 않음
```

Simple mapping 처리:

```text
FROM_TABLE / FROM_COLUMN / TO_TABLE / TO_COLUMN 반복 구조
correct_sql_hint_json 최대 2건 전달
```

### 7.9 Bind stage 진입 조건

```text
TobeSqlGenerationAgent.validate(state)
  -> correct_bind_sql = BIND_CORRECT_SQL.strip()
  -> bind_param_names =
       extract_bind_param_names(source_sql)
       OR extract_bind_param_names(tobe_sql)
       OR extract_bind_param_names(correct_bind_sql)
```

bind parameter 감지 대상:

- `#{param}`
- `${param}`
- `<foreach collection="...">`
- `<if test="...">`
- `<when test="...">`

분기:

```text
if bind_param_names 없음 AND BIND_CORRECT_SQL 없음:
  state.bind_sql = ""
  state.bind_set_for_db = None
  state.bind_set_json_for_test = "[{}]"
  stage=SKIP_BIND
else:
  Bind SQL 생성/실행 단계 진행
```

### 7.10 Bind SQL generation stage

```text
Bind source SQL 결정
  -> _prepare_bind_source_sql(state)
```

`_prepare_bind_source_sql()`:

```text
original_sql = job.source_sql
sql_length = job.sql_length
should_pretune =
  sql_length == LONG
  OR len(original_sql) >= BIND_SQL_PRETUNING_MIN_LENGTH

if BIND_SQL_PRETUNING_ENABLED == false:
  return original_sql
if should_pretune == false:
  return original_sql

generate_bind_tuned_sql()
  -> bind_tuned_sql_prompt.json
  -> tuning_examples = retrieve_tuning_examples(job.source_sql)
  -> update_fr_bindtuned_sql(FR_BINDTUNED_SQL)
  -> return tuned_sql
```

Bind correct SQL 분기:

```text
if BIND_CORRECT_SQL 존재:
  state.bind_sql = BIND_CORRECT_SQL
  NEXT_SQL_LOG:
    SQL_KIND=BIND_SQL
    STATUS=SUCCESS
    STAGE_NAME=USE_BIND_CORRECT_SQL
else:
  generate_bind_sql(job, last_error, bind_source_sql)
```

`generate_bind_sql()`:

```text
if FINAL_RETRY_MODE:
  template = bind_sql_final_retry_prompt.json
else:
  template = bind_sql_prompt.json

correct_sql_hints = retrieve_correct_sql_hints(kind=BIND)
prompt payload:
  from_sql = bind_source_sql
  from_schema = ORACLE_SCHEMA_SRC
  correct_sql_hint_json
  last_error
_call_llm_for_job(SQL_KIND=BIND_SQL)
```

### 7.11 Bind SQL execution

```text
execute_binding_query(state.bind_sql, max_rows=50)
  -> _prepare_runtime_sql(stage=EXECUTE_BIND_SQL)
      trim
      trailing semicolon 제거
      LIMIT/FETCH FIRST를 Oracle ROWNUM 형태로 변환
      MyBatis tag/placeholders 남아 있으면 DBSqlError
  -> Oracle에서 SQL 실행
  -> cursor.description 기준 column name 추출
  -> fetchmany(max_rows)
  -> list[dict[column, value]] 반환
```

실행 실패:

```text
NEXT_SQL_LOG:
  SQL_KIND=BIND_SQL
  STATUS=FAIL
  STAGE_NAME=EXECUTE_BIND_SQL
  ERROR_MESSAGE=...
raise
```

실행 성공:

```text
NEXT_SQL_LOG:
  SQL_KIND=BIND_SQL
  STATUS=SUCCESS
  STAGE_NAME=EXECUTE_BIND_SQL
```

### 7.12 BIND_SET 생성

```text
build_bind_sets(bind_query_rows, max_cases=3)
  -> row별 dict 생성
  -> column alias를 bind key로 사용
  -> key set이 {"NO_BIND"}이면 [{}] 반환
  -> 값 signature 중복 제거
  -> 최대 3건 선택
  -> 결과 없으면 [{}]

bind_sets_to_json()
  -> JSON string
  -> state.bind_set_json_for_test
  -> state.bind_set_for_db
```

### 7.13 Test SQL generation stage

```text
correct_test_sql = TEST_CORRECT_SQL.strip()

if TEST_CORRECT_SQL 존재:
  state.test_sql = TEST_CORRECT_SQL
  NEXT_SQL_LOG:
    SQL_KIND=TEST_SQL
    STATUS=SUCCESS
    STAGE_NAME=USE_TEST_CORRECT_SQL
else:
  generate_test_sql(job, tobe_sql, bind_set_json, last_error)
```

`generate_test_sql()`:

```text
correct_sql_hints = retrieve_correct_sql_hints(kind=TEST)
_generate_validation_test_sql(
  from_sql=job.source_sql,
  tobe_sql=state.tobe_sql,
  bind_set_json=state.bind_set_json_for_test,
  from_schema=ORACLE_SCHEMA_SRC,
  tobe_schema=ORACLE_SCHEMA_TGT,
  final_retry_mode=_is_final_retry_mode(last_error),
  correct_sql_hint_json=...
)
```

template 선택:

```text
FINAL_RETRY_MODE ON  -> test_sql_final_retry_prompt.json
FINAL_RETRY_MODE OFF -> test_sql_prompt.json
```

### 7.14 Test SQL execution and status 판정

```text
execute_test_query(state.test_sql)
  -> _prepare_runtime_sql(stage=EXECUTE_TEST_SQL)
  -> Oracle 실행
  -> 모든 row fetch
  -> list[dict[column, value]]
```

`evaluate_status_from_test_rows()`:

```text
if rows 없음:
  FAIL
if CASE_NO, FROM_COUNT, TO_COUNT 컬럼 없으면:
  DBSqlError
for each row:
  FROM_COUNT, TO_COUNT int 변환
  둘 중 하나 None이면 mismatch
  둘 다 0이면 mismatch
  서로 다르면 mismatch
모든 row match:
  PASS
else:
  FAIL
```

판정 후:

```text
state.status = PASS or FAIL
NEXT_SQL_LOG:
  SQL_KIND=TEST_SQL
  STATUS=state.status
  STAGE_NAME=EXECUTE_TEST_SQL
```

### 7.15 SELECT conversion retry

```text
if state.status != PASS:
  retry_count += 1
  state.last_error =
    "TEST_VALIDATION_FAIL: CASE_NO=...,FROM_COUNT=...,TO_COUNT=..."
  if retry_count < 3:
    sleep backoff
    retry
  else:
    _persist_failure()
```

retry context:

```text
RETRY_CONTEXT: attempt=2/3; FINAL_RETRY_MODE=OFF; last_error=...
RETRY_CONTEXT: attempt=3/3; FINAL_RETRY_MODE=ON; last_error=...
```

### 7.16 SELECT conversion success persistence

```text
_persist_success(state)
  -> update_cycle_result(
       TO_SQL_TEXT=state.tobe_sql,
       TUNED_SQL=state.tuned_sql or None,
       TUNED_RESULT=state.tuned_result or None,
       TUNED_TEST=state.tuned_test or "READY",
       BIND_SQL=state.bind_sql,
       BIND_SET=state.bind_set_for_db,
       TEST_SQL=state.test_sql,
       STATUS=state.status,
       LOG="FINAL SUCCESS stage=COMPLETED ..."
       FORMATTED_SQL=state.formatted_sql or None
     )
```

conversion agent에서는 tuning이 비활성화되어 있으므로 보통:

```text
TUNED_TEST = "READY"
FORMATTED_SQL = None
```

### 7.17 non-SELECT conversion completion

SQL workflow graph는 non-SELECT에서 TO-BE 생성 후 validate를 타지 않습니다.

```text
if TAG_KIND != SELECT:
  _complete_non_select_job(state, tag_kind)
```

저장값:

```text
TO_SQL_TEXT = state.tobe_sql
BIND_SQL = ""
BIND_SET = None
TEST_SQL = ""
STATUS = "PASS"
TUNED_TEST = state.tuned_test or "READY"
LOG = "FINAL SUCCESS ... reason=TAG_KIND:..."
```

## 8. Correct SQL 로직

Correct SQL은 job 전체를 PASS로 강제하지 않습니다.

각 stage의 LLM 생성만 대체하고, 그 이후 실행/검증은 계속 진행합니다.

### 8.1 TOBE_CORRECT_SQL

```text
TO-BE generation stage
  -> TOBE_CORRECT_SQL 존재?
      Y:
        state.tobe_sql = TOBE_CORRECT_SQL
        LLM 호출 없음
        NEXT_SQL_LOG USE_TOBE_CORRECT_SQL
        다음 stage 진행
      N:
        generate_tobe_sql() LLM 호출
```

### 8.2 BIND_CORRECT_SQL

```text
Bind stage
  -> BIND_CORRECT_SQL 존재?
      Y:
        state.bind_sql = BIND_CORRECT_SQL
        LLM 호출 없음
        execute_binding_query(state.bind_sql)
        build_bind_sets()
        Test SQL stage 진행
      N:
        generate_bind_sql() LLM 호출
```

### 8.3 TEST_CORRECT_SQL

```text
Test SQL stage
  -> TEST_CORRECT_SQL 존재?
      Y:
        state.test_sql = TEST_CORRECT_SQL
        LLM 호출 없음
        execute_test_query(state.test_sql)
        evaluate_status_from_test_rows()
      N:
        generate_test_sql() LLM 호출
```

예시:

```text
TOBE_CORRECT_SQL 없음
BIND_CORRECT_SQL 있음
TEST_CORRECT_SQL 없음

실제 흐름:
  TO-BE SQL은 LLM 생성
  Bind SQL은 correct SQL 사용
  Bind SQL 실행
  BIND_SET 생성
  Test SQL은 LLM 생성
  Test SQL 실행
  PASS/FAIL 판정
```

## 9. Correct SQL RAG Hint 로직

파일:

```text
server/services/sql/correct_sql_rag_service.py
server/repositories/sql/result_repository.py
```

### 9.1 Corpus 조회

```text
retrieve_correct_sql_hints(sql_text, correct_kind, current_row_id)
  -> get_feedback_corpus_rows(correct_kind)
```

`correct_kind` 매핑:

| correct_kind | 우선 컬럼 |
| --- | --- |
| `TOBE` | `TOBE_CORRECT_SQL` |
| `BIND` | `BIND_CORRECT_SQL` |
| `TEST` | `TEST_CORRECT_SQL` |

fallback:

```text
해당 correct column이 없으면 legacy CORRECT_SQL 사용 가능
correct SQL 비어 있으면 제외
EDIT_FR_SQL 있으면 검색 기준 SQL로 사용
없으면 FR_SQL_TEXT 사용
```

### 9.2 검색 방식

```text
if RAG_EMBED_BASE_URL 있고 faiss/numpy 사용 가능:
  vector search
else:
  token fallback search
```

Vector search:

```text
candidate SQL normalize
query SQL normalize
embedding endpoint 호출
FAISS IndexFlatIP
top_k 검색
score 포함 hint 생성
```

Token fallback:

```text
SQL normalize
identifier token set 생성
Jaccard similarity 계산
top_k 선택
```

Prompt에 실제 전달되는 값:

```json
[
  "SELECT ...",
  "SELECT ..."
]
```

메타데이터는 prompt에 넣지 않습니다.

## 10. SQL Tuning Agent 상세 흐름

파일:

```text
server/agents/sql_tuning/agent.py
server/services/sql/agents.py
server/services/sql/tobe_sql_tuning_service.py
```

### 10.1 SqlTuningAgent wrapper

```text
process_job(job)
  -> JobExecutionState 생성
       job
       job_key
       mapping_rules=get_all_mapping_rules()
       last_error=None
  -> state.tobe_sql = job.to_sql_text
  -> state.bind_set_for_db = job.bind_set
  -> _SqlTuningAgent.run(state)
  -> final_status = state.tuned_test or "FAIL"
  -> update_cycle_result(
       TO_SQL_TEXT=state.tobe_sql,
       TUNED_SQL=state.tuned_sql,
       TUNED_RESULT=state.tuned_result,
       TUNED_TEST=final_status,
       BIND_SQL=job.bind_sql,
       BIND_SET=job.bind_set,
       TEST_SQL=job.test_sql,
       STATUS=job.status,
       LOG=final_log,
       FORMATTED_SQL=state.formatted_sql
     )
  -> return final_status
```

예외 발생:

```text
update_tuning_error(
  TUNED_TEST='FAIL',
  TUNED_SQL=state.tuned_sql if exists,
  LOG='[TUNING_ERROR] ...'
)
return "FAIL"
```

### 10.2 Tuning core run()

```text
SqlTuningAgent.run(state)
  -> state.tuned_sql = ""
  -> state.tuned_test = None
  -> max_iterations <= 0이면 return
  -> tag_kind 확인
  -> max_tuning_attempts:
       SELECT     -> 2
       non-SELECT -> 1
  -> for tuning_attempt in 1..max_tuning_attempts:
       _apply_tuning_rules(state)
       if tag_kind != SELECT:
          state.tuned_test = "PASS_NON_SELECT"
          break
       try:
          _run_tuned_sql_validation(state)
       except:
          if 마지막 attempt이면 raise
          state.last_error = "TUNED_TEST_SQL_ERROR: ..."
          continue
       if state.tuned_test == PASS:
          break
       if 마지막 attempt:
          break
       state.last_error =
          "TUNED_TEST_VALIDATION_FAIL: CASE_NO=...,BASELINE_COUNT=...,TUNED_COUNT=..."
  -> if TUNED_TEST == PASS:
       increment_rule_hit_counts_for_success()
  -> if TUNED_TEST in (PASS, PASS_NON_SELECT):
       generate_formatted_sql()
       state.formatted_sql = result
```

### 10.3 _apply_tuning_rules()

```text
_apply_tuning_rules(state)
  -> current_sql = state.tobe_sql
  -> state.tuned_sql = current_sql
  -> state.tuned_result = ""
  -> state.tuned_test = None
  -> state.tuned_test_rows = []
  -> state.tuning_examples = []
  -> for iteration in 1..TOBE_SQL_TUNING_MAX_ITERATIONS:
       tuning_examples = retrieve_tuning_examples(current_sql)
       state.tuning_examples = tuning_examples
       update_block_rag_content(row_id, serialize_tuning_examples_for_log(tuning_examples))
       if not tuning_examples:
          state.tuned_result = "NO TUNING"
          break
       tuned_sql, tuned_result = tune_tobe_sql(current_sql, tuning_examples, last_error)
       if tuned_sql.strip() == current_sql.strip():
          state.tuned_result = "NO TUNING"
          break
       else:
          state.tuned_result = tuned_result
          current_sql = tuned_sql
  -> state.tuned_sql = current_sql
```

### 10.4 Tuning RAG 조회

```text
retrieve_tuning_examples(current_sql)
  -> NEXT_SQL_RULES에서 RULE_TYPE='SEARCH' rule 조회
  -> DB 조회 실패 시 JSON catalog fallback
  -> embedding 가능하면 vector search
  -> embedding 불가 시 normalize/token 기반 fallback
  -> top_k 반환
```

RAG 결과는 두 방식으로 사용됩니다.

```text
1. Prompt 입력:
   - source_sql
   - guidance
   - example_bad_sql
   - example_tuned_sql

2. BLOCK_RAG_CONTENT 저장:
   - rule_id
   - score
   - search method
   - guidance
   - examples
   - metadata
```

### 10.5 tune_tobe_sql()

```text
tune_tobe_sql(current_tobe_sql, tuning_examples, last_error, job)
  -> template = tobe_sql_tuning_prompt.json
  -> prompt payload:
       current_tobe_sql
       universal_tuning_rules
       tuning_examples_json
       last_error
  -> _call_tuning_llm_for_job()
       LLM response는 JSON object 기대
       tuned_sql 추출
       tuned_result 추출
       NEXT_SQL_LOG:
         TUNED_SQL
         TUNED_RESULT
  -> return tuned_sql, tuned_result
```

### 10.6 tuned test validation

```text
_run_tuned_sql_validation(state)
  -> generate_sql_comparison_test_sql(
       baseline_sql=state.tobe_sql,
       candidate_sql=state.tuned_sql,
       bind_set_json=state.bind_set_for_db,
       last_error=state.last_error
     )
  -> prompt = tuned_test_sql_prompt.json
  -> execute_test_query(comparison_test_sql)
  -> evaluate_status_from_test_rows(comparison_rows)
  -> state.tuned_test = PASS or FAIL
  -> NEXT_SQL_LOG:
       SQL_KIND=TUNED_TEST_SQL
       STATUS=state.tuned_test
       STAGE_NAME=EXECUTE_TUNED_TEST_SQL
```

tuned test의 비교 기준:

```text
baseline_tobe_sql = 기존 TO-BE SQL
tuned_sql = 튜닝된 SQL
BIND_SET = conversion에서 만든 동일 bind set
```

즉 tuning test는 source AS-IS와 비교하지 않고, baseline TO-BE와 tuned SQL의 row count가 같은지 비교합니다.

### 10.7 rule HIT_CNT 증가

```text
if state.tuned_test == PASS:
  increment_rule_hit_counts_for_success(state.tuning_examples)
```

조건:

- `RULE_TYPE='SEARCH'` rule만 대상
- 중복 rule id는 한 번만 count
- For non-SELECT SQL, TUNED_TEST='PASS_NON_SELECT' means tuned SQL was created and validation was intentionally not executed.

### 10.8 formatting in tuning

```text
if state.tuned_test in ("PASS", "PASS_NON_SELECT"):
  state.formatted_sql = generate_formatted_sql(
    input_sql = state.tuned_sql or state.tobe_sql
  )
```

즉 tuning agent 내부에서 각 job이 성공/skip이면 기존처럼 즉시 formatting까지 수행합니다.

## 11. SQL Formatting Agent 상세 흐름

파일:

```text
server/agents/sql_formatting/agent.py
server/tools/sql_formatting.py
```

목적:

- tuning 과정에서 `FORMATTED_SQL` 생성이 누락된 row를 나중에 일괄 보정
- 독립 실행 tool/agent 역할

대상:

```text
TUNED_TEST IN ('PASS', 'PASS_NON_SELECT')
     AND (FORMATTED_SQL IS NULL OR FORMATTED_SQL is empty CLOB/blank)
```

흐름:

```text
SqlFormattingAgent.process_job(job)
  -> job_key = SPACE_NM.SQL_ID
  -> source_sql = job.tuned_sql or job.to_sql_text
  -> source_sql 비어 있으면:
       return "SKIP"
  -> generate_formatted_sql(job, source_sql)
       prompt = sql_indent_format_prompt.json
       NEXT_SQL_LOG:
         SQL_KIND=FORMATTED_SQL
         STAGE_NAME=GENERATE_FORMATTED_SQL
  -> update_formatted_sql(row_id, formatted_sql)
  -> return "PASS"
  -> 예외 발생:
       return "FAIL"
```

## 12. LLM 호출 및 fallback 로직

파일:

```text
server/services/sql/llm_service.py
server/core/llm_fallback.py
```

### 12.1 공통 LLM call

```text
_call_llm_for_job()
  -> call_llm_api()
  -> 성공:
       insert_sql_log(... STATUS=SUCCESS ...)
       return sql_text
  -> 실패:
       insert_sql_log(... STATUS=FAIL, ERROR_MESSAGE=...)
       raise
```

Formatting:

```text
_call_formatter_llm_for_job()
  -> prompt = sql_indent_format_prompt.json
  -> call_llm_text_api()
  -> empty response이면 ValueError
  -> NEXT_SQL_LOG FORMATTED_SQL 기록
```

Tuning:

```text
_call_tuning_llm_for_job()
  -> call_llm_text_api()
  -> _extract_tuning_response(response_text)
  -> NEXT_SQL_LOG TUNED_SQL 기록
  -> tuned_result 있으면 NEXT_SQL_LOG TUNED_RESULT 기록
```

### 12.2 model_candidates()

```text
model_candidates(primary_model)
  -> candidates = []
  -> active_model 있으면 먼저 추가
  -> primary_model 추가
  -> LLM_FALLBACK_MODELS 순서대로 추가
  -> 중복 제거
```

### 12.3 call_llm_api fallback

```text
call_llm_api(...)
  -> resolved_api_key 확인
  -> resolved_model = LLM_MODEL
  -> raw_base_url = LLM_BASE_URL
  -> candidates = model_candidates(resolved_model)
  -> for candidate_model in candidates:
       provider 결정
       ChatAnthropic 또는 ChatOpenAI 생성
       llm.invoke(messages)
       성공:
         set_active_model(candidate_model)
         return text
       실패:
         if rate limit/timeout/504/429:
           raise LLMRateLimitError
         if 다음 후보 있고 is_model_fallback_error(message):
           다음 model 시도
         else:
           raise
```

fallback 대상 fatal pattern:

- `model not allow`
- `model_not_allow`
- `model not allowed`
- `not allowed to access model`
- `team not allowed`
- `model not found`
- `model does not exist`
- `not supported`
- `permission`
- `not authorized`
- `access denied`
- `forbidden`

fallback 제외 transient pattern:

- `timed out`
- `timeout`
- `rate limit`
- `429`
- `gateway timeout`
- `504`
- `connection reset`
- `temporarily unavailable`

### 12.4 cycle별 active model reset

```text
Supervisor poll_node 시작
  -> reset_active_model()
```

따라서:

- 한 cycle 안에서 fallback으로 성공한 모델은 계속 우선 사용
- 다음 cycle이 시작되면 다시 기본 모델부터 시도

## 13. NEXT_SQL_INFO update 로직

### 13.1 update_cycle_result()

SQL conversion/tuning 성공/실패 저장에 사용됩니다.

갱신 대상:

- `TO_SQL_TEXT`
- `TUNED_SQL`
- `TUNED_RESULT`
- `TUNED_TEST`
- `FORMATTED_SQL` 조건부
- `BIND_SQL`
- `BIND_SET`
- `TEST_SQL`
- `STATUS`
- `LOG`
- `UPD_TS`

특징:

- 대상 컬럼이 존재하는지 확인 후 optional 컬럼만 갱신
- VARCHAR 길이 제한이 있는 컬럼은 UTF-8 byte 기준 truncate
- CLOB 컬럼은 길이 제한 적용 제외

### 13.2 update_tuning_error()

```text
update_tuning_error(row_id, error_msg, tuned_sql)
  -> TUNED_TEST='FAIL'
  -> TUNED_SQL 있으면 저장
  -> LOG='[TUNING_ERROR] ...'
  -> UPD_TS=SYSDATE
```

### 13.3 update_job_na()

```text
update_job_na(row_id, reason)
  -> STATUS='NA'
  -> LOG='NA reason=...'
  -> UPD_TS=CURRENT_TIMESTAMP
```

### 13.4 reset_tuning_state()

conversion row가 `STATUS='FAIL'`로 재처리될 때 tuning 관련 이전 결과를 초기화합니다.

```text
TUNED_SQL = NULL
TUNED_TEST = NULL
TUNED_RESULT = NULL
BLOCK_RAG_CONTENT = NULL
```

컬럼이 없으면 해당 컬럼은 skip합니다.

### 13.5 update_formatted_sql()

```text
FORMATTED_SQL 컬럼 존재 확인
  -> FORMATTED_SQL = formatted_sql
  -> UPD_TS = CURRENT_TIMESTAMP
```

## 14. 상태값 의미

### 14.1 NEXT_SQL_INFO.STATUS

| STATUS | 의미 |
| --- | --- |
| `URGENT` | conversion 우선 처리 대상 |
| `READY` | conversion 일반 대기 |
| `PENDING` | conversion 대기 |
| `FAIL` | conversion 재시도 대상 |
| `SKIP` | Manual user exclusion. SQL conversion polling does not pick it up automatically. |
| `PASS` | conversion 완료 |
| `NA` | conversion/test 대상 제외 |
| `NULL` | conversion 대기 |

### 14.2 NEXT_SQL_INFO.TUNED_TEST

| TUNED_TEST | 의미 |
| --- | --- |
| `URGENT` | tuning 우선 처리 대상 |
| `READY` | tuning 일반 대기 |
| `FAIL` | tuning 재시도 대상 |
| `PASS` | tuning validation 통과 |
| `PASS_NON_SELECT` | Non-SELECT tuning validation was not executed, but tuning and formatting are treated as successful. |
| `NULL` | tuning 대상 아님 또는 아직 conversion 미완료 |
| `NA` | tuning 제외 |

## 15. 주요 end-to-end 시나리오

### 15.1 정상 SELECT conversion + tuning + formatting

```text
Supervisor poll
  -> SQL Conversion job 선정
  -> run_sql_conversion
      -> BATCH_CNT + 1
      -> SQL_LENGTH/MAP_TYPE 저장
      -> target mapping readiness PASS
      -> TOBE_CORRECT_SQL 없음
      -> TO-BE LLM 생성
      -> TAG_KIND=SELECT
      -> bind parameter 감지
      -> BIND_CORRECT_SQL 없음
      -> Bind SQL LLM 생성
      -> Bind SQL 실행
      -> BIND_SET 생성
      -> TEST_CORRECT_SQL 없음
      -> Test SQL LLM 생성
      -> Test SQL 실행
      -> FROM_COUNT == TO_COUNT and non-zero
      -> STATUS=PASS
      -> TUNED_TEST=READY
  -> 다음 cycle 또는 같은 cycle에서 SQL Tuning job 선정
  -> run_sql_tuning
      -> BATCH_CNT + 1
      -> RAG tuning rule 조회
      -> BLOCK_RAG_CONTENT 저장
      -> TUNED_SQL/TUNED_RESULT LLM 생성
      -> tuned_test_sql_prompt로 baseline TO-BE vs tuned SQL 비교 SQL 생성
      -> tuned test 실행
      -> PASS
      -> HIT_CNT 증가
      -> FORMATTED_SQL 생성
      -> TUNED_TEST=PASS 저장
```

### 15.2 Correct SQL 포함 SELECT conversion

```text
TOBE_CORRECT_SQL 있음
BIND_CORRECT_SQL 있음
TEST_CORRECT_SQL 있음

실행:
  -> TO-BE LLM 호출 안 함
  -> state.tobe_sql = TOBE_CORRECT_SQL
  -> Bind SQL LLM 호출 안 함
  -> state.bind_sql = BIND_CORRECT_SQL
  -> Bind SQL은 실제 DB에서 실행
  -> BIND_SET 생성
  -> Test SQL LLM 호출 안 함
  -> state.test_sql = TEST_CORRECT_SQL
  -> Test SQL은 실제 DB에서 실행
  -> PASS/FAIL 판정
```

### 15.3 non-SELECT conversion

```text
TAG_KIND != SELECT
  -> TO-BE SQL 생성 또는 TOBE_CORRECT_SQL 사용
  -> workflow graph route_after_generation에서 END
  -> _complete_non_select_job()
  -> STATUS=PASS
  -> TUNED_TEST=READY
  -> BIND_SQL=""
  -> TEST_SQL=""
```

이후 tuning:

```text
TUNED_TEST=READY, STATUS=PASS
  -> tuning rule 적용
  -> TAG_KIND != SELECT
  -> tuned validation skip
  -> TUNED_TEST=PASS_NON_SELECT
  -> FORMATTED_SQL 생성
```

### 15.4 target mapping 미준비

```text
SQL Conversion 시작
  -> get_unready_target_tables(TARGET_TABLE)
  -> target table에 해당하는 NEXT_MIG_INFO mapping 없음
     OR mapping STATUS != PASS
  -> STATUS=NA
  -> LOG='NA reason=TARGET_MAPPING_NOT_READY: ...'
  -> NEXT_SQL_LOG ERROR/NA
  -> conversion 종료
```

### 15.5 tuning 실패 후 재시도

```text
TUNED_TEST=READY
  -> tuning attempt 1
  -> tuned test FAIL
  -> last_error = TUNED_TEST_VALIDATION_FAIL: ...
  -> tuning attempt 2
  -> prompt에 last_error 전달
  -> PASS이면 저장
  -> 계속 FAIL이면 TUNED_TEST=FAIL
```

### 15.6 formatting 보정 실행

```text
SQL_FORMATTING_ONLY=true
  -> Supervisor는 formatting job만 조회
  -> TUNED_TEST IN (PASS, PASS_NON_SELECT)
     AND (FORMATTED_SQL IS NULL OR FORMATTED_SQL is empty CLOB/blank)
  -> source_sql = TUNED_SQL or TO_SQL_TEXT
  -> sql_indent_format_prompt.json 호출
  -> FORMATTED_SQL 저장
```

## 16. 화면 관련 로직 요약

### 16.1 Dashboard status

```text
STATUS/TUNED_TEST 표시 전 normalize
  -> NA는 표시 제외
  -> URGENT, READY는 RUNNING으로 합산
```

Chatbot:

```text
사용자 입력
  -> user message 먼저 chat history에 저장
  -> rerun
  -> message container 안에서 user message 표시
  -> assistant "입력중..." 표시
  -> LLM 호출
  -> answer 저장
  -> rerun
```

### 16.1.1 Sidebar and runtime log

```text
app/app.py sidebar
  -> MENU
       Dashboard / monitor / detail / settings / XML export screen selector
  -> Agent selection
       DB_MIGRATION_ONLY
       SQL_CONVERSION_ONLY
       SQL_TUNING_ONLY
       SQL_FORMATTING_ONLY
       toggle values are written to .env
  -> Agent control
       start / pause / resume / stop
  -> Log
       shows tail of runtime/agent.log
```

```text
server/core/logger.py
  -> creates migration_agent logger
  -> attaches stdout StreamHandler
  -> attaches runtime/agent.log FileHandler
  -> Streamlit sidebar log viewer reads the same file
```

### 16.2 Job Detail SQL 선택

```text
get_sql_jobs()
  -> 전체 SQL row 목록 조회
  -> STATUS 필터
  -> TUNED_TEST 필터
  -> SQL_ID 부분 검색
  -> Namespace 부분 검색
  -> selectbox로 SPACE_NM.SQL_ID 선택
  -> 선택된 ROW_ID로 get_sql_job_full()
```

ROW_ID 직접 조회는 expander 안에서 보조 기능으로 유지됩니다.

### 16.3 XML Export

```text
get_xml_export_sqls()
  -> SPACE_NM, TAG_KIND, SQL_ID, TUNED_TEST, FORMATTED_SQL 조회
  -> XML 생성 기준은 FORMATTED_SQL
  -> namespace별 PASS/FAIL 카운트 표시
  -> namespace에 FAIL 있으면 namespace 다운로드 비활성
  -> 전체 일괄 다운로드는 PASS + FORMATTED_SQL 존재 건만 포함
```

## 17. 운영상 확인 포인트

- `JOB_BATCH_SIZE=5`는 supervisor cycle당 registry에 담는 실행 대상 수입니다.
- `JOB_MAX_BATCH_COUNT`는 `NEXT_SQL_INFO.BATCH_CNT` 기반 SQL job 재처리 상한입니다.
- SQL conversion/tuning/formatting tool 모두 `sql_inc(row_id)`를 호출하므로 `BATCH_CNT`가 증가합니다.
- tuning agent 내부 formatting은 정상 경로입니다.
- 별도 formatting agent는 누락 보정 경로입니다.
- Correct SQL은 stage 생성 대체일 뿐, 전체 PASS 강제는 아닙니다.
- `NEXT_SQL_INFO.LOG`는 최신 요약이고, stage별 상세 이력은 `NEXT_SQL_LOG`를 봐야 합니다.
- fallback으로 성공한 LLM model은 같은 cycle에서 계속 사용되고, 다음 cycle 시작 시 기본 모델부터 다시 시도합니다.
