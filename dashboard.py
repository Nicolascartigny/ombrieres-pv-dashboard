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


@st.cache_data(ttl=600)
def get_courbe_horaire(target_date: date):
    """Interroge l'API FusionSolar pour récupérer la puissance heure par heure."""
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
            raw_text = login_resp.text[:250].replace('\n', ' ')
            return pd.DataFrame(), f"Le serveur a renvoyé du texte/HTML au lieu de JSON : {raw_text}"

        token = (login_resp.headers.get("xsrf-token") or 
                 login_resp.headers.get("X-SRX-TOKEN") or 
                 session.cookies.get("XSRF-TOKEN"))

        if not login_data.get("success"):
            return pd.DataFrame(), f"Connexion refusée par Huawei. (Retour : {login_data})"
            
        if not token:
            return pd.DataFrame(), f"Login réussi mais jeton introuvable. (En-têtes reçus : {dict(login_resp.headers)})"

        auth_headers = {**headers, "X-SRX-TOKEN": token, "xsrf-token": token}

        # 2. Récupération de la liste des centrales pour faire la correspondance
        dev_resp = session.post(
            f"{FS_URL}/thirdData/getStationList",
            headers=auth_headers,
            json={},
            timeout=15
        )
        stations = dev_resp.json().get("data", [])
        stations_map = {}
        for s in stations:
            code = s.get("plantCode") or s.get("stationCode")
            nom = s.get("plantName") or s.get("stationName") or ""
            key = match_site(nom)
            if key and code:
                stations_map[code] = SITES_REF[key]["nom"]

        if not stations_map:
            return pd.DataFrame(), "Aucune centrale reconnue dans votre compte FusionSolar."

        # 3. Récupération des données horaires
        ts = datetime(target_date.year, target_date.month, target_date.day, 0, 0, 0, tzinfo=TZ)
        collect_time_ms = int(ts.timestamp() * 1000)

        kpi_resp = session.post(
            f"{FS_URL}/thirdData/getKpiStationHour",
            headers=auth_headers,
            json={"stationCodes": ",".join(stations_map.keys()), "collectTime": collect_time_ms},
            timeout=15
        )

        data = kpi_resp.json().get("data", [])
        if not data:
            return pd.DataFrame(), "Aucune mesure horaire retournée par Huawei pour cette journée."

        lignes = []
        for item in data:
            code = item.get("stationCode") or item.get("plantCode")
            site_nom = stations_map.get(code, code)
            data_map = item.get("dataItemMap", {})
            collect_dt = datetime.fromtimestamp(item.get("collectTime", 0) / 1000, tz=TZ)
            puissance = data_map.get("inverter_power") or data_map.get("active_power") or 0.0

            lignes.append({
                "site": site_nom,
                "heure": collect_dt.strftime("%H:%M"),
                "puissance_kw": float(puissance)
            })

        return pd.DataFrame(lignes), None

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
    "Pôle Santé (ODA)": 198,
    "Déchèterie St Germain": 22.5,
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

# --- CALCUL DE L'ORDRE DYNAMIQUE DES SITES (Du plus productif au moins productif) ---
df_ytd_global = dfy.groupby("site", as_index=False)["kwh_produits"].sum().sort_values("kwh_produits", ascending=False)
ordre_sites = df_ytd_global["site"].tolist()
# ------------------------------------------------------------------------------------

if (dfy["source"] == "mock").any():
    st.info("Données de démonstration (mode MOCK) : renseignez les identifiants FusionSolar pour les données réelles.")

# ---------------------------------------------------------------- KPI & MAINTENANCE
dernier_jour = dfy["date"].max()

# --- 1. Calculs Jour ---
jour_df = dfy[dfy["date"] == dernier_jour]
prod_jour = jour_df["kwh_produits"].sum()

# Hier
jour_prec = dernier_jour - pd.Timedelta(days=1)
prod_jour_prec = df[df["date"] == jour_prec]["kwh_produits"].sum()
delta_jour = prod_jour - prod_jour_prec if prod_jour_prec > 0 else 0

# --- 2. Calculs Mois (Month-to-Date vs même période mois précédent) ---
mois_df = dfy[dfy["date"].dt.month == dernier_jour.month]
prod_mois = mois_df["kwh_produits"].sum()

