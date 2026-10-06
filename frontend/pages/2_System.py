"""System status: /health of every service and the catalogue sync queue."""

from datetime import datetime

import streamlit as st

import api

st.set_page_config(page_title="System · FashionRec", page_icon="🩺", layout="wide")
st.title("🩺 System status")

if st.button("🔄 Refresh"):
    st.rerun()

st.subheader("Services")
services = api.health_all()
cols = st.columns(len(services))
for col, s in zip(cols, services):
    with col.container(border=True):
        st.markdown(f"### {'✅' if s['ok'] else '❌'} {s['service']}")
        if s["error"]:
            st.caption(f"Unreachable: {s['error']}")
        elif s["ok"]:
            st.caption("Healthy")
        failing = [name for name, ok in s["checks"].items() if not ok]
        for name in failing:
            st.markdown(f"- ❌ `{name}`")
        passing = [name for name, ok in s["checks"].items() if ok]
        if passing:
            st.caption("OK: " + ", ".join(passing))

healthy = sum(s["ok"] for s in services)
if healthy == len(services):
    st.success("All services are healthy.")
else:
    st.warning(f"{len(services) - healthy} of {len(services)} services need attention. "
               "Search still works without query (rules fallback) and rerank (retrieval order); "
               "it needs gateway and retrieval.")

st.subheader("Catalogue sync")
try:
    waiting = api.queue_length()
    st.metric("Jobs waiting in `catalog_sync`", waiting)
    st.caption("Normally 0: the worker picks up each webhook change within about a second.")
except api.ApiError as err:
    st.error(err.message)

st.caption(f"Checked at {datetime.now():%H:%M:%S}. All browser users share this frontend's IP, "
           "so they share the gateway's limit of 30 searches per minute.")
