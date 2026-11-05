"""Configuration commune : référentiel des sites Mauges Énergies et objectifs."""
import os
import re
import unicodedata
from pathlib import Path

DB_PATH = Path(os.getenv("DB_PATH") or Path(__file__).parent / "production_ombrieres.db")

OBJECTIF_ANNUEL_KWH = 1_054_000   # 1 054 MWh/an
PUISSANCE_ANNONCEE_KWC = 987      # affichage ; les ratios utilisent la somme des sites

# aliases : mots (sans accents, minuscules) permettant de reconnaître la centrale
# dans la liste renvoyée par FusionSolar.
SITES_REF = {
    "JUMELLIERE":       {"nom": "La Jumellière (CEA)",      "kwc": 270, "aliases": ["jumelliere", "cea"]},
    "MINIERES":         {"nom": "Les Minières (MSE)",       "kwc": 190, "aliases": ["minieres", "mse"]},
    "POLE_SANTE":       {"nom": "Pôle Santé (ODA)",         "kwc": 175, "aliases": ["pole sante", "oda"]},
    "MAUGES_COM":       {"nom": "Mauges Communauté (MC)",   "kwc": 127, "aliases": ["mauges communaute", "mc"]},
    "LA_LOGE":          {"nom": "La Loge (BEM)",            "kwc": 127, "aliases": ["la loge", "bem"]},
    "COMPLEXE_SPORTIF": {"nom": "Complexe Sportif (SEV)",   "kwc": 97,  "aliases": ["complexe sportif", "sev"]},
    "Déchèterie St Germain": {"nom": "Déchèterie St Germain", "kwc": 22},
}
TOTAL_KWC = sum(s["kwc"] for s in SITES_REF.values())


def _norm(texte: str) -> str:
    texte = unicodedata.normalize("NFKD", texte).encode("ascii", "ignore").decode()
    return texte.lower()


def match_site(station_name: str):
    """Retourne la clé du site correspondant à un nom de centrale FusionSolar, ou None."""
    n = _norm(station_name)
    for key, site in SITES_REF.items():
        for alias in site["aliases"]:
            if re.search(rf"\b{re.escape(alias)}\b", n):
                return key
    return None