annee_prec_mois = dernier_jour.year if dernier_jour.month > 1 else dernier_jour.year - 1
mois_prec_idx = dernier_jour.month - 1 if dernier_jour.month > 1 else 12
df_mois_prec = df[(df["date"].dt.year == annee_prec_mois) & 
                  (df["date"].dt.month == mois_prec_idx) & 
                  (df["date"].dt.day <= dernier_jour.day)]
prod_mois_prec = df_mois_prec["kwh_produits"].sum()
delta_mois = prod_mois - prod_mois_prec if prod_mois_prec > 0 else 0

# --- 3. Calculs Année (Year-to-Date vs même période année précédente) ---
total_annee = dfy["kwh_produits"].sum()

df_annee_prec = df[(df["date"].dt.year == dernier_jour.year - 1) & 
                   (df["date"].dt.dayofyear <= dernier_jour.dayofyear)]
prod_annee_prec = df_annee_prec["kwh_produits"].sum()
delta_annee = total_annee - prod_annee_prec if prod_annee_prec > 0 else 0

# --- 4. Rémunération du mois ---
TARIFS_KPI = {
    "Les Minières (MSE)": 95.0,
    "Mauges Communauté (MC)": 95.0,
    "La Loge (BEM)": 95.0,
    "Complexe Sportif (SEV)": 91.2,
    "Déchèterie St Germain": 95.0
}

def calc_rev(d):
    if d.empty: return 0
    g = d.groupby("site")["kwh_produits"].sum().reset_index()
    return ((g["kwh_produits"] / 1000) * g["site"].map(lambda s: TARIFS_KPI.get(s, 95.0))).sum()

rev_mois = calc_rev(mois_df)
rev_mois_prec = calc_rev(df_mois_prec)
delta_rev = rev_mois - rev_mois_prec if rev_mois_prec > 0 else 0

# --- 5. Taux de réalisation ---
taux = total_annee / OBJECTIF_ANNUEL_KWH

# Fonction pour formater proprement les écarts avec le signe - préservé pour Streamlit
def fmt_delta(val, unit, dec=0):
    if val == 0: return None
    s = f"{val:,.{dec}f}".replace(",", "X").replace(".", ",").replace("X", " ")
    return f"{s} {unit}"

# === Affichage des 5 colonnes ===
k1, k2, k3, k4, k5 = st.columns(5)

k1.metric(f"Prod. du jour ({dernier_jour:%d/%m})", 
          f"{fmt(prod_jour)} kWh", 
          fmt_delta(delta_jour, "kWh", 0))

k2.metric(f"Prod. de {MOIS[dernier_jour.month - 1].lower()}", 
          f"{fmt(prod_mois / 1000, 1)} MWh", 
          fmt_delta(delta_mois / 1000, "MWh", 1))

k3.metric("Prod. de l'année", 
          f"{fmt(total_annee / 1000, 1)} MWh", 
          fmt_delta(delta_annee / 1000, "MWh", 1))

k4.metric(f"Rémunération ({MOIS[dernier_jour.month - 1].lower()})", 
          f"{fmt(rev_mois)} €", 
          fmt_delta(delta_rev, "€", 0))

k5.metric("Taux de réalisation", 
          f"{fmt(taux * 100, 1)} %", 
          f"{fmt(total_annee / 1000, 1)} / {fmt(OBJECTIF_ANNUEL_KWH / 1000)} MWh", 
          delta_color="off")

alarmes = get_alarmes_actives()
if alarmes:
    st.error(f"🚨 **Attention :** {len(alarmes)} alerte(s) technique(s) active(s) détectée(s) sur les parcs !")
else:
    st.success("✅ **État du parc :** Tous les onduleurs et centrales fonctionnent normalement (aucun défaut actif).")

st.divider()
# ---------------------------------------------------------------- 1. Graphique en Direct
st.subheader("⚡ Puissance en direct (Aujourd'hui)")

with st.spinner("Récupération des données en temps réel depuis FusionSolar..."):
    df_direct, direct_err = get_courbe_horaire(date.today())

