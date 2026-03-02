#!/usr/bin/env python3
"""
app.py — Interface web Flask pour la recherche d'entreprises françaises à racheter.

Routes :
  GET  /login        Page de connexion
  POST /login        Authentification
  GET  /logout       Déconnexion
  GET  /             Page principale (recherche)
  POST /api/search   Formulaire structuré → recherche Pappers → JSON
  POST /api/export   Génère et retourne le CSV
"""

import csv
import io
import os
import time
from functools import wraps
from types import SimpleNamespace

import re

import requests
from dotenv import load_dotenv
from flask import (
    Flask,
    jsonify,
    make_response,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from supabase import create_client as _mk_supabase

from recherche_entreprises import (
    FULLENRICH_BASE_URL,
    FULLENRICH_POLL_INTERVAL,
    FULLENRICH_POLL_MAX,
    _fullenrich_key,
    extract_company_info,
    get_company_details,
    search_datagouv,
    search_pappers,
)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", os.urandom(32).hex())

APP_PASSWORD = os.environ.get("APP_PASSWORD", "admin123")

CSV_FIELDS = [
    "nom_entreprise",
    "siren",
    "chiffre_affaires",
    "resultat_net",
    "effectif",
    "adresse",
    "site_web",
    "nom_dirigeant",
    "age_dirigeant",
    "email_dirigeant",
    "mobile_dirigeant",
    "pappers_url",
]

# ---------------------------------------------------------------------------
# Authentification
# ---------------------------------------------------------------------------


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("authenticated"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)

    return decorated


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        if request.form.get("password", "") == APP_PASSWORD:
            session["authenticated"] = True
            session.permanent = True
            return redirect(url_for("index"))
        error = "Mot de passe incorrect."
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ---------------------------------------------------------------------------
# Page principale
# ---------------------------------------------------------------------------


@app.route("/")
@login_required
def index():
    return render_template("index.html")


@app.route("/health")
def health():
    return "ok", 200


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _int(val):
    try:
        return int(val) if val else None
    except (TypeError, ValueError):
        return None


# Codes Pappers tranche_effectif → (min, max) salariés
_PAPPERS_TRANCHE = {
    "00": (0, 0),
    "01": (1, 2),
    "02": (3, 5),
    "03": (6, 9),
    "11": (10, 19),
    "12": (20, 49),
    "21": (50, 99),
    "22": (100, 199),
    "31": (200, 249),
    "32": (250, 499),
    "41": (500, 999),
    "42": (1000, 1999),
    "51": (2000, 4999),
    "52": (5000, 9999),
    "53": (10000, None),
}


def _parse_effectif(val):
    """Parse une valeur effectif vers (lo, hi) inclusive.
    hi=None = sans limite haute. (None, None) = inconnu.
    Gère : int, code Pappers ("02"), nombre ("42"),
           "Entre 3 et 5 salariés", "0 salarié", "10 000 et plus".
    """
    if val is None:
        return None, None
    if isinstance(val, int):
        return val, val
    s = str(val).strip()
    # Code Pappers à 2 caractères (ex: "02" = 3-5, "12" = 20-49)
    if s in _PAPPERS_TRANCHE:
        return _PAPPERS_TRANCHE[s]
    # Nombre simple
    try:
        n = int(s)
        return n, n
    except ValueError:
        pass
    # "Entre X et Y salariés" (champ effectif Pappers /entreprise)
    m = re.match(r"Entre\s+([\d\s]+)\s+et\s+([\d\s]+)", s)
    if m:
        return int(m.group(1).replace(" ", "")), int(m.group(2).replace(" ", ""))
    # "0 salarié" ou "X salarié(s)"
    m = re.match(r"^(\d+)\s+salarié", s)
    if m:
        n = int(m.group(1))
        return n, n
    # "X 000 et plus"
    m = re.match(r"^([\d\s]+)\s+et\s+plus", s)
    if m:
        return int(m.group(1).replace(" ", "")), None
    # "X à Y"
    m = re.match(r"^(\d+)\s+à\s+(\d+)$", s)
    if m:
        return int(m.group(1)), int(m.group(2))
    return None, None


def _filter_effectif(companies, min_val, max_val):
    """Filtre côté serveur sur _effectifs_finances.
    Parse les tranches Pappers ("10 à 19", "10000 et plus").
    Inclut les entreprises sans effectif connu (inconnu ≠ exclu).
    """
    if min_val is None and max_val is None:
        return companies
    result = []
    for c in companies:
        lo, hi = _parse_effectif(c.get("_effectifs_finances"))
        if lo is None:
            # Effectif inconnu → on garde
            result.append(c)
            continue
        # Exclure si toute la tranche est au-dessus du max demandé
        if max_val is not None and lo > max_val:
            continue
        # Exclure si toute la tranche est en-dessous du min demandé
        # hi=None ("et plus") ne peut pas être en-dessous d'un min
        if min_val is not None and hi is not None and hi < min_val:
            continue
        result.append(c)
    return result


def _filter_ca(companies, ca_min, ca_max):
    """Filtre côté serveur sur chiffre_affaires.
    Nécessaire quand la source est data.gouv.fr (pas de filtre CA natif).
    Inclut les entreprises sans CA connu.
    """
    if ca_min is None and ca_max is None:
        return companies
    result = []
    for c in companies:
        val = c.get("chiffre_affaires")
        if not val:
            result.append(c)
            continue
        try:
            n = int(val)
            if ca_min is not None and n < ca_min:
                continue
            if ca_max is not None and n > ca_max:
                continue
            result.append(c)
        except (TypeError, ValueError):
            result.append(c)
    return result


def _filter_age_dirigeant(companies, min_age):
    """Filtre côté serveur sur age_dirigeant. Inclut les entreprises sans âge connu."""
    if min_age is None:
        return companies
    result = []
    for c in companies:
        age = c.get("age_dirigeant")
        if not age:
            result.append(c)
            continue
        try:
            if int(age) >= min_age:
                result.append(c)
        except (TypeError, ValueError):
            result.append(c)
    return result


def _filter_resultat_net(companies, min_val, max_val):
    """Filtre côté serveur sur _resultat_net. Inclut les entreprises sans valeur connue."""
    if min_val is None and max_val is None:
        return companies
    result = []
    for c in companies:
        val = c.get("_resultat_net")
        if val is None:
            result.append(c)
            continue
        try:
            n = int(val)
            if min_val is not None and n < min_val:
                continue
            if max_val is not None and n > max_val:
                continue
            result.append(c)
        except (TypeError, ValueError):
            result.append(c)
    return result


# ---------------------------------------------------------------------------
# API — Recherche
# ---------------------------------------------------------------------------


@app.route("/api/search", methods=["POST"])
@login_required
def api_search():
    data = request.get_json(silent=True) or {}
    secteur = data.get("secteur", "").strip()

    # Validation : au moins un filtre requis
    if not any([
        secteur,
        data.get("region"),
        data.get("departement"),
        data.get("ca_min"),
        data.get("ca_max"),
        data.get("ville"),
        data.get("nom_entreprise"),
        data.get("nom_dirigeant"),
        data.get("prenom_dirigeant"),
    ]):
        return jsonify({"error": "Veuillez renseigner au moins un critère de recherche."}), 400

    args = SimpleNamespace(
        secteur=secteur,
        region=data.get("region", "").strip() or None,
        departement=data.get("departement", "").strip() or None,
        ca_min=_int(data.get("ca_min")),
        ca_max=_int(data.get("ca_max")),
        age_min_dirigeant=_int(data.get("age_min_dirigeant")),
        max_resultats=_int(data.get("max_resultats")) or 20,
        nom_entreprise=data.get("nom_entreprise", "").strip() or None,
        nom_dirigeant=data.get("nom_dirigeant", "").strip() or None,
        prenom_dirigeant=data.get("prenom_dirigeant", "").strip() or None,
        # Filtres avancés
        categorie_juridique=data.get("forme_juridique", "").strip() or None,
        effectif_min=_int(data.get("effectif_min")),
        effectif_max=_int(data.get("effectif_max")),
        resultat_net_min=_int(data.get("resultat_net_min")),
        resultat_net_max=_int(data.get("resultat_net_max")),
        date_creation_min=data.get("date_creation_min", "").strip() or None,
        ville=data.get("ville", "").strip() or None,
        statut_rcs=data.get("statut_rcs", "").strip() or None,
        entreprise_cessee=not data.get("en_activite", True),
    )

    # Si des filtres serveur sont actifs, récupérer plus de résultats bruts
    user_max = args.max_resultats
    has_server_filters = any([
        args.effectif_min, args.effectif_max,
        args.resultat_net_min, args.resultat_net_max,
        args.age_min_dirigeant,
        args.ca_min, args.ca_max,
    ])
    if has_server_filters:
        args.max_resultats = min(user_max * 4, 80)

    # ── Source choisie par l'utilisateur ───────────────────────────────────
    use_pappers = data.get("use_pappers", False)
    pappers_search_calls = 0
    total_count = 0
    companies_raw: list[dict] = []

    if use_pappers:
        source = "Pappers"
        pappers_search_calls = 1
        try:
            companies_raw, total_count = search_pappers(args)
        except Exception as e:
            return jsonify({"error": f"Erreur API Pappers : {str(e)}"}), 502
    else:
        source = "data.gouv.fr"
        try:
            companies_raw, total_count = search_datagouv(args)
        except Exception as e:
            return jsonify({"error": f"Erreur data.gouv.fr : {str(e)}"}), 502

    fetched_count = len(companies_raw)

    # ── Détails Pappers /entreprise pour chaque résultat ───────────────────
    pappers_detail_calls = 0
    companies_info = []
    for company in companies_raw:
        siren = company.get("siren", "")
        details = {}
        if siren:
            details = get_company_details(siren)
            pappers_detail_calls += 1
            time.sleep(0.2)
        info = extract_company_info(company, details)
        info["source"] = source
        companies_info.append(info)

    # ── Filtres côté serveur ───────────────────────────────────────────────
    companies_info = _filter_ca(companies_info, args.ca_min, args.ca_max)
    companies_info = _filter_effectif(companies_info, args.effectif_min, args.effectif_max)
    companies_info = _filter_resultat_net(companies_info, args.resultat_net_min, args.resultat_net_max)
    companies_info = _filter_age_dirigeant(companies_info, args.age_min_dirigeant)
    companies_info = companies_info[:user_max]

    pappers_calls_total = pappers_search_calls + pappers_detail_calls

    # ── Nettoyage + données Fullenrich ─────────────────────────────────────
    clean = []
    for c in companies_info:
        row = {k: v for k, v in c.items() if not k.startswith("_")}
        if c.get("_prenom") or c.get("_nom"):
            row["_enrich"] = {
                "prenom": c.get("_prenom", ""),
                "nom": c.get("_nom", ""),
                "domain": c.get("_domaine", ""),
                "company_name": c.get("_nom_entreprise_raw", ""),
            }
        clean.append(row)

    return jsonify({
        "results": clean,
        "total": len(clean),
        "pappers_total": total_count,
        "fetched_count": fetched_count,
        "source": source,
        "pappers_calls": pappers_calls_total,
    })


# ---------------------------------------------------------------------------
# API — Export CSV
# ---------------------------------------------------------------------------


@app.route("/api/export", methods=["POST"])
@login_required
def api_export():
    data = request.get_json(silent=True) or {}
    results = data.get("results", [])

    output = io.StringIO()
    writer = csv.DictWriter(
        output, fieldnames=CSV_FIELDS, delimiter=";", extrasaction="ignore"
    )
    writer.writeheader()
    writer.writerows(results)

    # UTF-8 BOM pour Excel
    csv_bytes = ("\ufeff" + output.getvalue()).encode("utf-8")

    response = make_response(csv_bytes)
    response.headers["Content-Type"] = "text/csv; charset=utf-8"
    response.headers["Content-Disposition"] = (
        f"attachment; filename=entreprises_{int(time.time())}.csv"
    )
    return response


# ---------------------------------------------------------------------------
# API — Fullenrich
# ---------------------------------------------------------------------------


@app.route("/api/fullenrich/credits", methods=["GET"])
@login_required
def api_fullenrich_credits():
    """Retourne le solde de crédits Fullenrich."""
    try:
        resp = requests.get(
            f"{FULLENRICH_BASE_URL}/account/credits",
            headers={"Authorization": f"Bearer {_fullenrich_key()}"},
            timeout=15,
        )
        resp.raise_for_status()
        return jsonify({"balance": resp.json().get("balance")})
    except Exception as e:
        return jsonify({"error": str(e)}), 502


@app.route("/api/fullenrich/enrich", methods=["POST"])
@login_required
def api_fullenrich_enrich():
    """Soumet des contacts à Fullenrich et attend les résultats (polling serveur)."""
    data = request.get_json(silent=True) or {}
    contacts = data.get("contacts", [])

    if not contacts:
        return jsonify({"error": "Aucun contact à enrichir."}), 400

    enrich_type = data.get("enrich_type", "both")
    if enrich_type not in ("both", "email", "phone"):
        enrich_type = "both"

    try:
        result = _do_fullenrich_enrich(contacts, enrich_type)
        return jsonify(result)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except TimeoutError as e:
        return jsonify({"error": str(e)}), 504
    except requests.exceptions.HTTPError as e:
        return jsonify({"error": f"Erreur Fullenrich ({e.response.status_code})"}), 502
    except Exception as e:
        return jsonify({"error": f"Erreur Fullenrich : {str(e)}"}), 502


def _do_fullenrich_enrich(contacts: list[dict], enrich_type: str = "both") -> dict:
    """
    Soumet les contacts en bulk à Fullenrich, poll jusqu'à FINISHED.
    contacts : [{prenom, nom, domain, company_name}, ...]
    enrich_type : "both" | "email" | "phone"
    Retourne : {enriched: [{index, email, mobile}], credits_used, total_submitted}
    """
    if enrich_type == "email":
        enrich_fields = ["contact.emails"]
    elif enrich_type == "phone":
        enrich_fields = ["contact.phones"]
    else:
        enrich_fields = ["contact.emails", "contact.phones"]

    auth_headers = {
        "Authorization": f"Bearer {_fullenrich_key()}",
        "Content-Type": "application/json",
    }

    # Construction du payload
    payload_data = []
    for c in contacts:
        first_name   = (c.get("prenom")       or "").strip()
        last_name    = (c.get("nom")           or "").strip()
        domain       = (c.get("domain")        or "").strip()
        company_name = (c.get("company_name")  or "").strip()

        # Fullenrich exige first_name ET last_name non vides
        if not first_name and not last_name:
            raise ValueError("Prénom et nom requis pour l'enrichissement (contact sans nom renseigné).")
        if not first_name:
            first_name = last_name
        if not last_name:
            last_name = first_name

        # Fullenrich exige domain OU company_name
        if not domain and not company_name:
            raise ValueError("Domaine ou nom d'entreprise requis pour l'enrichissement Fullenrich.")

        entry: dict = {
            "first_name":    first_name,
            "last_name":     last_name,
            "enrich_fields": enrich_fields,
        }
        if domain:
            entry["domain"] = domain
        else:
            entry["company_name"] = company_name
        linkedin_url = (c.get("linkedin_url") or "").strip()
        if linkedin_url:
            entry["linkedin_url"] = linkedin_url
        payload_data.append(entry)

    # Soumission
    resp = requests.post(
        f"{FULLENRICH_BASE_URL}/contact/enrich/bulk",
        json={"name": f"web-{int(time.time())}", "data": payload_data},
        headers=auth_headers,
        timeout=30,
    )
    resp.raise_for_status()
    enrichment_id = resp.json().get("enrichment_id") or resp.json().get("id", "")
    if not enrichment_id:
        raise ValueError("Pas d'identifiant d'enrichissement reçu.")

    # Polling
    for _ in range(FULLENRICH_POLL_MAX):
        time.sleep(FULLENRICH_POLL_INTERVAL)
        poll = requests.get(
            f"{FULLENRICH_BASE_URL}/contact/enrich/bulk/{enrichment_id}",
            headers={"Authorization": f"Bearer {_fullenrich_key()}"},
            timeout=30,
        )
        if poll.status_code == 402:
            raise ValueError("Crédits Fullenrich insuffisants (402).")
        if 400 <= poll.status_code < 500:
            raise ValueError(f"Erreur Fullenrich {poll.status_code} : {poll.text[:200]}")
        poll.raise_for_status()

        result = poll.json()
        status = result.get("status", "UNKNOWN").upper()

        if status == "FINISHED":
            enriched = []
            for i, record in enumerate(result.get("data", [])):
                contact_info = record.get("contact_info") or {}
                email = (
                    (contact_info.get("most_probable_work_email") or {}).get("email")
                    or (contact_info.get("most_probable_personal_email") or {}).get("email")
                    or ""
                )
                mobile = (contact_info.get("most_probable_phone") or {}).get("number", "")
                enriched.append({"index": i, "email": email, "mobile": mobile})
            return {
                "enriched": enriched,
                "credits_used": result.get("cost", {}).get("credits", 0),
                "total_submitted": len(contacts),
            }

        if status in ("CANCELED", "CREDITS_INSUFFICIENT", "RATE_LIMIT"):
            raise ValueError(f"Enrichissement interrompu : {status}")

    raise TimeoutError("Timeout : résultats Fullenrich non reçus.")


# ---------------------------------------------------------------------------
# Supabase
# ---------------------------------------------------------------------------

_supa = None


def _supabase():
    global _supa
    if _supa is None:
        url = os.environ.get("SUPABASE_URL", "")
        key = os.environ.get("SUPABASE_KEY", "")
        if url and key:
            _supa = _mk_supabase(url, key)
    return _supa


# ---------------------------------------------------------------------------
# Page Vendeurs
# ---------------------------------------------------------------------------


@app.route("/vendeurs")
@login_required
def vendeurs_page():
    return render_template("vendeurs.html")


# ---------------------------------------------------------------------------
# API — Vendeurs CRUD
# ---------------------------------------------------------------------------

VENDEUR_CSV_FIELDS = [
    "nom_entreprise", "siren", "ca", "resultat_net", "secteur",
    "adresse", "site_web", "nom_dirigeant", "age_dirigeant",
    "email", "telephone", "statut", "raison_cession", "notes",
    "lien_pappers", "created_at",
]


@app.route("/api/vendeurs", methods=["GET"])
@login_required
def api_vendeurs_list():
    supa = _supabase()
    if not supa:
        return jsonify({"error": "Supabase non configuré"}), 500
    try:
        res = supa.table("vendeurs").select("*").order("created_at", desc=True).execute()
        return jsonify({"vendeurs": res.data})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/vendeurs/export", methods=["GET"])
@login_required
def api_vendeurs_export():
    supa = _supabase()
    if not supa:
        return jsonify({"error": "Supabase non configuré"}), 500
    try:
        res = supa.table("vendeurs").select("*").order("created_at", desc=True).execute()
        vendeurs = res.data
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    output = io.StringIO()
    writer = csv.DictWriter(
        output, fieldnames=VENDEUR_CSV_FIELDS, delimiter=";", extrasaction="ignore"
    )
    writer.writeheader()
    writer.writerows(vendeurs)
    csv_bytes = ("\ufeff" + output.getvalue()).encode("utf-8")
    response = make_response(csv_bytes)
    response.headers["Content-Type"] = "text/csv; charset=utf-8"
    response.headers["Content-Disposition"] = (
        f"attachment; filename=vendeurs_{int(time.time())}.csv"
    )
    return response


@app.route("/api/vendeurs", methods=["POST"])
@login_required
def api_vendeurs_create():
    data = request.get_json(force=True)
    supa = _supabase()
    if not supa:
        return jsonify({"error": "Supabase non configuré"}), 500

    row = {
        "nom_entreprise": data.get("nom_entreprise") or "",
        "siren":          data.get("siren") or "",
        "ca":             _int(data.get("ca") or data.get("chiffre_affaires")),
        "resultat_net":   _int(data.get("resultat_net")),
        "secteur":        data.get("secteur") or "",
        "adresse":        data.get("adresse") or "",
        "site_web":       data.get("site_web") or "",
        "lien_pappers":   data.get("lien_pappers") or data.get("pappers_url") or "",
        "nom_dirigeant":  data.get("nom_dirigeant") or "",
        "age_dirigeant":  _int(data.get("age_dirigeant")),
        "email":          data.get("email") or data.get("email_dirigeant") or "",
        "telephone":      data.get("telephone") or data.get("mobile_dirigeant") or "",
        "statut":         data.get("statut") or "prospect",
        "raison_cession": data.get("raison_cession") or "",
        "notes":          data.get("notes") or "",
    }
    row = {k: v for k, v in row.items() if v is not None and v != ""}

    try:
        siren = row.get("siren")
        if siren:
            existing = supa.table("vendeurs").select("id").eq("siren", siren).execute()
            if existing.data:
                return jsonify({
                    "error": f"SIREN {siren} déjà dans la base.",
                    "duplicate": True,
                }), 409
        res = supa.table("vendeurs").insert(row).execute()
        return jsonify({"vendeur": res.data[0] if res.data else {}}), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/vendeurs/<vid>", methods=["PATCH"])
@login_required
def api_vendeurs_update(vid):
    data = request.get_json(force=True)
    supa = _supabase()
    if not supa:
        return jsonify({"error": "Supabase non configuré"}), 500

    allowed = {
        "statut", "notes", "email", "telephone",
        "raison_cession", "nom_entreprise", "ca", "secteur", "mandat",
    }
    update = {k: v for k, v in data.items() if k in allowed}
    if not update:
        return jsonify({"error": "Aucun champ modifiable fourni."}), 400

    try:
        res = supa.table("vendeurs").update(update).eq("id", vid).execute()
        return jsonify({"vendeur": res.data[0] if res.data else {}})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/vendeurs/<vid>", methods=["DELETE"])
@login_required
def api_vendeurs_delete(vid):
    supa = _supabase()
    if not supa:
        return jsonify({"error": "Supabase non configuré"}), 500
    try:
        supa.table("vendeurs").delete().eq("id", vid).execute()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/vendeurs/<vid>/enrich", methods=["POST"])
@login_required
def api_vendeurs_enrich(vid):
    data = request.get_json(force=True)
    enrich_type = data.get("enrich_type", "both")
    if enrich_type not in ("both", "email", "phone"):
        enrich_type = "both"

    supa = _supabase()
    if not supa:
        return jsonify({"error": "Supabase non configuré"}), 500

    try:
        res = supa.table("vendeurs").select("*").eq("id", vid).single().execute()
        vendeur = res.data
    except Exception as e:
        return jsonify({"error": f"Vendeur non trouvé : {e}"}), 404

    nom_full = (vendeur.get("nom_dirigeant") or "").strip()
    parts = nom_full.split()
    prenom = parts[0] if len(parts) >= 2 else ""
    nom    = " ".join(parts[1:]) if len(parts) >= 2 else nom_full

    contacts = [{
        "prenom":       prenom,
        "nom":          nom,
        "domain":       vendeur.get("site_web") or "",
        "company_name": vendeur.get("nom_entreprise") or "",
    }]

    try:
        result = _do_fullenrich_enrich(contacts, enrich_type)
    except (ValueError, TimeoutError) as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 502

    enriched = result.get("enriched", [{}])
    e0 = enriched[0] if enriched else {}
    update = {}
    if e0.get("email"):
        update["email"] = e0["email"]
    if e0.get("mobile"):
        update["telephone"] = e0["mobile"]
    if update:
        supa.table("vendeurs").update(update).eq("id", vid).execute()

    return jsonify({
        "email":        update.get("email", ""),
        "telephone":    update.get("telephone", ""),
        "credits_used": result.get("credits_used", 0),
    })


# ---------------------------------------------------------------------------
# Page Contacts (Fullenrich Search)
# ---------------------------------------------------------------------------


@app.route("/contacts")
@login_required
def contacts_page():
    return render_template("contacts.html")


@app.route("/api/contacts/search", methods=["POST"])
@login_required
def api_contacts_search():
    data = request.get_json(silent=True) or {}

    def _filters(raw, exact=False):
        """Convertit une chaîne (séparée par virgules/sauts de ligne) en [{value, exact_match, exclude}]."""
        if not raw:
            return None
        cleaned = str(raw).replace(",", "\n")
        items = [v.strip() for v in cleaned.split("\n") if v.strip()]
        if not items:
            return None
        return [{"value": v, "exact_match": exact, "exclude": False} for v in items]

    payload = {}

    # Nom / prénom → person_names
    firstname = (data.get("firstname") or "").strip()
    lastname  = (data.get("lastname")  or "").strip()
    if firstname or lastname:
        name_value = " ".join(filter(None, [firstname, lastname]))
        payload["person_names"] = [{"value": name_value, "exact_match": False, "exclude": False}]

    if data.get("company_names"):
        f = _filters(data["company_names"])
        if f:
            payload["current_company_names"] = f

    if data.get("company_domains"):
        f = _filters(data["company_domains"])
        if f:
            payload["current_company_domains"] = f

    if data.get("position_titles"):
        f = _filters(data["position_titles"])
        if f:
            payload["current_position_titles"] = f

    if data.get("seniority_levels"):
        levels = data["seniority_levels"] if isinstance(data["seniority_levels"], list) else [data["seniority_levels"]]
        levels = [lv for lv in levels if lv]
        if levels:
            payload["current_position_seniority_level"] = [
                {"value": lv, "exact_match": True, "exclude": False} for lv in levels
            ]

    if data.get("location"):
        f = _filters(data["location"])
        if f:
            payload["person_locations"] = f

    if data.get("industry"):
        f = _filters(data["industry"])
        if f:
            payload["current_company_industries"] = f

    payload["limit"] = min(_int(data.get("limit")) or 20, 100)
    payload["offset"] = _int(data.get("offset")) or 0

    # Au moins un filtre substantiel requis
    if not any(payload.get(k) for k in (
        "current_company_names", "current_company_domains",
        "current_position_titles", "person_names",
        "current_position_seniority_level", "person_locations",
    )):
        return jsonify({"error": "Renseignez au moins un nom d'entreprise, un domaine ou un titre de poste."}), 400

    try:
        resp = requests.post(
            f"{FULLENRICH_BASE_URL}/people/search",
            json=payload,
            headers={
                "Authorization": f"Bearer {_fullenrich_key()}",
                "Content-Type": "application/json",
            },
            timeout=30,
        )
        resp.raise_for_status()
        return jsonify(resp.json())
    except requests.exceptions.HTTPError as e:
        body = e.response.text[:300] if e.response else ""
        return jsonify({"error": f"Erreur Fullenrich ({e.response.status_code}) : {body}"}), 502
    except Exception as e:
        return jsonify({"error": str(e)}), 502


# ---------------------------------------------------------------------------
# Page Acheteurs
# ---------------------------------------------------------------------------


@app.route("/acheteurs")
@login_required
def acheteurs_page():
    return render_template("acheteurs.html")


# ---------------------------------------------------------------------------
# API — Acheteurs CRUD
# ---------------------------------------------------------------------------

ACHETEUR_CSV_FIELDS = [
    "nom", "prenom", "titre", "entreprise", "email", "telephone",
    "secteurs_interet", "taille_cibles", "statut", "notes", "created_at",
]


@app.route("/api/acheteurs", methods=["GET"])
@login_required
def api_acheteurs_list():
    supa = _supabase()
    if not supa:
        return jsonify({"error": "Supabase non configuré"}), 500
    try:
        res = supa.table("acheteurs").select("*").order("created_at", desc=True).execute()
        return jsonify({"acheteurs": res.data})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/acheteurs/export", methods=["GET"])
@login_required
def api_acheteurs_export():
    supa = _supabase()
    if not supa:
        return jsonify({"error": "Supabase non configuré"}), 500
    try:
        res = supa.table("acheteurs").select("*").order("created_at", desc=True).execute()
        acheteurs = res.data
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    output = io.StringIO()
    writer = csv.DictWriter(
        output, fieldnames=ACHETEUR_CSV_FIELDS, delimiter=";", extrasaction="ignore"
    )
    writer.writeheader()
    writer.writerows(acheteurs)
    csv_bytes = ("\ufeff" + output.getvalue()).encode("utf-8")
    response = make_response(csv_bytes)
    response.headers["Content-Type"] = "text/csv; charset=utf-8"
    response.headers["Content-Disposition"] = (
        f"attachment; filename=acheteurs_{int(time.time())}.csv"
    )
    return response


@app.route("/api/acheteurs", methods=["POST"])
@login_required
def api_acheteurs_create():
    data = request.get_json(force=True)
    supa = _supabase()
    if not supa:
        return jsonify({"error": "Supabase non configuré"}), 500

    row = {
        "nom":              data.get("nom") or "",
        "prenom":           data.get("prenom") or "",
        "titre":            data.get("titre") or "",
        "entreprise":       data.get("entreprise") or "",
        "email":            data.get("email") or "",
        "telephone":        data.get("telephone") or "",
        "secteurs_interet": data.get("secteurs_interet") or "",
        "taille_cibles":    data.get("taille_cibles") or "",
        "notes":            data.get("notes") or "",
        "statut":           data.get("statut") or "prospect",
    }
    row = {k: v for k, v in row.items() if v is not None and v != ""}

    try:
        res = supa.table("acheteurs").insert(row).execute()
        return jsonify({"acheteur": res.data[0] if res.data else {}}), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/acheteurs/import", methods=["POST"])
@login_required
def api_acheteurs_import():
    """
    Reçoit une liste de noms d'entreprises et crée des prospects dans acheteurs.
    Fullenrich Search API ne permet pas de chercher des contacts par entreprise + titre
    seul — on crée des enregistrements vides que l'utilisateur enrichit ensuite.
    """
    data = request.get_json(silent=True) or {}
    entreprises = [e.strip() for e in (data.get("entreprises") or []) if e and e.strip()]
    if not entreprises:
        return jsonify({"error": "Aucune entreprise fournie."}), 400

    supa = _supabase()
    if not supa:
        return jsonify({"error": "Supabase non configuré"}), 500

    rows = [{"entreprise": nom, "statut": "prospect"} for nom in entreprises]
    try:
        res = supa.table("acheteurs").insert(rows).execute()
        inserted = len(res.data) if res.data else len(rows)
        return jsonify({"inserted": inserted, "total": len(entreprises)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/acheteurs/<aid>", methods=["PATCH"])
@login_required
def api_acheteurs_update(aid):
    data = request.get_json(force=True)
    supa = _supabase()
    if not supa:
        return jsonify({"error": "Supabase non configuré"}), 500

    allowed = {
        "statut", "notes", "email", "telephone",
        "nom", "prenom", "titre", "entreprise",
        "secteurs_interet", "taille_cibles", "mandat",
    }
    update = {k: v for k, v in data.items() if k in allowed}
    if not update:
        return jsonify({"error": "Aucun champ modifiable fourni."}), 400

    try:
        res = supa.table("acheteurs").update(update).eq("id", aid).execute()
        return jsonify({"acheteur": res.data[0] if res.data else {}})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/acheteurs/<aid>", methods=["DELETE"])
@login_required
def api_acheteurs_delete(aid):
    supa = _supabase()
    if not supa:
        return jsonify({"error": "Supabase non configuré"}), 500
    try:
        supa.table("acheteurs").delete().eq("id", aid).execute()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/acheteurs/<aid>/enrich", methods=["POST"])
@login_required
def api_acheteurs_enrich(aid):
    data = request.get_json(force=True)
    enrich_type = data.get("enrich_type", "both")
    if enrich_type not in ("both", "email", "phone"):
        enrich_type = "both"

    supa = _supabase()
    if not supa:
        return jsonify({"error": "Supabase non configuré"}), 500

    try:
        res = supa.table("acheteurs").select("*").eq("id", aid).single().execute()
        acheteur = res.data
    except Exception as e:
        return jsonify({"error": f"Acheteur non trouvé : {e}"}), 404

    contacts = [{
        "prenom":       acheteur.get("prenom") or "",
        "nom":          acheteur.get("nom") or "",
        "domain":       "",
        "company_name": acheteur.get("entreprise") or "",
    }]

    try:
        result = _do_fullenrich_enrich(contacts, enrich_type)
    except (ValueError, TimeoutError) as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 502

    enriched = result.get("enriched", [{}])
    e0 = enriched[0] if enriched else {}
    update = {}
    if e0.get("email"):
        update["email"] = e0["email"]
    if e0.get("mobile"):
        update["telephone"] = e0["mobile"]
    if update:
        supa.table("acheteurs").update(update).eq("id", aid).execute()

    return jsonify({
        "email":        update.get("email", ""),
        "telephone":    update.get("telephone", ""),
        "credits_used": result.get("credits_used", 0),
    })


# ---------------------------------------------------------------------------
# Lancement local
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_DEBUG", "0") == "1"
    app.run(host="0.0.0.0", port=port, debug=debug)
