import pandas as pd
import streamlit as st

from utils.db import get_sql_job_full, get_sql_jobs


ALL = "전체"
_COLS_TABLE = [
    "SQL_ID",
    "SPACE_NM",
    "TAG_KIND",
    "STATUS",
    "TUNED_TEST",
    "SQL_LENGTH",
    "MAP_TYPE",
    "EFFECTIVE_FR_SQL_LEN",
    "TO_SQL_LEN",
    "TARGET_TABLE",
    "UPD_TS",
]

_SQL_DETAIL_OPTIONS = {
    "ASIS SQL": "FR_SQL_TEXT",
    "EDIT ASIS SQL": "EDIT_FR_SQL",
    "TOBE SQL": "TO_SQL_TEXT",
    "BIND SQL": "BIND_SQL",
    "BIND SET": "BIND_SET",
    "TEST SQL": "TEST_SQL",
    "TUNED SQL": "TUNED_SQL",
    "TUNED RESULT": "TUNED_RESULT",
    "FORMATTED SQL": "FORMATTED_SQL",
    "TOBE CORRECT SQL": "TOBE_CORRECT_SQL",
    "BIND CORRECT SQL": "BIND_CORRECT_SQL",
    "TEST CORRECT SQL": "TEST_CORRECT_SQL",
    "BLOCK RAG CONTENT": "BLOCK_RAG_CONTENT",
    "LOG": "LOG",
}


def _prepare_df(rows: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(rows)
    required = [
        "ROW_ID",
        "SQL_ID",
        "SPACE_NM",
        "TAG_KIND",
        "STATUS",
        "TUNED_TEST",
        "SQL_LENGTH",
        "MAP_TYPE",
        "TARGET_TABLE",
        "FR_SQL_TEXT",
        "EDIT_FR_SQL",
        "TO_SQL_TEXT",
        "TUNED_SQL",
        "FORMATTED_SQL",
        "LOG",
    ]
    for col in required:
        if col not in df.columns:
            df[col] = ""
        df[col] = df[col].fillna("").astype(str)

    for col in ("FR_SQL_LEN", "EDIT_FR_SQL_LEN", "EFFECTIVE_FR_SQL_LEN", "TO_SQL_LEN", "TUNED_SQL_LEN", "FORMATTED_SQL_LEN"):
        if col not in df.columns:
            df[col] = 0
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0).astype(int)
    return df


def _options(df: pd.DataFrame, column: str) -> list[str]:
    values = [v for v in df[column].dropna().astype(str).str.strip().unique().tolist() if v]
    return [ALL] + sorted(values)


def _contains(series: pd.Series, keyword: str) -> pd.Series:
    keyword = keyword.strip()
    if not keyword:
        return pd.Series(True, index=series.index)
    return series.fillna("").astype(str).str.contains(keyword, case=False, na=False, regex=False)


