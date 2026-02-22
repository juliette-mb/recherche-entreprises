#!/usr/bin/env python3
"""
recherche_entreprises.py

Recherche des entreprises françaises à racheter via l'API Pappers,
avec enrichissement des dirigeants (email + mobile) via Fullenrich.

Usage :
    python recherche_entreprises.py --secteur "plomberie" --region "Île-de-France" \
        --ca-min 2000000 --ca-max 6000000 --age-min-dirigeant 55
"""

import argparse
import csv
import os
import re
import sys
import time
from datetime import datetime

import unicodedata

import requests

# ---------------------------------------------------------------------------
# Correspondance noms de régions → codes INSEE (paramètre `region` de l'API Pappers)
# ---------------------------------------------------------------------------
REGIONS_INSEE: dict[str, str] = {
    "ile-de-france": "11",
    "centre-val de loire": "24",
    "centre-val-de-loire": "24",
    "bourgogne-franche-comte": "27",
    "bourgogne-franche-comté": "27",
    "normandie": "28",
    "hauts-de-france": "32",
    "grand est": "44",
    "grand-est": "44",
    "pays de la loire": "52",
    "pays-de-la-loire": "52",
    "bretagne": "53",
    "nouvelle-aquitaine": "75",
    "occitanie": "76",
    "auvergne-rhone-alpes": "84",
    "auvergne-rhône-alpes": "84",
    "provence-alpes-cote d'azur": "93",
    "provence-alpes-côte d'azur": "93",
    "paca": "93",
    "corse": "94",
}


def _normalize(s: str) -> str:
    """Supprime les accents et met en minuscules pour la correspondance."""
    return unicodedata.normalize("NFD", s).encode("ascii", "ignore").decode().lower().strip()


def _slugify(s: str) -> str:
    """Génère un slug URL à partir d'un nom d'entreprise (style Pappers)."""
    s = unicodedata.normalize("NFD", s).encode("ascii", "ignore").decode()
    s = s.lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")


def region_to_code(region: str) -> str:
    """
    Convertit un nom de région en code INSEE attendu par l'API Pappers.
    Si la valeur est déjà un code numérique, elle est renvoyée telle quelle.
    """
    if region.strip().isdigit():
        return region.strip()
    normalized = _normalize(region)
    # Cherche d'abord avec accents, puis sans
    for key, code in REGIONS_INSEE.items():
        if _normalize(key) == normalized:
            return code
    # Retourne la valeur originale si non trouvée (laisse l'API gérer)
    return region


# ---------------------------------------------------------------------------
# Clés API — définies via variables d'environnement (voir .env.example)
# ---------------------------------------------------------------------------
PAPPERS_API_KEY = os.environ.get("PAPPERS_API_KEY", "")
FULLENRICH_API_KEY = os.environ.get("FULLENRICH_API_KEY", "")

# ---------------------------------------------------------------------------
# URLs de base
# ---------------------------------------------------------------------------
PAPPERS_BASE_URL = "https://api.pappers.fr/v2"
FULLENRICH_BASE_URL = "https://app.fullenrich.com/api/v2"

# ---------------------------------------------------------------------------
# Paramètres par défaut
# ---------------------------------------------------------------------------
DEFAULT_MAX_RESULTATS = 100
DEFAULT_MAX_ENRICHISSEMENTS = 10
OUTPUT_DIR = "resultats"

# Délai entre les appels Pappers (secondes) pour respecter le rate limit
PAPPERS_DELAY = 0.4
FULLENRICH_POLL_INTERVAL = 4   # secondes entre chaque polling
FULLENRICH_POLL_MAX = 40        # nombre max de tentatives de polling


