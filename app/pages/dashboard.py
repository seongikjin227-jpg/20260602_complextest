import os
import re
import json
import streamlit as st
from pathlib import Path
from datetime import datetime
from dotenv import load_dotenv
from openai import OpenAI
from utils.db import (
    get_mig_status_summary,
    get_sql_status_summary,
    get_tuning_status_summary,
    get_formatting_summary,
    get_recent_fails,
    get_mig_jobs,
    get_mig_logs,
)

_ROOT      = Path(__file__).resolve().parent.parent.parent
load_dotenv(_ROOT / ".env")
_CHATS_DIR = _ROOT / "runtime" / "chats"

# ── CSS ───────────────────────────────────────────────────────────────────────
CSS = """
<style>
/* 대화 목록 버튼 */
div[data-testid="stVerticalBlock"] button.chat-item {
    text-align: left; width: 100%;
}
/* 상태 카드 */
.stat-card {
    background: #f8f9fa; border: 1px solid #e9ecef;
    border-radius: 12px; padding: 14px 16px; margin-bottom: 10px;
}
.stat-card-title {
    font-size: 12px; font-weight: 700; letter-spacing: 1px;
    text-transform: uppercase; color: #6c757d; margin-bottom: 10px;
}
.stat-row {
    display: flex; justify-content: space-between;
    align-items: center; margin-bottom: 4px;
}
.stat-label { font-size: 13px; color: #495057; }
.stat-val   { font-size: 14px; font-weight: 700; color: #212529; }
.badge-pass { color: #10b981; }
.badge-fail { color: #ef4444; }
.badge-etc  { color: #6c757d; }
.rate-grid {
    display: grid; grid-template-columns: 1fr 1fr; gap: 8px;
    margin: 10px 0 8px 0;
}
.rate-box {
    background: #ffffff; border: 1px solid #e9ecef;
    border-radius: 8px; padding: 8px 10px;
}
.rate-label {
    font-size: 11px; color: #6c757d; font-weight: 700;
    letter-spacing: .4px; margin-bottom: 4px;
}
.rate-value { font-size: 18px; font-weight: 800; color: #212529; }
.rate-sub { font-size: 11px; color: #adb5bd; margin-top: 2px; }
.rate-note { font-size: 10px; color: #868e96; line-height: 1.4; margin: 2px 0 8px 0; }
/* 구분선 */
.divider { border-top: 1px solid #e9ecef; margin: 8px 0; }
</style>
"""

# ── 채팅 파일 관리 ─────────────────────────────────────────────────────────────
def _list_chats() -> list[dict]:
    if not _CHATS_DIR.exists():
        return []
    chats = []
    for f in sorted(_CHATS_DIR.glob("*.json"), reverse=True):
        try:
            chats.append(json.loads(f.read_text(encoding="utf-8")))
        except Exception:
            pass
    return chats

def _load_chat(chat_id: str) -> dict | None:
    path = _CHATS_DIR / f"{chat_id}.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None

def _save_chat(chat: dict):
    _CHATS_DIR.mkdir(parents=True, exist_ok=True)
    (_CHATS_DIR / f"{chat['id']}.json").write_text(
        json.dumps(chat, ensure_ascii=False, indent=2), encoding="utf-8"
    )

def _delete_chat(chat_id: str):
    (_CHATS_DIR / f"{chat_id}.json").unlink(missing_ok=True)

def _new_chat() -> dict:
    return {
        "id":       datetime.now().strftime("%Y%m%d_%H%M%S_%f"),
        "title":    "새 대화",
        "messages": [],
    }

# ── MAP_ID 감지 + DB 로그 조회 ────────────────────────────────────────────────
def _extract_map_ids(text: str) -> list[int]:
    """메시지에서 MAP_ID 숫자를 추출."""
    patterns = [
        r"map[\s_-]?id[\s:=]?\s*(\d+)",
        r"(\d+)\s*번",
        r"#(\d+)",
    ]
    found = set()
    for p in patterns:
        for m in re.findall(p, text, re.IGNORECASE):
            found.add(int(m))
    return list(found)

