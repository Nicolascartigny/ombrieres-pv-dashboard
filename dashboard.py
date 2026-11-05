"""Tableau de bord Streamlit : suivi des 6 ombrières photovoltaïques Mauges Énergies."""
import os
import sqlite3
from contextlib import closing
from datetime import datetime, date
from zoneinfo import ZoneInfo

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import requests
import streamlit as st

from config import (DB_PATH, OBJECTIF_ANNUEL_KWH, PUISSANCE_ANNONCEE_KWC,
                    SITES_REF, TOTAL_KWC)
from collect_data import FusionSolarClient, match_site

TZ = ZoneInfo("Europe/Paris")
MOIS = ["Janv.", "Févr.", "Mars", "Avr.", "Mai", "Juin",
        "Juil.", "Août", "Sept.", "Oct.", "Nov.", "Déc."]
COULEURS = dict(zip([s["nom"] for s in SITES_REF.values()], px.colors.qualitative.Set2))

st.set_page_config(page_title="Suivi Ombrières PV", page_icon="⚡", layout="wide")


def fmt(x, nd=0):
    return f"{x:,.{nd}f}".replace(",", "\u00a0").replace(".", ",")


@st.cache_data(ttl=600)
def load_data():
    if not DB_PATH.exists():
        return pd.DataFrame()
    with closing(sqlite3.connect(DB_PATH)) as conn:
        return pd.read_sql_query("SELECT * FROM production_journaliere", conn, parse_dates=["date"])


def perf_par_site(d):
    g = d.groupby("site", as_index=False).agg(kwh=("kwh_produits", "sum"), kwc=("kwc", "first"),
                                              jours=("date", "nunique"))
    g["kwh_kwc"] = g["kwh"] / g["kwc"]
    g["kwh_kwc_j"] = g["kwh_kwc"] / g["jours"]
    return g


# =====================================================================
# FONCTIONS DE RÉCUPÉRATION EN DIRECT (API FUSIONSOLAR)
# =====================================================================
FS_USER = st.secrets.get("FUSIONSOLAR_USER", os.getenv("FUSIONSOLAR_USER"))
FS_PASS = st.secrets.get("FUSIONSOLAR_PASSWORD", os.getenv("FUSIONSOLAR_PASSWORD"))
FS_URL = st.secrets.get("FUSIONSOLAR_URL", os.getenv("FUSIONSOLAR_URL", "https://eu5.fusionsolar.huawei.com")).rstrip("/")


@st.cache_data(ttl=600)
def get_courbe_horaire(target_date: date):
    """Interroge l'API FusionSolar pour récupérer la puissance heure par heure à la date choisie."""
    if not FS_USER or not FS_PASS:
        return pd.DataFrame(), "Identifiants FusionSolar introuvables dans les secrets."

    session = requests.Session()
    try:
        login_resp = session.post(
            f"{FS_URL}/thirdData/login",
            json={"userName": FS_USER, "systemCode": FS_PASS},
            timeout=15
        )
        login_data = login_resp.json()
        if not (login_