# ---------------------------------------------------------------------------
# Parsing des arguments
# ---------------------------------------------------------------------------
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Recherche d'entreprises françaises à racheter",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Exemples :
  python recherche_entreprises.py --secteur "plomberie" --region "Île-de-France" \\
      --ca-min 2000000 --ca-max 6000000 --age-min-dirigeant 55

  python recherche_entreprises.py --secteur "4322A" --region "Bretagne" --ca-min 500000

  python recherche_entreprises.py --secteur "boulangerie" --max-resultats 50 \\
      --max-enrichissements 20 --output boulangeries.csv
        """,
    )
    parser.add_argument(
        "--secteur",
        required=True,
        help="Secteur d'activité : mot-clé (ex: plomberie) ou code NAF (ex: 4322A)",
    )
    parser.add_argument(
        "--region",
        help="Région française (ex: Île-de-France, Bretagne, Nouvelle-Aquitaine)",
    )
    parser.add_argument(
        "--ca-min",
        type=int,
        metavar="EUROS",
        help="Chiffre d'affaires minimum en € (ex: 2000000)",
    )
    parser.add_argument(
        "--ca-max",
        type=int,
        metavar="EUROS",
        help="Chiffre d'affaires maximum en € (ex: 6000000)",
    )
    parser.add_argument(
        "--age-min-dirigeant",
        type=int,
        metavar="ANS",
        help="Âge minimum du dirigeant en années (ex: 55)",
    )
    parser.add_argument(
        "--max-resultats",
        type=int,
        default=DEFAULT_MAX_RESULTATS,
        metavar="N",
        help=f"Nombre maximum d'entreprises à récupérer (défaut: {DEFAULT_MAX_RESULTATS})",
    )
    parser.add_argument(
        "--max-enrichissements",
        type=int,
        default=DEFAULT_MAX_ENRICHISSEMENTS,
        metavar="N",
        help=(
            f"Nombre maximum de dirigeants à enrichir via Fullenrich "
            f"(défaut: {DEFAULT_MAX_ENRICHISSEMENTS}). "
            "Mettre 0 pour désactiver l'enrichissement."
        ),
    )
    parser.add_argument(
        "--output",
        metavar="FICHIER.csv",
        help="Chemin du fichier CSV de sortie (défaut: resultats/resultats_YYYYMMDD_HHMMSS.csv)",
    )
    return parser.parse_args()


# ---------------------------------------------------------------------------
# Pappers — Recherche
# ---------------------------------------------------------------------------
def search_pappers(args: argparse.Namespace) -> list[dict]:
    """
    Interroge l'endpoint /recherche de Pappers avec les critères fournis.
    Retourne la liste brute des entreprises (format Pappers).
    """
    print("\nRecherche Pappers en cours...")

    companies: list[dict] = []
    page = 1
    par_page = min(100, args.max_resultats)

    # Déterminer si --secteur est un code NAF (format DDDDL, ex: 4322A)
    secteur = getattr(args, 'secteur', '') or ''
    is_naf = bool(secteur) and bool(re.match(r"^\d{4}[A-Za-z]$", secteur.strip()))

    while len(companies) < args.max_resultats:
        params: dict = {
            "api_token": PAPPERS_API_KEY,
            "par_page": par_page,
            "page": page,
        }

        entreprise_cessee = getattr(args, 'entreprise_cessee', False)
        params["entreprise_cessee"] = "true" if entreprise_cessee else "false"

        if secteur:
            if is_naf:
                params["code_naf"] = secteur.strip().upper()
            else:
                q_parts = [secteur.strip()]
                ville = getattr(args, 'ville', None)
                if ville:
                    q_parts.append(ville.strip())
                params["q"] = " ".join(q_parts)
        elif getattr(args, 'ville', None):
            params["q"] = args.ville.strip()

        if args.region:
            params["region"] = region_to_code(args.region)
        if args.ca_min:
            params["chiffre_affaires_min"] = args.ca_min
        if args.ca_max:
            params["chiffre_affaires_max"] = args.ca_max
        if args.age_min_dirigeant:
            params["age_dirigeant_min"] = args.age_min_dirigeant

        departement = getattr(args, 'departement', None)
        if departement:
            params["departement"] = departement.strip()

        categorie_juridique = getattr(args, 'categorie_juridique', None)
        if categorie_juridique:
            params["categorie_juridique"] = categorie_juridique

        date_creation_min = getattr(args, 'date_creation_min', None)
        if date_creation_min:
            params["date_creation_min"] = f"{date_creation_min}-01-01"

        statut_rcs = getattr(args, 'statut_rcs', None)
        if statut_rcs:
            params["statut_rcs"] = statut_rcs

        try:
            resp = requests.get(
                f"{PAPPERS_BASE_URL}/recherche",
                params=params,
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
        except requests.exceptions.HTTPError as e:
            print(f"  Erreur HTTP Pappers ({e.response.status_code}): {e.response.text[:200]}")
            break
        except requests.exceptions.RequestException as e:
            print(f"  Erreur réseau Pappers : {e}")
            break

        results: list[dict] = data.get("resultats", [])
        total: int = data.get("total", 0)

        if page == 1:
            print(f"  {total} entreprise(s) trouvée(s) au total (on récupère max {args.max_resultats})")

        if not results:
            break

        companies.extend(results)
        remaining = min(total, args.max_resultats) - len(companies)
        print(f"  Page {page} : {len(results)} entreprises récupérées ({len(companies)}/{min(total, args.max_resultats)})")

        if remaining <= 0 or len(results) < par_page:
            break

        page += 1
        time.sleep(PAPPERS_DELAY)

    return companies[: args.max_resultats]


# ---------------------------------------------------------------------------
# Pappers — Détail entreprise
# ---------------------------------------------------------------------------
def get_company_details(siren: str) -> dict:
    """Récupère le détail complet d'une entreprise via son SIREN."""
    try:
        resp = requests.get(
            f"{PAPPERS_BASE_URL}/entreprise",
            params={"api_token": PAPPERS_API_KEY, "siren": siren},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()
    except requests.exceptions.RequestException:
        return {}


# ---------------------------------------------------------------------------
# Extraction des informations utiles
# ---------------------------------------------------------------------------
def extract_company_info(company: dict, details: dict) -> dict:
    """
    Construit un dictionnaire normalisé à partir des données brutes Pappers
    (résultat de recherche + détail entreprise).
    """
    siren = company.get("siren", "")
    nom = company.get("nom_entreprise") or company.get("denomination", "")

    # --- Adresse ---
    siege = details.get("siege") or company.get("siege") or {}
    adresse_parts = [
        str(siege.get(f, "")).strip()
        for f in ("numero_voie", "indice_repetition", "type_voie", "libelle_voie")
        if siege.get(f)
    ]
    adresse_ligne = " ".join(adresse_parts)
    complement = siege.get("complement_adresse", "")
    if complement:
        adresse_ligne = f"{complement}, {adresse_ligne}".strip(", ")
    code_postal = siege.get("code_postal", "")
    ville = siege.get("ville", "")
    adresse = ", ".join(filter(None, [adresse_ligne, f"{code_postal} {ville}".strip()]))

    # --- Finances ---
    # La recherche expose directement chiffre_affaires à la racine ; le détail
    # le met dans finances[0]. On prend la valeur la plus riche disponible.
    ca = (
        company.get("chiffre_affaires")
        or (details.get("finances") or [{}])[0].get("chiffre_affaires")
        or ""
    )

    # --- Effectif ---
    # effectifs_finances (nombre entier) > effectif (chaîne) > tranche_effectif
    effectif = (
        company.get("effectifs_finances")
        or details.get("effectif")
        or company.get("effectif")
        or (details.get("finances") or [{}])[0].get("effectif")
        or details.get("tranche_effectif")
        or company.get("tranche_effectif")
        or ""
    )

    # --- Site web ---
    site_web = (
        details.get("domaine_url")
        or company.get("domaine_url")
        or siege.get("domaine_url")
        or ""
    )

    # --- Dirigeant principal ---
    # L'endpoint /entreprise expose les dirigeants dans "representants" ;
    # l'endpoint /recherche les expose dans "dirigeants" (souvent vide).
    dirigeants: list[dict] = (
        details.get("representants")
        or details.get("dirigeants")
        or company.get("dirigeants")
        or []
    )
    dirigeant: dict = {}
    for d in dirigeants:
        # Ignorer les personnes morales (filiales, holdings)
        if not d.get("personne_morale", False):
            dirigeant = d
            break
    if not dirigeant and dirigeants:
        dirigeant = dirigeants[0]

    nom_dirigeant = ""
    age_dirigeant = ""
    prenom_dir = ""
    nom_dir = ""

    if dirigeant:
        prenom_dir = dirigeant.get("prenom", "").strip()
        nom_dir = dirigeant.get("nom", "").strip()
        # Préférer nom_complet (fourni par /entreprise) pour éviter les virgules dans les prénoms multiples
        nom_dirigeant = dirigeant.get("nom_complet", "").strip() or " ".join(filter(None, [prenom_dir, nom_dir]))

        age_dirigeant = dirigeant.get("age", "")
        if not age_dirigeant:
            # Tenter de calculer depuis date_de_naissance (YYYY-MM-DD) ou
            # date_de_naissance_formate (DD/MM/YYYY)
            dob = dirigeant.get("date_de_naissance", "") or dirigeant.get("date_de_naissance_formate", "")
            if dob:
                try:
                    # Format YYYY-MM-DD
                    if "-" in str(dob):
                        birth_year = int(str(dob).split("-")[0])
                    else:
                        # Format DD/MM/YYYY
                        birth_year = int(str(dob).split("/")[-1])
                    age_dirigeant = datetime.now().year - birth_year
                except (ValueError, IndexError):
                    pass

    slug = _slugify(nom) if nom else ""
    pappers_url = (
        f"https://www.pappers.fr/entreprise/{slug}-{siren}"
        if siren and slug else ""
    )

    return {
        "nom_entreprise": nom,
        "siren": siren,
        "chiffre_affaires": ca,
        "effectif": effectif,
        "adresse": adresse,
        "site_web": site_web,
        "nom_dirigeant": nom_dirigeant,
        "age_dirigeant": age_dirigeant,
        "email_dirigeant": "",
        "mobile_dirigeant": "",
        "pappers_url": pappers_url,
        # Champs internes pour Fullenrich (non exportés dans le CSV)
        "_prenom": prenom_dir,
        "_nom": nom_dir,
        "_domaine": site_web,
        "_nom_entreprise_raw": nom,
        "_effectifs_finances": (
            company.get("effectifs_finances")
            or (details.get("finances") or [{}])[0].get("effectif")
        ),
    }


# ---------------------------------------------------------------------------
# Fullenrich — Crédits disponibles
# ---------------------------------------------------------------------------
def get_fullenrich_credits() -> int | None:
    """Retourne le solde de crédits Fullenrich, ou None en cas d'erreur."""
    try:
        resp = requests.get(
            f"{FULLENRICH_BASE_URL}/account/credits",
            headers={"Authorization": f"Bearer {FULLENRICH_API_KEY}"},
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json().get("balance")
    except requests.exceptions.RequestException:
        return None


# ---------------------------------------------------------------------------
# Fullenrich — Enrichissement en masse
# ---------------------------------------------------------------------------
def enrich_with_fullenrich(companies_info: list[dict]) -> list[dict]:
    """
    Enrichit les dirigeants via l'API Fullenrich (bulk v2).
    Affiche les crédits disponibles et demande une confirmation avant d'envoyer.
    Modifie en place les champs email_dirigeant et mobile_dirigeant.
    """
    if not companies_info:
        return companies_info

    # Ne garder que ceux qui ont un dirigeant identifié
    to_enrich: list[tuple[int, dict]] = [
        (i, c) for i, c in enumerate(companies_info) if c.get("_nom") or c.get("_prenom")
    ]

    if not to_enrich:
        print("  Aucun dirigeant identifié — enrichissement ignoré.")
        return companies_info

    n = len(to_enrich)

    # --- Affichage du bilan avant enrichissement ---
    print(f"\n{'─' * 60}")
    print(f"  ENRICHISSEMENT FULLENRICH")
    print(f"{'─' * 60}")
    print(f"  Dirigeants à soumettre : {n}")
    print(f"  Credits consommés      : 0 à {n} (uniquement si un résultat est trouvé)")

    credits = get_fullenrich_credits()
    if credits is not None:
        print(f"  Solde actuel           : {credits} crédit(s)")
        if credits < n:
            print(f"  ATTENTION : solde potentiellement insuffisant pour {n} enrichissement(s).")
    else:
        print("  Solde actuel           : impossible à récupérer")

    print()
    try:
        confirm = input("  Lancer l'enrichissement Fullenrich ? [o/N] : ").strip().lower()
    except (KeyboardInterrupt, EOFError):
        print("\n  Enrichissement annulé.")
        return companies_info

    if confirm not in ("o", "oui", "y", "yes"):
        print("  Enrichissement annulé.")
        return companies_info

    # --- Construction du payload ---
    payload_data = []
    for _, c in to_enrich:
        contact: dict = {
            "first_name": c["_prenom"],
            "last_name": c["_nom"],
            "enrich_fields": ["contact.emails", "contact.phones"],
        }
        if c.get("_domaine"):
            contact["domain"] = c["_domaine"]
        elif c.get("_nom_entreprise_raw"):
            contact["company_name"] = c["_nom_entreprise_raw"]
        payload_data.append(contact)

    payload = {
        "name": f"recherche-entreprises-{datetime.now().strftime('%Y%m%d-%H%M%S')}",
        "data": payload_data,
    }

    headers = {
        "Authorization": f"Bearer {FULLENRICH_API_KEY}",
        "Content-Type": "application/json",
    }

    # --- Lancement de l'enrichissement ---
    print(f"\n  Envoi de {n} contact(s) à Fullenrich...")
    try:
        resp = requests.post(
            f"{FULLENRICH_BASE_URL}/contact/enrich/bulk",
            json=payload,
            headers=headers,
            timeout=30,
        )
        resp.raise_for_status()
        bulk_resp = resp.json()
    except requests.exceptions.HTTPError as e:
        print(f"  Erreur HTTP Fullenrich ({e.response.status_code}): {e.response.text[:300]}")
        return companies_info
    except requests.exceptions.RequestException as e:
        print(f"  Erreur réseau Fullenrich : {e}")
        return companies_info

    enrichment_id = bulk_resp.get("enrichment_id") or bulk_resp.get("id", "")
    if not enrichment_id:
        print(f"  Erreur : pas d'identifiant d'enrichissement reçu. Réponse : {bulk_resp}")
        return companies_info

    print(f"  Enrichissement lancé (ID : {enrichment_id})")
    print(f"  Attente des résultats (polling toutes les {FULLENRICH_POLL_INTERVAL}s)...")

    # --- Polling ---
    for attempt in range(1, FULLENRICH_POLL_MAX + 1):
        time.sleep(FULLENRICH_POLL_INTERVAL)
        try:
            resp = requests.get(
                f"{FULLENRICH_BASE_URL}/contact/enrich/bulk/{enrichment_id}",
                headers={"Authorization": f"Bearer {FULLENRICH_API_KEY}"},
                timeout=30,
            )
            if resp.status_code == 402:
                body = resp.json() if resp.content else {}
                print(f"  Crédits insuffisants (402) : {body.get('message', resp.text[:200])}")
                break
            if 400 <= resp.status_code < 500:
                print(f"  Erreur client Fullenrich ({resp.status_code}) : {resp.text[:200]}")
                break
            resp.raise_for_status()
            result = resp.json()
        except requests.exceptions.RequestException as e:
            print(f"  Tentative {attempt}/{FULLENRICH_POLL_MAX} - erreur réseau : {e}")
            continue

        status: str = result.get("status", "UNKNOWN").upper()
        print(f"  Tentative {attempt}/{FULLENRICH_POLL_MAX} — statut : {status}")

        if status == "FINISHED":
            records: list[dict] = result.get("data", [])
            enriched_count = 0

            for j, record in enumerate(records):
                if j >= len(to_enrich):
                    break
                original_idx = to_enrich[j][0]
                contact_info: dict = record.get("contact_info") or {}

                # Email
                email_obj = contact_info.get("most_probable_work_email") or {}
                email = email_obj.get("email", "")
                if not email:
                    email_obj2 = contact_info.get("most_probable_personal_email") or {}
                    email = email_obj2.get("email", "")

                # Mobile
                phone_obj = contact_info.get("most_probable_phone") or {}
                mobile = phone_obj.get("number", "")

                companies_info[original_idx]["email_dirigeant"] = email
                companies_info[original_idx]["mobile_dirigeant"] = mobile

                if email or mobile:
                    enriched_count += 1

            print(f"\n  Enrichissement terminé : {enriched_count}/{n} contact(s) enrichi(s)")
            credits_used = result.get("cost", {}).get("credits", "?")
            print(f"  Crédits consommés : {credits_used}")
            break

        if status in ("CANCELED", "CREDITS_INSUFFICIENT", "RATE_LIMIT"):
            print(f"  Enrichissement interrompu ({status}).")
            break
    else:
        print(f"  Timeout : résultats non reçus après {FULLENRICH_POLL_MAX} tentatives.")

    return companies_info


# ---------------------------------------------------------------------------
# Export CSV
# ---------------------------------------------------------------------------
CSV_FIELDS = [
    "nom_entreprise",
    "siren",
    "chiffre_affaires",
    "effectif",
    "adresse",
    "site_web",
    "nom_dirigeant",
    "age_dirigeant",
    "email_dirigeant",
    "mobile_dirigeant",
]


def export_csv(companies_info: list[dict], output_path: str) -> None:
    """Exporte les résultats dans un fichier CSV (séparateur : point-virgule, encodage UTF-8 BOM)."""
    parent = os.path.dirname(output_path)
    if parent:
        os.makedirs(parent, exist_ok=True)

    rows = [{k: c.get(k, "") for k in CSV_FIELDS} for c in companies_info]

    with open(output_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS, delimiter=";")
        writer.writeheader()
        writer.writerows(rows)

    print(f"\nExport CSV terminé : {output_path}  ({len(rows)} ligne(s))")


# ---------------------------------------------------------------------------
# Point d'entrée
# ---------------------------------------------------------------------------
def main() -> None:
    args = parse_args()

    # Fichier de sortie
    if args.output:
        output_path = args.output
    else:
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = os.path.join(OUTPUT_DIR, f"resultats_{ts}.csv")

    # Résumé des critères
    print(f"\n{'═' * 60}")
    print("  RECHERCHE D'ENTREPRISES FRANÇAISES A RACHETER")
    print(f"{'═' * 60}")
    print(f"  Secteur           : {args.secteur}")
    region_display = args.region or "Toutes"
    if args.region:
        code = region_to_code(args.region)
        if code != args.region:
            region_display = f"{args.region} (code INSEE : {code})"
    print(f"  Région            : {region_display}")
    print(f"  CA                : {f'{args.ca_min:,} €' if args.ca_min else '—'} → {f'{args.ca_max:,} €' if args.ca_max else '—'}")
    print(f"  Âge min dirigeant : {f'{args.age_min_dirigeant} ans' if args.age_min_dirigeant else '—'}")
    print(f"  Max entreprises   : {args.max_resultats}")
    print(f"  Max enrichissements Fullenrich : {args.max_enrichissements}")
    print(f"{'═' * 60}")

    # 1. Recherche Pappers
    companies_raw = search_pappers(args)

    if not companies_raw:
        print("\nAucune entreprise trouvée avec ces critères.")
        sys.exit(0)

    # 2. Récupération des détails
    print(f"\nRécupération du détail pour {len(companies_raw)} entreprise(s)...")
    companies_info: list[dict] = []

    for i, company in enumerate(companies_raw, 1):
        siren = company.get("siren", "")
        label = company.get("nom_entreprise") or company.get("denomination") or siren
        print(f"  [{i:>3}/{len(companies_raw)}] {label}", end="", flush=True)

        details = {}
        if siren:
            details = get_company_details(siren)
            time.sleep(PAPPERS_DELAY)

        info = extract_company_info(company, details)
        companies_info.append(info)
        print(f"  — dirigeant : {info['nom_dirigeant'] or '?'}, âge : {info['age_dirigeant'] or '?'}")

    # 3. Enrichissement Fullenrich (sur les N premiers)
    if args.max_enrichissements > 0:
        to_enrich = companies_info[: args.max_enrichissements]
        rest = companies_info[args.max_enrichissements :]

        to_enrich = enrich_with_fullenrich(to_enrich)
        companies_info = to_enrich + rest
    else:
        print("\nEnrichissement Fullenrich désactivé (--max-enrichissements 0).")

    # 4. Export CSV
    export_csv(companies_info, output_path)


if __name__ == "__main__":
    main()
