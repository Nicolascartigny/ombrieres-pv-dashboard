#!/usr/bin/env python3
"""Collecte la production quotidienne des 6 sites via l'API Northbound Huawei FusionSolar
et l'enregistre dans SQLite. Sans identifiants (ou avec --mock) : données de démonstration.

Usage :
    python collect_data.py                    # derniers jours (par défaut hier + aujourd'hui)
    python collect_data.py --since 2026-01-01 # rattrapage historique
    python collect_data.py --mock             # force le mode démonstration
"""
import argparse
import math
import os
import random
import sqlite3
import sys
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import requests

from config import DB_PATH, SITES_REF, match_site

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

TZ = ZoneInfo("Europe/Paris")
DEFAULT_URL = "https://uni005eu5.fusionsolar.huawei.com"


# --------------------------------------------------------------------------- base
def get_conn():
    return sqlite3.connect(DB_PATH)


def init_db():
    with get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS production_journaliere (
                date          TEXT NOT NULL,
                site_key      TEXT NOT NULL,
                station_code  TEXT,
                station_name  TEXT,
                kwh_produits  REAL,
                ratio_kwh_kwc REAL,
                source        TEXT DEFAULT 'api',
                PRIMARY KEY (date, site_key)
            )
        """)


def save_records(records):
    with get_conn() as conn:
        conn.executemany("""
            INSERT OR REPLACE INTO production_journaliere
            (date, site_key, station_code, station_name, kwh_produits, ratio_kwh_kwc, source)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, records)
    print(f"{len(records)} enregistrements sauvegardés dans {DB_PATH.name}.")


def purge_mock():
    with get_conn() as conn:
        n = conn.execute("DELETE FROM production_journaliere WHERE source = 'mock'").rowcount
    if n:
        print(f"{n} lignes de démonstration supprimées (remplacées par les données réelles).")


# --------------------------------------------------------------------------- API
class FusionSolarClient:
    def __init__(self, base_url, username, password):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.session = requests.Session()
        self.session.headers.update({"Content-Type": "application/json"})

    def login(self) -> bool:
        resp = self.session.post(
            f"{self.base_url}/thirdData/login",
            json={"userName": self.username, "systemCode": self.password},
            timeout=30,
        )
        try:
            data = resp.json()
        except ValueError:
            print(f"Réponse de login illisible (HTTP {resp.status_code}). Vérifiez FUSIONSOLAR_URL.")
            return False
        if not (resp.ok and data.get("success") and data.get("failCode", 0) == 0):
            print(f"Échec de connexion API : {data}")
            return False
        token = (resp.headers.get("xsrf-token") or resp.headers.get("xsfr-token")
                 or resp.headers.get("X-SRX-TOKEN")
                 or self.session.cookies.get("XSRF-TOKEN"))
        if not token:
            print("Connexion acceptée mais aucun jeton XSRF reçu.")
            return False
        self.session.headers["XSRF-TOKEN"] = token
        self.session.headers["X-SRX-TOKEN"] = token
        print("Connexion à l'API FusionSolar réussie.")
        return True

    def _post(self, endpoint, payload, retries=3):
        url = f"{self.base_url}/thirdData/{endpoint}"
        for attempt in range(retries):
            resp = self.session.post(url, json=payload, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            code = data.get("failCode", 0)
            if data.get("success") or code == 0:
                return data
            if code == 305 and self.login():        # session expirée
                continue
            if code == 407:                          # quota de fréquence dépassé
                wait = 30 * (attempt + 1)
                print(f"Limite de fréquence atteinte, nouvelle tentative dans {wait}s...")
                time.sleep(wait)
                continue
            raise RuntimeError(f"Erreur API {endpoint} : {data}")
        raise RuntimeError(f"Échec de l'appel {endpoint} après {retries} tentatives.")

    def get_stations(self):
        """Liste des centrales : endpoint récent /stations (paginé), repli sur getStationList."""
        erreurs = []
        for endpoint in ("stations", "getStationList"):
            try:
                stations, page = [], 1
                while True:
                    data = self._post(endpoint, {"pageNo": page, "pageSize": 100}).get("data") or []
                    if isinstance(data, list):
                        stations += data
                        break
                    stations += data.get("list", [])
                    if page >= data.get("pageCount", 1):
                        break
                    page += 1
                return stations
            except RuntimeError as e:
                erreurs.append(str(e))
        raise RuntimeError(" | ".join(erreurs))

    def get_daily_kpi(self, station_codes, any_day: date):
        ts = datetime(any_day.year, any_day.month, any_day.day, 12, tzinfo=TZ)
        payload = {"stationCodes": ",".join(station_codes), "collectTime": int(ts.timestamp() * 1000)}
        return self._post("getKpiStationDay", payload).get("data") or []

    def get_hourly_kpi(self, station_codes, any_day: date):
        """Récupère la courbe de puissance horaire/infra-journalière du jour."""
        ts = datetime(any_day.year, any_day.month, any_day.day, 0, 0, 0, tzinfo=TZ)
        payload = {
            "stationCodes": ",".join(station_codes),
            "collectTime": int(ts.timestamp() * 1000)
        }
        return self._post("getKpiStationHour", payload).get("data") or []

    def get_active_alarms(self, station_codes=None):
        """Récupère la liste des alarmes actives."""
        payload = {"status": 1}
        if station_codes:
            payload["stationCodes"] = ",".join(station_codes)
        return self._post("getAlarmList", payload).get("data") or []


def _as_float(value):
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def months_between(start: date, end: date):
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        yield date(y, m, 1)
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)


