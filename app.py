import streamlit as st

st.set_page_config(layout="wide", page_title="Ultimate Mermaid Architect v3.1")

designer_page = st.Page("designer.py", title="Mermaid Designer", icon="⚡", default=True)
gallery_page = st.Page("gallery.py", title="Diagram Gallery", icon="🗂️")

st.navigation([designer_page, gallery_page]).run()
