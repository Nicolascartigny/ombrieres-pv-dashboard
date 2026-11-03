import streamlit as st
import pandas as pd
from datetime import date
from collect_data import FusionSolarClient, SITES_REF, match_site

# 1. Récupération des secrets Streamlit
fs_user = st.secrets.get("FUSIONSOLAR_USER")
fs_pass = st.secrets.get("FUSIONSOLAR_PASSWORD")
fs_url = st.secrets.get("FUSIONSOLAR_URL", "https://eu5.fusionsolar.huawei.com")

@st.cache_data(ttl=600) # Cache de 10 minutes (600 secondes) pour préserver les quotas de l'API
def get_courbes_du_jour():
    if not fs_user or not fs_pass:
        return pd.DataFrame()
        
    client = FusionSolarClient(fs_url, fs_user, fs_pass)
    if not client.login():
        return pd.DataFrame()
        
    # 2. Récupérer les codes des centrales dynamiquement (comme dans ta collecte)
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
        
    # 3. Interroger l'API pour les données horaires du jour
    aujourdhui = date.today()
    data_brute = client.get_hourly_kpi(list(mapping.keys()), aujourdhui)
    
    # 4. Formater les données pour Streamlit
    lignes = []
    for item in data_brute:
        code = item.get("stationCode") or item.get("plantCode")
        if code not in mapping:
            continue
            
        temps = pd.to_datetime(item.get("collectTime"), unit="ms", utc=True).tz_convert("Europe/Paris")
        map_data = item.get("dataItemMap", {})
        
        # Selon ce que renvoie l'API, cherche la puissance de l'onduleur ou la puissance active
        puissance = map_data.get("inverter_power") or map_data.get("active_power") or 0.0
        
        lignes.append({
            "Site": mapping[code],
            "Heure": temps,
            "Puissance (kW)": float(puissance)
        })
        
    return pd.DataFrame(lignes)