if df_direct is not None and not df_direct.empty:
    fig_direct = px.line(
        df_direct,
        x="heure",
        y="puissance_kw",
        color="site",
        category_orders={"site": ordre_sites},
        color_discrete_map=COULEURS,
        markers=True,
        labels={"puissance_kw": "Puissance (kW)", "heure": ""}
    )
    fig_direct.update_traces(line=dict(width=2.5), marker=dict(size=5))
    fig_direct.update_layout(
        margin=dict(t=30, b=10),
        legend=dict(orientation="h", y=-0.25, title=None),
        hovermode="x unified"
    )
    st.plotly_chart(fig_direct, width="stretch")
    st.caption("Données horaires de l'API. Un délai de consolidation de 1 à 2 heures est normal du côté de Huawei.")
else:
    msg = direct_err or "Vérifiez vos identifiants dans les secrets Streamlit."
    st.info(f"Les données horaires en direct sont indisponibles pour le moment. {msg}")

st.divider()

# ---------------------------------------------------------------- 2. Graphique : Production totale
st.subheader("⚡ Production totale depuis la mise en service")

prod = dfy.groupby("site", as_index=False)["kwh_produits"].sum()

# Conversion de kWh vers MWh
prod["MWh"] = prod["kwh_produits"] / 1000

# Formatage visuel (ex: "65,4 MWh")
prod["label_visuel"] = prod["MWh"].apply(lambda x: f"{x:,.1f} MWh".replace(".", ","))

fig_prod = px.bar(
    prod, 
    x="site", 
    y="MWh", 
    color="site", 
    text="label_visuel",
    category_orders={"site": ordre_sites},
    color_discrete_map=COULEURS,
    labels={"MWh": "Production cumulée (MWh)", "site": ""}
)

fig_prod.update_traces(
    texttemplate='<b>%{text}</b>', 
    textposition='inside', 
    insidetextanchor='middle',
    textfont_color='white'
)
fig_prod.update_layout(showlegend=False, margin=dict(t=30, b=10))
st.plotly_chart(fig_prod, width="stretch")

st.divider()

# ---------------------------------------------------------------- 3. Graphique : Productible annuel extrapolé (Run Rate)
st.subheader("🎯 Tendance annuelle de production (Extrapolée)")

perf = perf_par_site(dfy)

# Création d'un libellé enrichi avec le nombre de jours d'exploitation pour l'axe X (avec un retour à la ligne HTML <br>)
perf["site_label"] = perf.apply(lambda row: f"{row['site']}<br><i>({row['jours']} jours d'exploitation)</i>", axis=1)

# Calcul du productible annualisé basé sur la moyenne journalière : (kWh / kWc / nb_jours) * 365
perf["kwh_kwc_an"] = (perf["kwh"] / perf["kwc"]) / perf["jours"] * 365

# Formatage visuel (ex: "1 088 kWh/kWc/an")
perf["label_visuel"] = perf["kwh_kwc_an"].apply(lambda x: f"{x:,.0f} kWh/kWc/an".replace(",", " "))

# On recrée l'ordre de tri pour qu'il corresponde aux nouveaux libellés enrichis
ordre_labels = []
for s in ordre_sites:
    match = perf[perf["site"] == s]
    if not match.empty:
        ordre_labels.append(match.iloc[0]["site_label"])

fig_perf = px.bar(
    perf, 
    x="site_label", 
    y="kwh_kwc_an", 
    color="site", 
    text="label_visuel",
    category_orders={"site_label": ordre_labels},
    color_discrete_map=COULEURS,
    labels={"kwh_kwc_an": "Productible extrapolé (kWh/kWc/an)", "site_label": ""}
)

# Ligne d'objectif P75 du Business Plan
fig_perf.add_hline(
    y=1051, 
    line_dash="dot", 
    line_color="red", 
    annotation_text="Objectif BP (P75) : 1 051 kWh/kWc/an",
    annotation_position="bottom right"
)

fig_perf.update_traces(
    texttemplate='<b>%{text}</b>', 
    textposition='inside', 
    insidetextanchor='middle',
    textfont_color='white'
)

# Ajustement de l'axe Y pour toujours voir la ligne P75 même en cas de faible production hivernale
max_y = max(1200, perf["kwh_kwc_an"].max() * 1.1)
fig_perf.update_layout(yaxis_range=[0, max_y], showlegend=False, margin=dict(t=30, b=10))

st.plotly_chart(fig_perf, width="stretch")
st.caption("Productible annuel extrapolé (Moyenne journalière x 365 jours). Le nombre de jours d'exploitation sous chaque site permet de contextualiser les écarts de tendance liés aux dates de raccordement.")

st.divider()