def _fetch_map_context(map_ids: list[int]) -> str:
    """MAP_ID별 상세 정보 + 로그를 텍스트로 반환."""
    if not map_ids:
        return ""
    lines = ["", "[조회된 MAP_ID 상세 정보]"]
    try:
        all_jobs = {int(j["MAP_ID"]): j for j in get_mig_jobs()}
        for mid in map_ids:
            lines.append(f"\n▶ MAP_ID {mid}")
            job = all_jobs.get(mid)
            if not job:
                lines.append("  - 해당 MAP_ID 없음")
                continue
            lines.append(f"  - 소스→타겟: {job.get('FR_TABLE')} → {job.get('TO_TABLE')}")
            lines.append(f"  - 상태: {job.get('STATUS') or 'NULL'}")
            lines.append(f"  - 재시도: {job.get('RETRY_COUNT')}회, 소요: {job.get('ELAPSED_SECONDS')}초")
            if job.get("MIG_SQL"):
                lines.append(f"  - MIG_SQL: {str(job['MIG_SQL'])[:200]}")
            if job.get("VERIFY_SQL"):
                lines.append(f"  - VERIFY_SQL: {str(job['VERIFY_SQL'])[:200]}")

            logs = get_mig_logs(mid)
            if logs:
                lines.append(f"  - 실행 로그 ({len(logs)}건):")
                for lg in logs[-10:]:  # 최근 10개만
                    lines.append(
                        f"    [{lg.get('LOG_LEVEL','?')}][{lg.get('STEP_NAME','?')}] "
                        f"{str(lg.get('MESSAGE',''))[:150]}"
                    )
            else:
                lines.append("  - 로그 없음")
    except Exception as e:
        lines.append(f"  (조회 오류: {e})")
    return "\n".join(lines)

# ── LLM ───────────────────────────────────────────────────────────────────────
def _system_prompt() -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [
        "당신은 Oracle 데이터 마이그레이션 파이프라인의 운영 어시스턴트입니다.",
        "사용자의 질문에 한국어로 친절하고 간결하게 답변하세요.",
        "숫자나 상태를 물어보면 아래 실시간 DB 데이터를 기반으로 정확히 답변하세요.",
        "",
        f"[현재 시각: {now}]",
        "",
        "[에이전트별 현황]",
    ]
    for label, fn in [
        ("Mig Agent",    get_mig_status_summary),
        ("SQL Agent",    get_sql_status_summary),
        ("Tuning Agent", get_tuning_status_summary),
    ]:
        try:
            s = fn()
            detail = ", ".join(f"{k} {v}건" for k, v in s.items())
            lines.append(f"- {label}: {detail} (합계 {sum(s.values())}건)")
        except Exception:
            lines.append(f"- {label}: 조회 실패")

    try:
        fails = get_recent_fails(5)
        lines.append("")
        if fails:
            lines.append("[최근 실패 작업]")
            for r in fails:
                lines.append(f"- MAP_ID {r['MAP_ID']}: {r['FR_TABLE']} → {r['TO_TABLE']}")
        else:
            lines.append("[최근 실패 작업]: 없음")
    except Exception:
        pass

    return "\n".join(lines)

def _call_llm(chat_messages: list[dict]) -> str:
    api_key  = os.getenv("OPEN_API_KEY") or os.getenv("LLM_API_KEY", "")
    base_url = os.getenv("LLM_BASE_URL",  "")
    model    = os.getenv("LLM_MODEL", "GLM-5.1")
    client   = OpenAI(api_key=api_key, base_url=base_url)

    # 마지막 유저 메시지에서 MAP_ID 감지 → DB 로그 컨텍스트 추가
    last_user = next(
        (m["content"] for m in reversed(chat_messages) if m["role"] == "user"), ""
    )
    map_ids = _extract_map_ids(last_user)
    extra   = _fetch_map_context(map_ids) if map_ids else ""
    system  = _system_prompt() + extra

    full_messages = [{"role": "system", "content": system}] + chat_messages
    resp = client.chat.completions.create(
        model=model, messages=full_messages, temperature=0.7, max_tokens=1500
    )
    return resp.choices[0].message.content.strip()

