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
from collect_data import match_site

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
# FONCTIONS DE RÉCUPÉRATION EN DIRECT (API FUSIONSOLAR AUTONOME)
# =====================================================================
FS_USER = st.secrets.get("FUSIONSOLAR_USER", os.getenv("FUSIONSOLAR_USER"))
FS_PASS = st.secrets.get("FUSIONSOLAR_PASSWORD", os.getenv("FUSIONSOLAR_PASSWORD"))
FS_URL = "https://eu5.fusionsolar.huawei.com"


@st.cache_data(ttl=300)
def get_courbe_horaire(target_date: date):
    """Récupère la puissance en vrai temps réel (5 min) en interrogeant directement les onduleurs."""
    if not FS_USER or not FS_PASS:
        return pd.DataFrame(), "Identifiants FusionSolar introuvables dans les secrets Streamlit."

    session = requests.Session()
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json"
    }

    try:
        # 1. Connexion / Login
        login_resp = session.post(
            f"{FS_URL}/thirdData/login",
            headers=headers,
            json={"userName": FS_USER, "systemCode": FS_PASS},
            timeout=15
        )

        try:
            login_data = login_resp.json()
        except ValueError:
            return pd.DataFrame(), "Le serveur a renvoyé du texte/HTML au lieu de JSON."

        token = (login_resp.headers.get("xsrf-token") or 
                 login_resp.headers.get("X-SRX-TOKEN") or 
                 session.cookies.get("XSRF-TOKEN"))

        if not login_data.get("success"):
            return pd.DataFrame(), f"Connexion refusée par Huawei. (Retour : {login_data})"
            
        if not token:
            return pd.DataFrame(), "Login réussi mais jeton introuvable."

        auth_headers = {**headers, "X-SRX-TOKEN": token, "xsrf-token": token}

        # 2. Découverte des centrales
        dev_resp = session.post(f"{FS_URL}/thirdData/getStationList", headers=auth_headers, json={}, timeout=15)
        stations = dev_resp.json().get("data", [])
        stations_map = {}
        for s in stations:
            code = s.get("plantCode") or s.get("stationCode")
            nom = s.get("plantName") or s.get("stationName") or ""
            key = match_site(nom)
            if key and code:
                stations_map[code] = SITES_REF[key]["nom"]

        if not stations_map:
            return pd.DataFrame(), "Aucune centrale reconnue."

        # 3. Découverte automatique des onduleurs sur ces centrales
        inv_resp = session.post(
            f"{FS_URL}/thirdData/getDevList", 
            headers=auth_headers, 
            json={"stationCodes": ",".join(stations_map.keys())}, 
            timeout=15
        )
        devices = inv_resp.json().get("data", [])
        
        inverters = {}
        for dev in devices:
            dev_type = dev.get("devTypeId")
            # devTypeId 1 = String Inverter, 38 = Micro-onduleur
            if dev_type in [1, 2, 38]:
                inverters[dev.get("id")] = {
                    "stationCode": dev.get("stationCode"),
                    "devTypeId": dev_type
                }
                
        if not inverters:
            return pd.DataFrame(), "Aucun onduleur détecté sur les centrales."

        # 4. Récupération de l'historique 5-min par onduleur avec DIAGNOSTIC
        ts = datetime(target_date.year, target_date.month, target_date.day, 0, 0, 0, tzinfo=TZ)
        start_ms = int(ts.timestamp() * 1000)
        end_ms = int(datetime.now(TZ).timestamp() * 1000)
        
        all_history = []
        types_to_query = set(v["devTypeId"] for v in inverters.values())
        
        for dtype in types_to_query:
            ids = [str(k) for k, v in inverters.items() if v["devTypeId"] == dtype]
            
            for i in range(0, len(ids), 90):
                chunk = ids[i:i+90]
                hist_resp = session.post(
                    f"{FS_URL}/thirdData/getDevHistory",
                    headers=auth_headers,
                    json={
                        "devIds": ",".join(chunk), 
                        "devTypeId": dtype, 
                        "startTime": start_ms, 
                        "endTime": end_ms
                    },
                    timeout=25
                )
                
                hist_json = hist_resp.json()
                
                # DIAGNOSTIC : On vérifie si Huawei bloque spécifiquement cette requête
                if not hist_json.get("success"):
                    return pd.DataFrame(), f"Refus API sur les onduleurs (Code {hist_json.get('failCode')}) : {hist_json.get('message', hist_json)}"
                
                all_history.extend(hist_json.get("data", []))

        if not all_history:
            return pd.DataFrame(), "L'API a répondu 'succès', mais le tableau 'data' est totalement vide pour vos onduleurs."

        # 5. Traitement et Aggrégation des données
        records = []
        for item in all_history:
            dev_id = item.get("devId")
            if dev_id not in inverters:
                continue
                
            st_code = inverters[dev_id]["stationCode"]
            site_nom = stations_map.get(st_code, st_code)
            
            collect_time = item.get("collectTime", 0)
            data_map = item.get("dataItemMap", {})
            
            # Recherche de la puissance sous plusieurs noms possibles
            puissance = data_map.get("active_power")
            if puissance is None:
                puissance = data_map.get("inverter_power")
                
            if puissance is not None:
                records.append({
                    "site": site_nom,
                    "collectTime": collect_time,
                    "puissance_kw": float(puissance)
                })

        if not records:
            return pd.DataFrame(), f"Les onduleurs ont bien renvoyé un historique, mais la variable de puissance (active_power) est introuvable. Variables trouvées : {list(all_history[0].get('dataItemMap', {}).keys())}"

        df_inv = pd.DataFrame(records)
        
        # Convertir le timestamp en heure française et arrondir aux 5 minutes
        df_inv["heure_dt"] = pd.to_datetime(df_inv["collectTime"], unit="ms", utc=True).dt.tz_convert(TZ)
        df_inv["heure_dt"] = df_inv["heure_
