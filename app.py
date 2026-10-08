import streamlit as st

st.set_page_config(layout="wide", page_title="GalleryEd v3.1")

designer_page = st.Page("designer.py", title="Mermaid Designer", icon="⚡", default=True)
gallery_page = st.Page("gallery.py", title="Gallery", icon="🗂️")

st.navigation([designer_page, gallery_page]).run()