# ── 오른쪽 상태 패널 ───────────────────────────────────────────────────────────
_ICON = {
    "PASS": "✅",
    "PASS (non-select)": "✅",
    "FAIL": "❌",
    "RUNNING": "🔄",
    "READY": "🔵",
    "SKIP": "⏭️",
    "NA": "🚫",
    "NULL": "⚫",
    "SQL Conversion 단계": "⏳",
    "PENDING": "🟣",
}
_CLR  = {"PASS": "badge-pass", "PASS (non-select)": "badge-pass", "FAIL": "badge-fail"}
_STATUS_ORDER = ["PASS", "PASS (non-select)", "FAIL", "RUNNING", "SKIP", "PENDING", "NULL", "SQL Conversion 단계"]
_PROGRESS_EXCLUDED = {"NA"}

def _norm_status(status) -> str:
    return str(status or "NULL").strip().upper() or "NULL"

def _pct(numerator: int, denominator: int) -> str:
    if denominator <= 0:
        return "-"
    return f"{(numerator / denominator) * 100:.1f}%"

def _sum_excluding(normalized: dict[str, int], excluded: set[str]) -> int:
    return sum(v for k, v in normalized.items() if k not in excluded)

def _dashboard_status(status, title: str = "") -> str | None:
    normalized = _norm_status(status)
    if normalized == "NA":
        return None
    if normalized in {"URGENT", "READY"}:
        return "RUNNING"
    if "Tuning" in title and normalized == "NULL":
        return "SQL Conversion 단계"
    if normalized == "PASS_NON_SELECT":
        return "PASS (non-select)"
    return normalized

def _rate_values(title: str, normalized: dict[str, int]) -> tuple[int, int, int, int]:
    pass_count = normalized.get("PASS", 0)
    pass_non_select_count = normalized.get("PASS (non-select)", 0)
    skip_count = normalized.get("SKIP", 0)
    fail_count = normalized.get("FAIL", 0)

    if "Tuning" in title:
        progress_count = pass_count + pass_non_select_count
        progress_base = _sum_excluding(normalized, {"NA", "NULL", "SQL Conversion 단계"})
    else:
        progress_count = pass_count
        progress_base = _sum_excluding(normalized, _PROGRESS_EXCLUDED)

    success_count = pass_count + pass_non_select_count
    success_base = pass_count + pass_non_select_count + fail_count
    return progress_count, progress_base, success_count, success_base

def _rate_html(title: str, summary: dict) -> str:
    normalized: dict[str, int] = {}
    for k, v in summary.items():
        status = _dashboard_status(k, title)
        if status is None:
            continue
        normalized[status] = normalized.get(status, 0) + int(v or 0)

    progress_count, progress_base, success_count, success_base = _rate_values(title, normalized)
    if "Tuning" in title:
        rate_note = "진척률=PASS 계열/(이전 단계 대기 제외), 성공률=PASS 계열/(PASS 계열+FAIL)"
    else:
        rate_note = "진척률=PASS/(NA 제외), 성공률=PASS/(PASS+FAIL)"

    return f"""
      <div class="rate-grid">
        <div class="rate-box">
          <div class="rate-label">진척률</div>
          <div class="rate-value">{_pct(progress_count, progress_base)}</div>
          <div class="rate-sub">{progress_count}/{progress_base}건</div>
        </div>
        <div class="rate-box">
          <div class="rate-label">성공률</div>
          <div class="rate-value">{_pct(success_count, success_base)}</div>
          <div class="rate-sub">{success_count}/{success_base}건</div>
        </div>
      </div>
      <div class="rate-note">{rate_note}</div>
    """

