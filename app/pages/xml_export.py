from collections import defaultdict

import streamlit as st

from utils.db import get_tuned_pass_sqls

_XML_HEADER = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<!DOCTYPE mapper PUBLIC "-//mybatis.org//DTD Mapper 3.0//EN"'
    ' "http://mybatis.org/dtd/mybatis-3-mapper.dtd">'
)


def _build_xml(namespace: str, rows: list[dict]) -> str:
    lines = [_XML_HEADER, "", f'<mapper namespace="{namespace}">']
    for row in rows:
        tag = (row.get("TAG_KIND") or "select").strip().lower() or "select"
        sql_id = (row.get("SQL_ID") or "").strip()
        sql = (row.get("TUNED_SQL") or "").strip()
        if tag == "select":
            open_tag = f'  <{tag} id="{sql_id}" resultType="hashmap">'
        else:
            open_tag = f'  <{tag} id="{sql_id}">'
        lines.append(open_tag)
        for sql_line in sql.splitlines():
            lines.append(f"    {sql_line}")
        lines.append(f"  </{tag}>")
        lines.append("")
    lines.append("</mapper>")
    return "\n".join(lines)


def render():
    st.title("📄 MyBatis XML Export")
    st.caption("TUNED_TEST = PASS인 SQL을 namespace 기준으로 XML로 생성합니다.")

    col_refresh, _ = st.columns([1, 9])
    with col_refresh:
        if st.button("🔄 새로고침"):
            st.rerun()

    try:
        rows = get_tuned_pass_sqls()
    except Exception as exc:
        st.error(f"DB 연결 실패: {exc}")
        return

    if not rows:
        st.info("TUNED_TEST = PASS인 SQL이 없습니다.")
        return

    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        grouped[(row.get("SPACE_NM") or "(없음)").strip()].append(row)

    namespaces = sorted(grouped.keys())

    col_sel, col_info = st.columns([3, 1])
    with col_sel:
        selected_ns = st.selectbox(
            "Namespace 선택",
            options=namespaces,
            format_func=lambda ns: f"{ns} ({len(grouped[ns])}개 SQL)",
        )
    with col_info:
        st.metric("전체 namespace", len(namespaces))

    st.markdown("---")

    if selected_ns:
        xml_text = _build_xml(selected_ns, grouped[selected_ns])
        selected_count = len(grouped[selected_ns])

        col_download, col_selected = st.columns([1, 3])
        with col_download:
            st.download_button(
                "XML 다운로드",
                data=xml_text.encode("utf-8"),
                file_name=f"{selected_ns}.xml",
                mime="application/xml",
                width="stretch",
            )
        with col_selected:
            st.caption(f"{selected_ns} · {selected_count}개 SQL")

        st.code(xml_text, language="xml")
