import os
import pandas as pd
import streamlit as st
from datetime import date

# Import des outils que tu as déjà codés dans collect_data.py
from collect_data import FusionSolarClient, SITES_REF, match_site

# =====================================================================
# 1. FONCTION DE RÉCUPÉRATION EN DIRECT (À mettre avant l'affichage)
# =====================================================================

# Récupération des identifiants stockés dans les secrets de Streamlit Cloud
fs_user = st.secrets.get("FUSIONSOLAR_USER")
fs_pass = st.secrets.get("FUSIONSOLAR_PASSWORD")
fs_url = st.secrets.get("FUSIONSOLAR_URL", "https://eu5.fusionsolar.huawei.com")

@st.cache_data(ttl=600) # Garde les données en mémoire 10 minutes pour ne pas saturer l'API
def get_courbes_du_jour():
    """Interroge FusionSolar pour récupérer la puissance heure par heure de la journée en cours."""
    if not fs_user or not fs_pass:
        return pd.DataFrame()
        
    client = FusionSolarClient(fs_url, fs_user, fs_pass)
    if not client.login():
        return pd.DataFrame()
        
    # Identification des centrales sur le compte
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
        
    # Interrogation de l'API pour aujourd'hui
    aujourdhui = date.today()
    data_brute = client.get_hourly_kpi(list(mapping.keys()), aujourdhui)
    
    # Formatage des données pour le graphique
    lignes = []
    for item in data_brute:
        code = item.get("stationCode") or item.get("plantCode")
        if code not in mapping:
            continue
            
        temps = pd.to_datetime(item.get("collectTime"), unit="ms", utc=True).tz_convert("Europe/Paris")
        map_data = item.get("dataItemMap", {})
        
        # Puissance instantanée renvoyée par Huawei (inverter_power ou active_power)
        puissance = map_data.get("inverter_power") or map_data.get("active_power") or 0.0
        
        lignes.append({
            "Site": mapping[code],
            "Heure": temps,
            "Puissance (kW)": float(puissance)
        })
        
    return pd.DataFrame(lignes)


# =====================================================================
# 2. AFFICHAGE DANS L'INTERFACE STREAMLIT (À mettre là où tu veux le graphique)
# =====================================================================

st.markdown("---")
st.subheader("⚡ Puissance en direct (Aujourd'hui)")

with st.spinner("Récupération des données en temps réel depuis FusionSolar..."):
    df_direct = get_courbes_du_jour()

if not df_direct.empty:
    # Création du graphique avec Streamlit
    st.line_chart(
        data=df_direct,
        x="Heure",
        y="Puissance (kW)",
        color="Site",
        use_container_width=True
    )
    st.caption("Données actualisées toutes les 10 minutes (Appuyez sur 'R' ou actualisez la page pour forcer la mise à jour).")
else:
    st.info("Les données horaires en direct sont indisponibles pour le moment. Vérifiez vos identifiants dans les secrets Streamlit.")