"""Editor review UI stub.

Real implementation (Stage 10) lands later. For now this just confirms
the Streamlit server starts and can read the pipeline DB.
"""

from __future__ import annotations

import streamlit as st

from newsfeed.config import get_settings
from newsfeed.db import Candidate, get_session, init_db


def main() -> None:
    st.set_page_config(page_title="Regulatory Newsfeed — Editor", layout="wide")
    st.title("Regulatory Newsfeed — Editor Review")
    st.caption("Stage 10 UI stub. Drafts and approval actions wire in later.")

    settings = get_settings()
    st.sidebar.subheader("Settings")
    st.sidebar.write(f"Lookback: **{settings.pipeline.lookback_days}** days")
    st.sidebar.write(f"Confidence threshold: **{settings.pipeline.confidence_threshold}**")
    st.sidebar.write(f"DB: `{settings.paths.db}`")

    init_db()
    with get_session() as session:
        total = session.query(Candidate).count()
        in_review = (
            session.query(Candidate).filter(Candidate.status == "review").count()
        )
    st.metric("Candidates (total)", total)
    st.metric("Awaiting review", in_review)


if __name__ == "__main__":
    main()