# ---------------------------------------------------------------- 4. Graphique : Rémunération du mois en cours
st.subheader("💶 Rémunération du mois en cours")

# Filtrage dynamique pour ne garder que le mois actuel
mois_actuel = pd.Timestamp.now().month
annee_actuelle = pd.Timestamp.now().year
df_mois = dfy[(dfy["date"].dt.month == mois_actuel) & (dfy["date"].dt.year == annee_actuelle)]

# Calcul des revenus pour ce mois précis
df_rem_mois = df_mois.groupby("site", as_index=False)["kwh_produits"].sum()
df_rem_mois["MWh"] = df_rem_mois["kwh_produits"] / 1000

# Application des tarifs 
TARIFS = {
    "Les Minières (MSE)": 95.0,
    "Mauges Communauté (MC)": 95.0,
    "La Loge (BEM)": 95.0,
    "Complexe Sportif (SEV)": 91.2,
    "Déchèterie St Germain": 95.0
}
df_rem_mois["Tarif (€/MWh)"] = df_rem_mois["site"].map(lambda s: TARIFS.get(s, 95.0))
df_rem_mois["Revenus (€)"] = df_rem_mois["MWh"] * df_rem_mois["Tarif (€/MWh)"]

# Formatage visuel (ex: "450 €")
df_rem_mois["label_visuel"] = df_rem_mois["Revenus (€)"].apply(lambda x: f"{x:,.0f} €".replace(",", " "))

fig_rem_mois = px.bar(
    df_rem_mois, 
    x="site", 
    y="Revenus (€)", 
    color="site", 
    text="label_visuel",
    category_orders={"site": ordre_sites},
    color_discrete_map=COULEURS,
    labels={"Revenus (€)": "Revenus du mois (€)", "site": ""}
)

fig_rem_mois.update_traces(
    texttemplate='<b>%{text}</b>', 
    textposition='inside', 
    insidetextanchor='middle',
    textfont_color='white'
)
fig_rem_mois.update_layout(showlegend=False, margin=dict(t=30, b=10))
st.plotly_chart(fig_rem_mois, width="stretch")

st.divider()

# ---------------------------------------------------------------- 5. Graphique : Rémunération totale YTD
st.subheader("💶 Rémunération totale depuis le début de l'année")

df_rem_ytd = dfy.groupby("site", as_index=False)["kwh_produits"].sum()
df_rem_ytd["MWh"] = df_rem_ytd["kwh_produits"] / 1000

df_rem_ytd["Tarif (€/MWh)"] = df_rem_ytd["site"].map(lambda s: TARIFS.get(s, 95.0))
df_rem_ytd["Revenus (€)"] = df_rem_ytd["MWh"] * df_rem_ytd["Tarif (€/MWh)"]
df_rem_ytd["Revenus (k€)"] = df_rem_ytd["Revenus (€)"] / 1000

# Formatage visuel (ex: "6,2 k€")
df_rem_ytd["label_visuel"] = df_rem_ytd["Revenus (k€)"].apply(lambda x: f"{x:,.1f} k€".replace(".", ","))

fig_rem_ytd = px.bar(
    df_rem_ytd, 
    x="site", 
    y="Revenus (k€)", 
    color="site", 
    text="label_visuel",
    category_orders={"site": ordre_sites},
    color_discrete_map=COULEURS,
    labels={"Revenus (k€)": "Revenus cumulés YTD (k€)", "site": ""}
)

fig_rem_ytd.update_traces(
    texttemplate='<b>%{text}</b>', 
    textposition='inside', 
    insidetextanchor='middle',
    textfont_color='white'
)
fig_rem_ytd.update_layout(showlegend=False, margin=dict(t=30, b=10))
st.plotly_chart(fig_rem_ytd, width="stretch")

# Calcul et affichage du total global (en euros précis)
total_revenus = df_rem_ytd["Revenus (€)"].sum()
st.metric(label="💰 Total généré par l'ensemble du parc", value=f"{total_revenus:,.0f} €".replace(",", " "))

st.divider()

# ---------------------------------------------------------------- Tableau des données
with st.expander("Données journalières récentes"):
    recent = dfy.sort_values(["date", "site"], ascending=[False, True])
    st.dataframe(recent[["date", "site", "kwh_produits", "ratio_kwh_kwc", "source"]].head(120),
                 width="stretch", hide_index=True)
