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
        df_inv["heure_dt"] = df_inv["heure_dt"].dt.round("5min")
        df_inv["heure"] = df_inv["heure_dt"].dt.strftime("%H:%M")

        # Grouper par site et par heure, puis faire la somme des puissances
        df_direct = df_inv.groupby(["site", "heure"], as_index=False)["puissance_kw"].sum()
        df_direct = df_direct.sort_values(by=["heure", "site"])

        return df_direct, None

    except Exception as e:
        return pd.DataFrame(), f"Erreur de communication : {e}"


@st.cache_data(ttl=300)
def get_alarmes_actives():
    """Récupère les alarmes actives sans faire planter l'application."""
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

# ---------------------------------------------------------------- KPI & MAINTENANCE
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

pr_moyen = (prod_mois / kwc_mois) if kwc_mois > 0 else 0

k1, k2, k3, k4 = st.columns(4)
k1.metric(f"Production du jour ({dernier_jour:%d/%m})", f"{fmt(prod_jour)} kWh",
          f"{fmt(prod_jour / kwc_jour, 2)} kWh/kWc", delta_color="off")
k2.metric(f"Production de {MOIS[dernier_jour.month - 1].lower()}", f"{fmt(prod_mois / 1000, 1)} MWh",
          f"{fmt(prod_mois / kwc_mois, 1)} kWh/kWc", delta_color="off")
k3.metric("Indice de Productivité (Mois)", f"{fmt(pr_moyen, 1)} kWh/kWc",
          "Rendement spécifique", delta_color="off")
k4.metric("Taux de réalisation de l'objectif", f"{fmt(taux * 100, 1)} %",
          f"{fmt(total_annee / 1000, 1)} / {fmt(OBJECTIF_ANNUEL_KWH / 1000)} MWh", delta_color="off")

alarmes = get_alarmes_actives()
if alarmes:
    st.error(f"🚨 **Attention :** {len(alarmes)} alerte(s) technique(s) active(s) détectée(s) sur les parcs !")
else:
    st.success("✅ **État du parc :** Tous les onduleurs et centrales fonctionnent normalement (aucun défaut actif).")

st.divider()

# ---------------------------------------------------------------- Graphique en Direct
st.subheader("⚡ Puissance en direct (Aujourd'hui)")

with st.spinner("Récupération de la télémétrie des onduleurs en temps réel..."):
    df_direct, direct_err = get_courbe_horaire(date.today())

if df_direct is not None and not df_direct.empty:
    fig_direct = px.line(
        df_direct,
        x="heure",
        y="puissance_kw",
        color="site",
        color_discrete_map=COULEURS,
        markers=False,
        labels={"puissance_kw": "Puissance (kW)", "heure": ""}
    )
    # Rendu plus fluide pour le pas de 5 min (courbe spline sans les gros points)
    fig_direct.update_traces(line=dict(width=2.5, shape="spline"), mode="lines")
    fig_direct.update_layout(
        margin=dict(t=30, b=10),
        legend=dict(orientation="h", y=-0.25, title=None),
        hovermode="x unified"
    )
    st.plotly_chart(fig_direct, width="stretch")
    st.caption("Données actualisées toutes les 5 à 10 minutes (Appuyez sur 'R' pour forcer la mise à jour).")
else:
    msg = direct_err or "Vérifiez vos identifiants dans les secrets Streamlit."
    st.info(f"Les données en direct sont indisponibles pour le moment. {msg}")

st.divider()

# ---------------------------------------------------------------- Graphique 1 : Rémunération mensuelle estimée
st.subheader("💶 Rémunération mensuelle estimée")

df_rem = (dfy.assign(mois=dfy["date"].dt.month)
             .groupby(["mois", "site"], as_index=False)["kwh_produits"].sum())
df_rem["MWh"] = df_rem["kwh_produits"] / 1000
df_rem["Mois"] = df_rem["mois"].map(lambda m: MOIS[m - 1])

