import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

_ROOT = Path(__file__).resolve().parent.parent
_LOG_FILE = _ROOT / "runtime" / "agent.log"

import streamlit as st

st.set_page_config(
    page_title="Migration Pipeline Console",
    page_icon="🛠️",
    layout="wide",
    initial_sidebar_state="expanded",
)

from pages.dashboard import render as render_dashboard
from pages.job_detail import render as render_job_detail
from pages.mig_monitor import render as render_mig
from pages.rag_manager_page import render as render_rag
from pages.settings_page import render as render_settings
from pages.sql_monitor import render as render_sql
from pages.system_health import render as render_health
from pages.tuning_monitor import render as render_tuning
from pages.xml_export import render as render_xml
from utils.agent_control import get_status, pause, resume, start, stop
from utils.env_manager import read_env, write_env_key

_MENU = {
    "📊 Dashboard": render_dashboard,
    "🗄️ Mig Agent Monitor": render_mig,
    "🧾 SQL Agent Monitor": render_sql,
    "⚡ Tuning Agent Monitor": render_tuning,
    "🔎 Job Detail": render_job_detail,
    "📚 Tuning Rule Manager": render_rag,
    "🩺 System Health": render_health,
    "⚙️ Settings": render_settings,
    "📦 XML Export": render_xml,
}

st.markdown(
    """
<style>
[data-testid="stSidebarNav"],
[data-testid="stSidebarNavItems"],
[data-testid="stSidebarNavSeparator"],
section[data-testid="stSidebar"] ul { display: none !important; }
</style>
""",
    unsafe_allow_html=True,
)

with st.sidebar:
    st.image("https://img.icons8.com/color/96/database.png", width=60)
    st.markdown("## Migration Console")

    st.markdown("---")
    st.markdown("#### MENU")
    selected = st.radio("MENU", list(_MENU.keys()), label_visibility="collapsed")

    st.markdown("---")
    st.markdown("#### 🧭 Agent 선택")
    env = read_env()
    db_only = env.get("DB_MIGRATION_ONLY", "false").lower() == "true"
    sql_only = env.get("SQL_CONVERSION_ONLY", "false").lower() == "true"
    tuning_only = env.get("SQL_TUNING_ONLY", "false").lower() == "true"
    formatting_only = env.get("SQL_FORMATTING_ONLY", "false").lower() == "true"

    new_db_only = st.toggle("DB Migration", value=db_only)
    new_sql_only = st.toggle("SQL Conversion", value=sql_only)
    new_tuning_only = st.toggle("SQL Tuning", value=tuning_only)
    new_formatting_only = st.toggle("SQL Formatting", value=formatting_only)

    if (new_db_only, new_sql_only, new_tuning_only, new_formatting_only) != (
        db_only,
        sql_only,
        tuning_only,
        formatting_only,
    ):
        write_env_key("DB_MIGRATION_ONLY", str(new_db_only).lower())
        write_env_key("SQL_CONVERSION_ONLY", str(new_sql_only).lower())
        write_env_key("SQL_TUNING_ONLY", str(new_tuning_only).lower())
        write_env_key("SQL_FORMATTING_ONLY", str(new_formatting_only).lower())
        st.toast("Agent 선택 설정을 저장했습니다. 실행 중인 Agent에는 재시작 후 적용됩니다.")
        st.rerun()

    if not any((new_db_only, new_sql_only, new_tuning_only, new_formatting_only)):
        st.caption("전체 실행: 모든 Agent를 실행합니다.")
    else:
        selected_agents = []
        if new_db_only:
            selected_agents.append("DB")
        if new_sql_only:
            selected_agents.append("SQL")
        if new_tuning_only:
            selected_agents.append("Tuning")
        if new_formatting_only:
            selected_agents.append("Formatting")
        st.caption("선택 실행: " + ", ".join(selected_agents))

    st.markdown("---")
    st.markdown("#### ⚙️ Agent 제어")

    status = get_status()
    st.markdown(f"**{status['label']}**" + (f"  `PID {status['pid']}`" if status["pid"] else ""))

    if not status["running"]:
        if st.button("▶️ 시작", width="stretch", type="primary"):
            msg = start()
            st.toast(msg)
            st.rerun()
    else:
        c1, c2 = st.columns(2)
        if status["paused"]:
            with c1:
                if st.button("▶️ 재개", width="stretch", type="primary"):
                    st.toast(resume())
                    st.rerun()
        else:
            with c1:
                if st.button("⏸️ 일시정지", width="stretch"):
                    st.toast(pause())
                    st.rerun()
        with c2:
            if st.button("⏹️ 중지", width="stretch", type="secondary"):
                st.toast(stop())
                st.rerun()

    st.markdown("---")
    st.markdown("#### 📋 로그")
    col_log1, col_log2 = st.columns([3, 1])
    with col_log1:
        log_lines = st.number_input("로그 줄 수", min_value=10, max_value=200, value=30, step=10, label_visibility="collapsed")
    with col_log2:
        if st.button("↻", help="새로고침", width="stretch"):
            st.rerun()

    if _LOG_FILE.exists():
        lines = _LOG_FILE.read_text(encoding="utf-8", errors="ignore").splitlines()
        tail = "\n".join(lines[-int(log_lines):])
        st.code(tail if tail else "(로그 없음)", language=None)
    else:
        st.caption("에이전트 시작 후 로그가 생성됩니다.")

    st.markdown("---")
    st.caption("Unified Multi-Agent Pipeline")

_MENU[selected]()
