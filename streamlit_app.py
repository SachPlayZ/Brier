"""Brier's Streamlit Community Cloud entrypoint."""

from __future__ import annotations

from importlib import import_module

import streamlit as st

from streamlit_ui.runtime import boot

st.set_page_config(
    page_title="Brier | Expense control",
    page_icon=":material/receipt_long:",
    layout="wide",
    initial_sidebar_state="expanded",
)


@st.cache_resource(show_spinner="Preparing the secure workspace…")
def initialize() -> bool:
    boot(st.secrets)
    return True


initialize()
close_old_connections = import_module("django.db").close_old_connections
close_old_connections()
try:
    import_module("streamlit_ui.app").main()
finally:
    close_old_connections()