def render():
    st.title("SQL Agent Monitor")

    if st.button("새로고침"):
        st.rerun()

    try:
        jobs = get_sql_jobs()
    except Exception as e:
        st.error(f"DB 연결 실패: {e}")
        return

    if not jobs:
        st.info("조회할 작업이 없습니다.")
        return

    df_all = _prepare_df(jobs)

    with st.expander("검색 / 필터", expanded=True):
        c1, c2, c3, c4 = st.columns([1.4, 1.4, 1, 1])
        with c1:
            sql_id_query = st.text_input("SQL_ID LIKE", placeholder="예: SEL_001")
        with c2:
            namespace_query = st.text_input("Namespace LIKE", placeholder="예: userMapper")
        with c3:
            target_query = st.text_input("TARGET_TABLE LIKE", placeholder="예: CUSTOMER")
        with c4:
            any_sql_query = st.text_input("SQL/LOG 본문 LIKE", placeholder="FROM, JOIN, 오류 메시지")

        c1, c2, c3, c4, c5 = st.columns(5)
        with c1:
            sel_status = st.selectbox("STATUS", _options(df_all, "STATUS"))
        with c2:
            sel_tuned = st.selectbox("TUNED_TEST", _options(df_all, "TUNED_TEST"))
        with c3:
            sel_map_type = st.selectbox("MAP_TYPE / map_kind", _options(df_all, "MAP_TYPE"))
        with c4:
            sel_sql_length = st.selectbox("SQL_LENGTH", _options(df_all, "SQL_LENGTH"))
        with c5:
            sel_tag_kind = st.selectbox("TAG_KIND", _options(df_all, "TAG_KIND"))

        presence = st.multiselect(
            "생성/로그 여부",
            ["TOBE SQL 있음", "TUNED SQL 있음", "FORMATTED SQL 있음", "LOG 있음"],
        )

    df = df_all.copy()
    df = df[_contains(df["SQL_ID"], sql_id_query)]
    df = df[_contains(df["SPACE_NM"], namespace_query)]
    df = df[_contains(df["TARGET_TABLE"], target_query)]

    if any_sql_query.strip():
        fields = (
            df["FR_SQL_TEXT"]
            + "\n"
            + df["EDIT_FR_SQL"]
            + "\n"
            + df["TO_SQL_TEXT"]
            + "\n"
            + df["TUNED_SQL"]
            + "\n"
            + df["FORMATTED_SQL"]
            + "\n"
            + df["LOG"]
        )
        df = df[_contains(fields, any_sql_query)]

    if sel_status != ALL:
        df = df[df["STATUS"] == sel_status]
    if sel_tuned != ALL:
        df = df[df["TUNED_TEST"] == sel_tuned]
    if sel_map_type != ALL:
        df = df[df["MAP_TYPE"] == sel_map_type]
    if sel_sql_length != ALL:
        df = df[df["SQL_LENGTH"] == sel_sql_length]
    if sel_tag_kind != ALL:
        df = df[df["TAG_KIND"] == sel_tag_kind]
    if "TOBE SQL 있음" in presence:
        df = df[df["TO_SQL_TEXT"].str.strip() != ""]
    if "TUNED SQL 있음" in presence:
        df = df[df["TUNED_SQL"].str.strip() != ""]
    if "FORMATTED SQL 있음" in presence:
        df = df[df["FORMATTED_SQL"].str.strip() != ""]
    if "LOG 있음" in presence:
        df = df[df["LOG"].str.strip() != ""]

    show_cols = [c for c in _COLS_TABLE if c in df.columns]
    with st.expander(f"검색 결과 표 ({len(df)}건 / 전체 {len(df_all)}건)", expanded=False):
        st.dataframe(df[show_cols], width="stretch", hide_index=True)

    st.divider()
    st.subheader("SQL 상세 조회")

    if df.empty:
        st.warning("조건에 맞는 SQL Job이 없습니다.")
        return

    row_ids = df["ROW_ID"].tolist()
    labels = [
        f"{r['SPACE_NM']} / {r['SQL_ID']} | STATUS={r['STATUS'] or 'NULL'} | MAP_TYPE={r['MAP_TYPE'] or '-'} | LEN={r['EFFECTIVE_FR_SQL_LEN']}"
        for _, r in df.iterrows()
    ]
    idx = st.selectbox("목록 선택", range(len(labels)), format_func=lambda i: labels[i])

    sel_row_id = row_ids[idx]
    row = next((j for j in jobs if j["ROW_ID"] == sel_row_id), None)
    if not row:
        return

    detail = get_sql_job_full(sel_row_id) or row

    m1, m2, m3, m4 = st.columns(4)
    with m1:
        st.metric("STATUS", detail.get("STATUS") or "-")
    with m2:
        st.metric("TUNED_TEST", detail.get("TUNED_TEST") or "-")
    with m3:
        st.metric("SQL_LENGTH", detail.get("SQL_LENGTH") or row.get("SQL_LENGTH") or "-")
    with m4:
        st.metric("MAP_TYPE", detail.get("MAP_TYPE") or row.get("MAP_TYPE") or "-")

    with st.expander("로그", expanded=True):
        log = detail.get("LOG") or ""
        if log:
            st.text_area("LOG", log, height=200, label_visibility="collapsed")
        else:
            st.info("로그 없음")

    st.subheader("SQL 컬럼 비교")
    option_labels = list(_SQL_DETAIL_OPTIONS.keys())
    left_picker, right_picker = st.columns(2)
    with left_picker:
        left_label = st.selectbox(
            "왼쪽 컬럼",
            option_labels,
            index=0,
            key="sql_monitor_left_col",
        )
    with right_picker:
        right_label = st.selectbox(
            "오른쪽 컬럼",
            option_labels,
            index=2,
            key="sql_monitor_right_col",
        )

    col1, col2 = st.columns(2)
    with col1:
        st.caption(left_label)
        st.code(detail.get(_SQL_DETAIL_OPTIONS[left_label]) or "(없음)", language="sql")
    with col2:
        st.caption(right_label)
        st.code(detail.get(_SQL_DETAIL_OPTIONS[right_label]) or "(없음)", language="sql")