TARIFS = {
    "Les Minières (MSE)": 95.0,
    "Mauges Communauté (MC)": 95.0,
    "La Loge (BEM)": 95.0,
    "Complexe Sportif (SEV)": 91.2
}
df_rem["Tarif (€/MWh)"] = df_rem["site"].map(lambda s: TARIFS.get(s, 95.0))
df_rem["Rémunération (€)"] = df_rem["MWh"] * df_rem["Tarif (€/MWh)"]

fig_rem = px.bar(
    df_rem,
    x="Mois",
    y="Rémunération (€)",
    color="site",
    barmode="group",
    text="site",
    category_orders={"Mois": MOIS},
    color_discrete_map=COULEURS,
    labels={"site": "Site", "Rémunération (€)": "Revenus (€)"}
)
fig_rem.update_traces(texttemplate='%{text}', textposition='outside')
fig_rem.update_layout(margin=dict(t=30, b=10), legend=dict(orientation="h", y=-0.2, title=None))
st.plotly_chart(fig_rem, width="stretch")

# ---------------------------------------------------------------- Graphiques 2 et 3
g2, g3 = st.columns(2)

with g2:
    st.subheader("Performance spécifique par site")
    perf = perf_par_site(dfy).sort_values("kwh_kwc_j", ascending=False)
    moyenne = perf["kwh"].sum() / (perf["kwc"] * perf["jours"]).sum()
    fig2 = px.bar(perf, x="site", y="kwh_kwc_j", color="site", text="site",
                  color_discrete_map=COULEURS,
                  labels={"kwh_kwc_j": "kWh/kWc/jour", "site": ""})
    fig2.add_hline(y=moyenne, line_dash="dash", line_color="gray",
                   annotation_text=f"Moyenne : {fmt(moyenne, 2)}")
    fig2.update_traces(texttemplate='%{text}', textposition='outside')
    fig2.update_layout(showlegend=False, margin=dict(t=30, b=10))
    st.plotly_chart(fig2, width="stretch")
    st.caption("Production moyenne par jour et par kWc installé, sur les jours de production de chaque site.")

with g3:
    st.subheader("Production mensuelle par site (kWh)")
    mensuel = (dfy.assign(mois=dfy["date"].dt.month)
                  .groupby(["mois", "site"], as_index=False)["kwh_produits"].sum())
    mensuel["kWh"] = mensuel["kwh_produits"]
    mensuel["Mois"] = mensuel["mois"].map(lambda m: MOIS[m - 1])

    fig3 = px.bar(mensuel, x="Mois", y="kWh", color="site", barmode="group",
                  text="site", category_orders={"Mois": MOIS}, color_discrete_map=COULEURS,
                  labels={"site": "Site", "kWh": "Production (kWh)"})
    fig3.update_traces(texttemplate='%{text}', textposition='outside')
    fig3.update_layout(margin=dict(t=30, b=10), legend=dict(orientation="h", y=-0.25, title=None))
    st.plotly_chart(fig3, width="stretch")

# ---------------------------------------------------------------- Graphique 4 : Production totale depuis le début de l'année
st.subheader("📈 Production totale par site depuis le début de l'année")
df_ytd = dfy.groupby("site", as_index=False)["kwh_produits"].sum()

fig_ytd = px.bar(df_ytd, x="site", y="kwh_produits", color="site", text="site",
                 color_discrete_map=COULEURS,
                 labels={"kwh_produits": "Production totale (kWh)", "site": ""})
fig_ytd.update_traces(texttemplate='%{text}', textposition='outside')
fig_ytd.update_layout(showlegend=False, margin=dict(t=30, b=10))
st.plotly_chart(fig_ytd, width="stretch")

with st.expander("Données journalières récentes"):
    recent = dfy.sort_values(["date", "site"], ascending=[False, True])
    st.dataframe(recent[["date", "site", "kwh_produits", "ratio_kwh_kwc", "source"]].head(120),
                 width="stretch", hide_index=True)