def _status_card(title: str, summary: dict):
    normalized_summary: dict[str, int] = {}
    for k, v in summary.items():
        status = _dashboard_status(k, title)
        if status is None:
            continue
        normalized_summary[status] = normalized_summary.get(status, 0) + int(v or 0)

    if not normalized_summary:
        st.markdown(f"""
        <div class="stat-card">
          <div class="stat-card-title">{title}</div>
          <span style="color:#9ca3af;font-size:13px">데이터 없음</span>
        </div>""", unsafe_allow_html=True)
        return
    total = sum(normalized_summary.values())
    rows  = ""
    for k, v in sorted(normalized_summary.items(),
                       key=lambda x: _STATUS_ORDER.index(x[0]) if x[0] in _STATUS_ORDER else 99):
        icon  = _ICON.get(k, "◻️")
        cls   = _CLR.get(k, "badge-etc")
        rows += f"""<div class="stat-row">
            <span class="stat-label">{icon} {k}</span>
            <span class="stat-val {cls}">{v}</span>
        </div>"""
    st.markdown(f"""
    <div class="stat-card">
      <div class="stat-card-title">{title} &nbsp;<span style="font-weight:400;color:#adb5bd">총 {total}건</span></div>
      {_rate_html(title, normalized_summary)}
      {rows}
    </div>""", unsafe_allow_html=True)


def _formatting_card(summary: dict):
    total = int(summary.get("TOTAL", 0) or 0)
    applied = int(summary.get("APPLIED", 0) or 0)
    pending = int(summary.get("PENDING", 0) or 0)
    if total <= 0:
        st.markdown("""
        <div class="stat-card">
          <div class="stat-card-title">🧾 Formatting Guide</div>
          <span style="color:#9ca3af;font-size:13px">데이터 없음</span>
        </div>""", unsafe_allow_html=True)
        return

    st.markdown(f"""
    <div class="stat-card">
      <div class="stat-card-title">🧾 Formatting Guide &nbsp;<span style="font-weight:400;color:#adb5bd">총 {total}건</span></div>
      <div class="rate-grid">
        <div class="rate-box">
          <div class="rate-label">적용률</div>
          <div class="rate-value">{_pct(applied, total)}</div>
          <div class="rate-sub">{applied}/{total}건</div>
        </div>
        <div class="rate-box">
          <div class="rate-label">미적용</div>
          <div class="rate-value">{pending}</div>
          <div class="rate-sub">포맷팅 대기</div>
        </div>
      </div>
      <div class="rate-note">적용률=FORMATTED_SQL 값 있음 / TUNED_TEST PASS 계열</div>
      <div class="stat-row">
        <span class="stat-label">✅ 적용</span>
        <span class="stat-val badge-pass">{applied}</span>
      </div>
      <div class="stat-row">
        <span class="stat-label">⚫ 미적용</span>
        <span class="stat-val badge-etc">{pending}</span>
      </div>
    </div>""", unsafe_allow_html=True)

