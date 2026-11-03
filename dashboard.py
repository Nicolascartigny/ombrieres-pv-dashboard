"""Tableau de bord Streamlit : suivi des 6 ombrières photovoltaïques Mauges Énergies."""
import os
import sqlite3
from contextlib import closing
from datetime import date

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from config import (DB_PATH, OBJECTIF_ANNUEL_KWH, PUISSANCE_ANNONCEE_KWC,
                    SITES_REF, TOTAL_KWC)
from collect_data import FusionSolarClient, match_site

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
    g["kwh_kwc_j"] = g["kwh_kwc"] / g["jours"]   # moyenne par jour de production : comparable entre sites
    return g


# =====================================================================
# FONCTIONS DE RÉCUPÉRATION EN DIRECT (API FUSIONSOLAR)
# =====================================================================
fs_user = st.secrets.get("FUSIONSOLAR_USER", os.getenv("FUSIONSOLAR_USER"))
fs_pass = st.secrets.get("FUSIONSOLAR_PASSWORD", os.getenv("FUSIONSOLAR_PASSWORD"))
fs_url = st.secrets.get("FUSIONSOLAR_URL", "https://eu5.fusionsolar.huawei.com")

@st.cache_data(ttl=600)
def get_courbes_du_jour():
    """Interroge FusionSolar pour récupérer la puissance heure par heure de la journée en cours."""
    if not fs_user or not fs_pass:
        return pd.DataFrame()
        
    client = FusionSolarClient(fs_url, fs_user, fs_pass)
    if not client.login():
        return pd.DataFrame()
        
    stations = client.get_stations()
    mapping = {}
    for s in stations:
        code = s.get("plantCode") or s.get("stationCode")
        nom = s.get("plantName") or s.get("stationName")
        key = match_site(nom)
        if key and code:
            mapping[code] = SITES_REF[key]["nom"]
            
    if not mapping:
        return pd.DataFrame()
        
    aujourdhui = date.today()
    data_brute = client.get_hourly_kpi(list(mapping.keys()), aujourdhui)
    
    lignes = []
    for item in data_brute:
        code = item.get("stationCode") or item.get("plantCode")
        if code not in mapping:
            continue
            
        temps = pd.to_datetime(item.get("collectTime"), unit="ms", utc=True).tz_convert("Europe/Paris")
        map_data = item.get("dataItemMap", {})
        
        puissance = map_data.get("inverter_power") or map_data.get("active_power") or 0.0
        
        lignes.append({
            "Site": mapping[code],
            "Heure": temps,
            "Puissance (kW)": float(puissance)
        })
        
    return pd.DataFrame(lignes)


@st.cache_data(ttl=300)
def get_alarmes_actives():
    """Récupère les alarmes/défauts actifs en temps réel sur les centrales."""
    if not fs_user or not fs_pass:
        return []
        
    client = FusionSolarClient(fs_url, fs_user, fs_pass)
    if not client.login():
        return []
        
    try:
        # Appel de l'endpoint d'alarmes actives de l'API Northbound
        response = client._post("getAlarmList", {"status": 1}) # 1 = alarmes actives
        return response.get("data", [])
    except Exception:
        return []


# =====================================================================
# INTERFACE PRINCIPALE
# =====================================================================
st.title("⚡ Suivi & performance des ombrières photovoltaïques")
st.caption(f"Mauges Énergies · 6 sites · {PUISSANCE_ANNONCEE_KWC} kWc · objectif {fmt(OBJECTIF_ANNUEL_KWH / 1000)} MWh/an")

df = load_data()
if df.empty:
    st.warning("Aucune donnée. Lancez d'abord `python collect_data.py` (mode démonstration sans identifiants).")
    st.stop()

df = df[df["site_key"].isin(SITES_REF)].copy()
df["site"] = df["site_key"].map(lambda k: SITES_REF[k]["nom"])
df["kwc"] = df["site_key"].map(lambda k: SITES_REF[k]["kwc"])

OBJECTIFS_PAR_SITE = {
    "La Loge (BEM)": 133,
    "Mauges Communauté (MC)": 121,
    "Les Minières (MSE)": 207,
    "Complexe Sportif (SEV)": 105,
    "La Jumellière (CEA)": 290,
    "Pôle Santé (ODA)": 198
}

with st.sidebar:
    annees = sorted(df["date"].dt.year.unique(), reverse=True)
    annee = st.selectbox("Année", annees)
    if st.button("Rafraîchir les données"):
        st.cache_data.clear()
        st.rerun()

dfy = df[df["date"].dt.year == annee]
sites_actifs = dfy["site"].unique()
objectif_annuel_mwh = sum(OBJECTIFS_PAR_SITE.get(site, 0) for site in sites_actifs)
OBJECTIF_ANNUEL_KWH = objectif_annuel_mwh * 1000

if (dfy["source"] == "mock").any():
    st.info("Données de démonstration (mode MOCK) : renseignez les identifiants FusionSolar pour les données réelles.")

# ---------------------------------------------------------------- KPI & MAINTENANCE (1 et 2)
dernier_jour = dfy["date"].max()
jour_df = dfy[dfy["date"] == dernier_jour]
prod_jour = jour_df["kwh_produits"].sum()
kwc_jour = jour_df["kwc"].sum()
mois_df = dfy[dfy["date"].dt.month == dernier_jour.month]
prod_mois = mois_df["kwh_produits"].sum()
kwc_mois = mois_df.drop_duplicates("site_key")["kwc"].sum()
perf_mois = perf_par_site(mois_df)
meilleur = perf_mois.loc[perf_mois["kwh_kwc_j"].idxmax()]
total_annee = dfy["kwh_produits"].sum()
taux = total_annee / OBJECTIF_ANNUEL_KWH

# Calcul de l'Indice de Performance global (Ratio kWh/kWc moyen du mois en cours vs référence)
pr_moyen = (prod_mois / kwc_mois) if kwc_mois > 0 else 0

k1, k2, k3, k4 = st.columns(4)
k1.metric(f"Production du jour ({dernier_jour:%d/%m})", f"{fmt(prod_jour)} kWh",
          f"{fmt(prod_jour / kwc_jour, 2)} kWh/kWc", delta_color="off")
k2.metric(f"Production de {MOIS[dernier_jour.month - 1].lower()}", f"{fmt
