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
FS_URL = st.secrets.get("FUSIONSOLAR_URL", "https://eu5.fusionsolar.huawei.com")

@st.cache_data(ttl=600)
def get_courbe_horaire(target_date: date):
    """Interroge l'API FusionSolar pour récupérer la puissance heure par heure à la date choisie."""
    if not FS_USER or not FS_PASS:
        return pd.DataFrame()
    
    session = requests.Session()
    try:
        login_resp = session.post(
            f"{FS_URL}/thirdData/login",
            json={"userName": FS_USER, "systemCode": FS_PASS},
            timeout=10
        )
        token = login_resp.headers.get("xsrf-token") or login_resp.headers.get("X-SRX-TOKEN") or session.cookies.get("XSRF-TOKEN")
        
        client = FusionSolarClient(FS_URL, FS_USER, FS_PASS)
        if not client.login():
            return pd.DataFrame()
        
        stations = client.get_stations()
        STATIONS_MAP = {}
        for s in stations:
            code = s.get("plantCode") or s.get("stationCode")
            nom = s.get("plantName") or s.get("stationName")
            key = match_site(nom)
            if key and code:
                STATIONS_MAP[code] = SITES_REF[key]["nom"]

        if not STATIONS_MAP:
            return pd.DataFrame()

        ts = datetime(target_date.year, target_date.month, target_date.day, 12, tzinfo=TZ)
        collect_time_ms = int(ts.timestamp() * 1000)
        station_codes = list(STATIONS_MAP.keys())

        kpi_resp = session.post(
            f"{FS_URL}/thirdData/getKpiStationHour",
            headers={"X-SRX-TOKEN": token if token else "", "xsrf-token": token if token else ""},
            json={"stationCodes": ",".join(station_codes), "collectTime": collect_time_ms},
            timeout=10
        )
        data = kpi_resp.json().get("data", [])
        lignes = []
        for item in data:
            code = item.get("stationCode") or item.get("plantCode")
            site_nom = STATIONS_MAP.get(code, code)
            data_map = item.get("dataItemMap", {})
            collect_dt = datetime.fromtimestamp(item.get("collectTime", 0) / 1000, tz=TZ)
            puissance = data_map.get("inverter_power", 0) or data_map.get("active_power", 0) or 0.0
            lignes.append({
                "site": site_nom,
                "heure": collect_dt.strftime("%H:%M"),
                "puissance_kw": float(puissance)
            })
        return pd.DataFrame(lignes)
    except Exception:
        return pd.DataFrame()


@st.cache_data(ttl=300)
def get_alarmes_actives():
    """Récupère les alarmes/défauts actifs en temps réel sur les centrales."""
    if not FS_USER or not FS_PASS:
        return []
    client = FusionSolarClient(FS_URL, FS_USER, FS_PASS)
    if not client.login():
        return []
    try:
        response = client._post("getAlarmList", {"status": 1})
        return response.get("data", [])
    except Exception:
        return []


# =====================================================================
# INTERFACE PRINCIPALE
# =====================================================================
st.title("⚡ Suivi & performance des ombrières photovoltaïques")
st.caption(f"Mauges Énergies · 6 sites · {PUISSANCE_ANNONCEE_KWC} kWc")

df = load_data()
if df.empty:
    st.warning("Aucune donnée. Lancez d'abord `python collect_data.py` (mode démonstration sans identifiants).")
    st.stop()

df = df[df["site_key"].isin(SITES_REF)].copy()
df["site"] = df["site_