def collect_real(client, since: date, until: date) -> bool:
    stations = client.get_stations()
    mapping = {}
    for s in stations:
        nom = s.get("plantName") or s.get("stationName") or ""
        code = s.get("plantCode") or s.get("stationCode")
        key = match_site(nom)
        if key and code:
            mapping[code] = (key, nom)
            print(f"Centrale reconnue : {nom} -> {SITES_REF[key]['nom']}")
        else:
            print(f"Centrale ignorée (hors périmètre) : {nom or s}")
    manquants = set(SITES_REF) - {k for k, _ in mapping.values()}
    if manquants:
        print(f"Attention, sites non trouvés dans FusionSolar : {', '.join(sorted(manquants))}")
    if not mapping:
        print("Aucune centrale reconnue. Ajustez les 'aliases' dans config.py.")
        return False

    records = []
    for first_of_month in months_between(since, until):
        for item in client.get_daily_kpi(list(mapping), first_of_month):
            code = item.get("stationCode") or item.get("plantCode")
            if code not in mapping:
                continue
            jour = datetime.fromtimestamp(item["collectTime"] / 1000, TZ).date()
            if not (since <= jour <= until):
                continue
            kpi = item.get("dataItemMap") or {}
            kwh = _as_float(kpi.get("inverter_power"))
            if kwh is None:
                kwh = _as_float(kpi.get("product_power"))
            if kwh is None:
                continue
            key, name = mapping[code]
            kwc = SITES_REF[key]["kwc"]
            records.append((jour.isoformat(), key, code, name, kwh, round(kwh / kwc, 3), "api"))
        time.sleep(2)

    if not records:
        print("Aucune donnée journalière renvoyée par l'API.")
        return False
    purge_mock()
    save_records(records)
    return True


# --------------------------------------------------------------------------- mode MOCK
def mock_ratio(jour: date, site_key: str) -> float:
    """kWh/kWc réaliste : saison (Anjou ~1 070 kWh/kWc/an), météo commune, facteur site."""
    doy = jour.timetuple().tm_yday
    saison = 2.95 + 2.15 * math.sin(2 * math.pi * (doy - 80) / 365)
    meteo = 0.3 + 1.4 * random.Random(jour.isoformat()).betavariate(2, 2)
    facteur_site = random.Random(site_key).uniform(0.95, 1.05)
    bruit = random.Random(f"{jour.isoformat()}-{site_key}").uniform(0.97, 1.03)
    return round(max(0.1, min(6.5, saison * meteo * facteur_site * bruit)), 3)


def collect_mock(since: date, until: date):
    records = []
    jour = since
    while jour <= until:
        for key, site in SITES_REF.items():
            ratio = mock_ratio(jour, key)
            records.append((jour.isoformat(), key, key, site["nom"],
                            round(ratio * site["kwc"], 1), ratio, "mock"))
        jour += timedelta(days=1)
    save_records(records)


# --------------------------------------------------------------------------- main
def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mock", action="store_true", help="forcer le mode démonstration")
    parser.add_argument("--since", type=date.fromisoformat, help="date de début (AAAA-MM-JJ)")
    parser.add_argument("--days", type=int, default=2, help="nombre de jours récents à collecter (défaut 2)")
    args = parser.parse_args()

    init_db()
    today = datetime.now(TZ).date()

    base_url = os.getenv("FUSIONSOLAR_URL") or DEFAULT_URL
    user = os.getenv("FUSIONSOLAR_USER") or ""
    password = os.getenv("FUSIONSOLAR_PASSWORD") or os.getenv("FUSIONSOLAR_PASS") or ""
    creds_ok = bool(user and password) and not user.startswith("votre_") and not password.startswith("votre_")

    if args.mock or os.getenv("MOCK") == "1" or not creds_ok:
        print("Mode DÉMONSTRATION : données simulées (identifiants API non renseignés).")
        collect_mock(args.since or date(today.year, 1, 1), today)
        return 0

    since = args.since or (today - timedelta(days=max(args.days, 1) - 1))
    client = FusionSolarClient(base_url, user, password)
    if not client.login():
        return 1
    return 0 if collect_real(client, since, today) else 1


if __name__ == "__main__":
    sys.exit(main())