# ── 메인 렌더 ─────────────────────────────────────────────────────────────────
def render():
    st.markdown(CSS, unsafe_allow_html=True)

    # ── 세션 초기화 ──────────────────────────────────────────────────────────
    if "current_chat" not in st.session_state:
        st.session_state.current_chat = _new_chat()
    if "chat_refresh" not in st.session_state:
        st.session_state.chat_refresh = False
    if "chat_pending_response" not in st.session_state:
        st.session_state.chat_pending_response = False
    if "chat_pending_id" not in st.session_state:
        st.session_state.chat_pending_id = None

    chat = st.session_state.current_chat

    # ── 3패널 레이아웃 ────────────────────────────────────────────────────────
    left, center, right = st.columns([1.6, 4, 1.6], gap="medium")

    # ════════════════════════════════════════════════════════════
    # 왼쪽: 대화 목록
    # ════════════════════════════════════════════════════════════
    with left:
        st.markdown("#### 💬 대화 목록")
        if st.button("✏️ 새 대화", width="stretch", type="primary"):
            st.session_state.current_chat = _new_chat()
            st.rerun()

        st.markdown("---")
        chats = _list_chats()
        if not chats:
            st.caption("대화 기록 없음")
        for c in chats:
            label = c.get("title", "대화")[:18]
            is_current = c["id"] == chat["id"]
            col_title, col_del = st.columns([5, 1])
            with col_title:
                if st.button(
                    f"{'▶ ' if is_current else ''}{label}",
                    key=f"chat_{c['id']}",
                    width="stretch",
                    type="primary" if is_current else "secondary",
                ):
                    loaded = _load_chat(c["id"])
                    if loaded:
                        st.session_state.current_chat = loaded
                        st.rerun()
            with col_del:
                if st.button("🗑", key=f"del_{c['id']}", help="삭제"):
                    _delete_chat(c["id"])
                    if is_current:
                        st.session_state.current_chat = _new_chat()
                    st.rerun()

    # ════════════════════════════════════════════════════════════
    # 가운데: 채팅
    # ════════════════════════════════════════════════════════════
    with center:
        st.markdown(f"#### 🤖 Migration 어시스턴트")
        st.caption("파이프라인 상태, 실패 원인, 작업 현황 등 무엇이든 물어보세요.")

        # 메시지 표시
        msg_container = st.container(height=660)
        with msg_container:
            if not chat["messages"]:
                st.markdown("""
                <div style="text-align:center;color:#9ca3af;padding:80px 0 40px 0">
                    <div style="font-size:40px">💬</div>
                    <div style="font-size:15px;margin-top:12px">아래에서 질문을 입력해보세요</div>
                    <div style="font-size:12px;margin-top:8px;color:#d1d5db">
                        예: "현재 실패한 작업은 몇 개야?" · "PASS된 이관 테이블 목록 보여줘"
                    </div>
                </div>
                """, unsafe_allow_html=True)
            for msg in chat["messages"]:
                with st.chat_message(msg["role"]):
                    st.markdown(msg["content"])
            if (
                st.session_state.chat_pending_response
                and st.session_state.chat_pending_id == chat["id"]
            ):
                with st.chat_message("assistant"):
                    placeholder = st.empty()
                    placeholder.markdown("입력중...")
                    try:
                        answer = _call_llm(chat["messages"])
                    except Exception as e:
                        answer = f"⚠️ LLM 호출 실패: {e}"
                    placeholder.markdown(answer)
                chat["messages"].append({"role": "assistant", "content": answer})
                _save_chat(chat)
                st.session_state.current_chat = chat
                st.session_state.chat_pending_response = False
                st.session_state.chat_pending_id = None
                st.rerun()

        # 입력
        user_input = st.chat_input("메시지를 입력하세요...", key="chat_input")

    # ════════════════════════════════════════════════════════════
    # 오른쪽: 에이전트 상태
    # ════════════════════════════════════════════════════════════
    with right:
        rc, rr = st.columns([3, 1])
        with rc:
            st.markdown("#### 📊 현황")
        with rr:
            if st.button("🔄", help="새로고침"):
                st.rerun()

        try:
            _status_card("📦 Mig",    get_mig_status_summary())
        except Exception as e:
            st.error(str(e))
        try:
            _status_card("🔄 SQL",    get_sql_status_summary())
        except Exception as e:
            st.error(str(e))
        try:
            _status_card("⚡ Tuning", get_tuning_status_summary())
        except Exception as e:
            st.error(str(e))
        try:
            _formatting_card(get_formatting_summary())
        except Exception as e:
            st.error(str(e))

    # ── 메시지 처리 (컬럼 밖에서) ─────────────────────────────────────────────
    if user_input and user_input.strip():
        # 유저 메시지 추가
        chat["messages"].append({"role": "user", "content": user_input.strip()})
        if chat["title"] == "새 대화":
            chat["title"] = user_input.strip()[:24]

        _save_chat(chat)
        st.session_state.current_chat = chat
        st.session_state.chat_pending_response = True
        st.session_state.chat_pending_id = chat["id"]
        st.rerun()
