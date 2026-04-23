"""Stage 10 — Editor Review UI.

Streamlit app for reviewing, editing, and approving/rejecting LLM-generated
draft newsfeed items before publication.

Inputs:  SQLite drafts + candidates + context_bundles tables
Outputs: Updated editor_decision / edited_text, candidate.status transitions

Usage:
    streamlit run ui/streamlit_app.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Ensure src/ is on sys.path when launched from project root or ui/
_src = Path(__file__).resolve().parents[1] / "src"
if str(_src) not in sys.path:
    sys.path.insert(0, str(_src))

import streamlit as st

from newsfeed.config import get_settings
from newsfeed.db import AuditLog, Candidate, ContextBundle, Draft, get_session, init_db

# ── page config ────────────────────────────────────────────────────────────

st.set_page_config(
    page_title="Newsfeed Editor Review",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown(
    """
    <style>
    div[data-testid="stExpander"] > div:first-child {font-weight:600;}
    .conf-pill {
        display:inline-block; padding:3px 10px; border-radius:20px;
        font-size:0.82em; font-weight:700; margin-left:6px;
    }
    .conf-high {background:#c8e6c9; color:#1b5e20;}
    .conf-med  {background:#fff9c4; color:#f57f17;}
    .conf-low  {background:#ffcdd2; color:#b71c1c;}
    .decision-pill {
        display:inline-block; padding:2px 10px; border-radius:20px;
        font-size:0.78em; font-weight:600; margin-left:4px;
    }
    .dp-pending  {background:#ffe0b2; color:#e65100;}
    .dp-approve  {background:#c8e6c9; color:#1b5e20;}
    .dp-edit     {background:#bbdefb; color:#0d47a1;}
    .dp-reject   {background:#ffcdd2; color:#b71c1c;}
    </style>
    """,
    unsafe_allow_html=True,
)


# ── helpers ────────────────────────────────────────────────────────────────


def _conf_pill(conf: float | None) -> str:
    if conf is None:
        return ""
    pct = f"{conf:.0%}"
    cls = "conf-high" if conf >= 0.85 else "conf-med" if conf >= 0.70 else "conf-low"
    return f'<span class="conf-pill {cls}">{pct}</span>'


def _decision_pill(decision: str) -> str:
    icons = {"pending": "⏳", "approve": "✅", "edit": "✏️", "reject": "❌"}
    icon = icons.get(decision, "")
    return f'<span class="decision-pill dp-{decision}">{icon} {decision.upper()}</span>'


def _set_decision(
    draft_id: int,
    decision: str,
    edited_text: str | None = None,
) -> None:
    """Write editor decision + optional edited text to DB, then rerun."""
    with get_session() as session:
        draft = session.get(Draft, draft_id)
        if not draft:
            return
        draft.editor_decision = decision
        if edited_text is not None:
            draft.edited_text = edited_text
        candidate = session.get(Candidate, draft.candidate_id)
        if candidate:
            status_map = {
                "approve": "approved",
                "reject": "rejected",
                "edit": "review",
                "pending": "validated",
            }
            candidate.status = status_map.get(decision, candidate.status)
        session.add(
            AuditLog(
                stage="stage10_review",
                candidate_id=draft.candidate_id,
            )
        )
    st.cache_data.clear()
    st.rerun()


# ── data loaders ──────────────────────────────────────────────────────────


@st.cache_data(ttl=10)
def _load_metrics() -> dict:
    init_db()
    with get_session() as session:
        total = session.query(Draft).count()
        pending = session.query(Draft).filter(Draft.editor_decision == "pending").count()
        approved = session.query(Draft).filter(Draft.editor_decision == "approve").count()
        rejected = session.query(Draft).filter(Draft.editor_decision == "reject").count()
        edited = session.query(Draft).filter(Draft.editor_decision == "edit").count()
        sections = sorted(
            r[0]
            for r in session.query(Draft.section).distinct().all()
            if r[0]
        )
        jurisdictions = sorted(
            r[0]
            for r in session.query(Draft.jurisdiction).distinct().all()
            if r[0]
        )
    return {
        "total": total,
        "pending": pending,
        "approved": approved,
        "rejected": rejected,
        "edited": edited,
        "sections": sections,
        "jurisdictions": jurisdictions,
    }


@st.cache_data(ttl=10)
def _load_drafts(
    section_f: str,
    juris_f: str,
    decision_f: str,
) -> list[dict]:
    init_db()
    rows: list[dict] = []
    with get_session() as session:
        q = session.query(Draft).join(Candidate, Draft.candidate_id == Candidate.id)
        if section_f != "All":
            q = q.filter(Draft.section == section_f)
        if juris_f != "All":
            q = q.filter(Draft.jurisdiction == juris_f)
        if decision_f != "All":
            q = q.filter(Draft.editor_decision == decision_f)
        q = q.order_by(Draft.section, Draft.jurisdiction, Draft.id)
        for d in q.all():
            cand = session.get(Candidate, d.candidate_id)
            rows.append(
                {
                    "id": d.id,
                    "section": d.section or "—",
                    "jurisdiction": d.jurisdiction or "—",
                    "content_type": d.content_type or "—",
                    "confidence": d.confidence,
                    "draft_text": d.draft_text or "",
                    "edited_text": d.edited_text,
                    "editor_decision": d.editor_decision or "pending",
                    "validation_errors": (
                        json.loads(d.validation_errors_json)
                        if d.validation_errors_json
                        else []
                    ),
                    "created_at": d.created_at.strftime("%Y-%m-%d %H:%M") if d.created_at else "",
                    "url": cand.url if cand else None,
                    "title": cand.title if cand else None,
                    "source_label": cand.source_label if cand else None,
                    "pub_date": (
                        cand.pub_date.strftime("%Y-%m-%d") if cand and cand.pub_date else None
                    ),
                }
            )
    return rows


# ── draft card renderer ────────────────────────────────────────────────────


def _render_draft_card(draft: dict) -> None:
    draft_id = draft["id"]
    decision = draft["editor_decision"]
    conf = draft["confidence"]

    edit_key = f"edit_mode_{draft_id}"
    if edit_key not in st.session_state:
        st.session_state[edit_key] = False

    # Expander title: jurisdiction · content_type · decision pill
    expander_label = (
        f"{draft['jurisdiction']}  ·  {draft['content_type']}"
        f"  ·  {decision.upper()}"
        + (f"  ·  {conf:.0%}" if conf is not None else "")
    )
    # Expand pending items by default so editors can act immediately
    is_pending = decision == "pending"

    with st.expander(expander_label, expanded=is_pending):

        # ── top row: source link + confidence ──
        top_l, top_r = st.columns([5, 1])
        with top_l:
            if draft["url"]:
                label = draft["title"] or draft["url"]
                st.markdown(f"**Source:** [{label}]({draft['url']})")
            if draft["source_label"]:
                st.caption(f"Agency / label: {draft['source_label']}")
            if draft["pub_date"]:
                st.caption(f"Published: {draft['pub_date']}")
            st.caption(f"Draft created: {draft['created_at']}")
        with top_r:
            if conf is not None:
                color = (
                    "#1b5e20" if conf >= 0.85 else "#e65100" if conf >= 0.70 else "#b71c1c"
                )
                st.markdown(
                    f'<div style="text-align:center;padding-top:4px">'
                    f'<span style="font-size:1.8em;font-weight:700;color:{color}">{conf:.0%}</span>'
                    f'<br/><small style="color:#666">confidence</small></div>',
                    unsafe_allow_html=True,
                )

        st.divider()

        # ── validation errors ──
        if draft["validation_errors"]:
            with st.container(border=True):
                st.warning("Validation issues — review before approving")
                for err in draft["validation_errors"]:
                    st.markdown(f"- {err}")

        # ── draft text ──
        st.markdown("**Draft text**")

        if st.session_state[edit_key]:
            # Edit mode
            current_text = draft["edited_text"] or draft["draft_text"]
            edited = st.text_area(
                "Edit draft text",
                value=current_text,
                height=220,
                key=f"textarea_{draft_id}",
                label_visibility="collapsed",
            )
            e1, e2, _ = st.columns([1, 1, 4])
            with e1:
                if st.button(
                    "Save & Approve",
                    key=f"save_approve_{draft_id}",
                    type="primary",
                    use_container_width=True,
                ):
                    st.session_state[edit_key] = False
                    _set_decision(draft_id, "approve", edited_text=edited)
            with e2:
                if st.button(
                    "Cancel",
                    key=f"cancel_{draft_id}",
                    use_container_width=True,
                ):
                    st.session_state[edit_key] = False
                    st.rerun()

        else:
            # Read mode — render draft as markdown inside a bordered container
            display_text = draft["edited_text"] or draft["draft_text"]
            if draft["edited_text"]:
                st.caption("_(showing edited version)_")
            with st.container(border=True):
                st.markdown(display_text)

            st.markdown("")  # spacer

            # Action buttons
            if decision == "pending":
                b1, b2, b3, _ = st.columns([1, 1, 1, 3])
                with b1:
                    if st.button(
                        "Approve",
                        key=f"approve_{draft_id}",
                        type="primary",
                        use_container_width=True,
                    ):
                        _set_decision(draft_id, "approve")
                with b2:
                    if st.button(
                        "Edit",
                        key=f"edit_btn_{draft_id}",
                        use_container_width=True,
                    ):
                        st.session_state[edit_key] = True
                        st.rerun()
                with b3:
                    if st.button(
                        "Reject",
                        key=f"reject_{draft_id}",
                        use_container_width=True,
                    ):
                        _set_decision(draft_id, "reject")
            else:
                # Already decided — offer undo / re-edit
                u1, u2, _ = st.columns([1, 1, 4])
                with u1:
                    if st.button(
                        "Undo",
                        key=f"undo_{draft_id}",
                        use_container_width=True,
                    ):
                        _set_decision(draft_id, "pending")
                with u2:
                    if st.button(
                        "Re-edit",
                        key=f"reedit_{draft_id}",
                        use_container_width=True,
                    ):
                        st.session_state[edit_key] = True
                        st.rerun()


# ── main ───────────────────────────────────────────────────────────────────


def main() -> None:
    settings = get_settings()

    # ── sidebar ──
    st.sidebar.title("Newsfeed Editor")
    st.sidebar.caption(f"DB: `{settings.paths.db.name}`")
    st.sidebar.divider()

    metrics = _load_metrics()

    st.sidebar.subheader("Filters")
    section_filter = st.sidebar.selectbox(
        "Section", ["All"] + metrics["sections"], key="section_f"
    )
    juris_filter = st.sidebar.selectbox(
        "Jurisdiction", ["All"] + metrics["jurisdictions"], key="juris_f"
    )
    decision_filter = st.sidebar.selectbox(
        "Decision",
        ["All", "pending", "approve", "edit", "reject"],
        key="decision_f",
    )

    st.sidebar.divider()
    st.sidebar.subheader("Summary")
    st.sidebar.metric("Total drafts", metrics["total"])
    st.sidebar.metric("Pending review", metrics["pending"])
    st.sidebar.metric("Approved", metrics["approved"])
    st.sidebar.metric("Rejected", metrics["rejected"])

    st.sidebar.divider()
    if st.sidebar.button("Refresh", use_container_width=True):
        st.cache_data.clear()
        st.rerun()

    # ── header ──
    st.title("Regulatory Newsfeed — Editor Review")

    # Top metrics
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Total", metrics["total"])
    m2.metric("Pending", metrics["pending"])
    m3.metric("Approved", metrics["approved"])
    m4.metric("Rejected", metrics["rejected"])

    if metrics["total"] == 0:
        st.info("No drafts in the database yet. Run the pipeline first.")
        st.code("python main.py run --lookback-days 21", language="bash")
        return

    # Approve-all shortcut for pending batch
    pending_drafts_exist = metrics["pending"] > 0
    if pending_drafts_exist and decision_filter in ("All", "pending"):
        st.divider()
        col_aa, col_info = st.columns([2, 5])
        with col_aa:
            if st.button(
                f"Approve all {metrics['pending']} pending drafts",
                type="primary",
            ):
                with get_session() as session:
                    pending = (
                        session.query(Draft)
                        .filter(Draft.editor_decision == "pending")
                        .all()
                    )
                    for d in pending:
                        d.editor_decision = "approve"
                        cand = session.get(Candidate, d.candidate_id)
                        if cand:
                            cand.status = "approved"
                st.cache_data.clear()
                st.rerun()
        with col_info:
            st.caption(
                "Approves every pending draft without edits. "
                "Use only when you have reviewed them all above."
            )

    st.divider()

    # ── load and render drafts ──
    drafts = _load_drafts(section_filter, juris_filter, decision_filter)

    if not drafts:
        st.info("No drafts match the selected filters.")
        return

    st.caption(f"Showing **{len(drafts)}** draft(s)")

    # Group by section for clarity
    current_section: str | None = None
    for draft in drafts:
        if draft["section"] != current_section:
            current_section = draft["section"]
            st.subheader(current_section)
        _render_draft_card(draft)


if __name__ == "__main__":
    main()
