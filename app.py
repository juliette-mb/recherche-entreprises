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


def _filter_effectif(companies, min_val, max_val):
    """Filtre côté serveur sur _effectifs_finances. Inclut les entreprises sans valeur connue."""
    if min_val is None and max_val is None:
        return companies
    result = []
    for c in companies:
        val = c.get("_effectifs_finances")
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

    # Recherche Pappers
    try:
        companies_raw = search_pappers(args)
    except Exception as e:
        return jsonify({"error": f"Erreur API Pappers : {str(e)}"}), 502

    # Récupération des détails (representants, finances…)
    companies_info = []
    for company in companies_raw:
        siren = company.get("siren", "")
        details = {}
        if siren:
            details = get_company_details(siren)
            time.sleep(0.3)
        companies_info.append(extract_company_info(company, details))

    # Filtres côté serveur
    companies_info = _filter_effectif(companies_info, args.effectif_min, args.effectif_max)
    companies_info = _filter_resultat_net(companies_info, args.resultat_net_min, args.resultat_net_max)

    # Nettoyage + ajout des données d'enrichissement Fullenrich
    clean = []
    for c in companies_info:
        row = {k: v for k, v in c.items() if not k.startswith("_")}
        # Inclure les données nécessaires à Fullenrich (non affichées dans le tableau)
        if c.get("_prenom") or c.get("_nom"):
            row["_enrich"] = {
                "prenom": c.get("_prenom", ""),
                "nom": c.get("_nom", ""),
                "domain": c.get("_domaine", ""),
                "company_name": c.get("_nom_entreprise_raw", ""),
            }
        clean.append(row)

    return jsonify({"results": clean, "total": len(clean)})


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
        entry: dict = {
            "first_name": c.get("prenom", ""),
            "last_name": c.get("nom", ""),
            "enrich_fields": enrich_fields,
        }
        if c.get("domain"):
            entry["domain"] = c["domain"]
        elif c.get("company_name"):
            entry["company_name"] = c["company_name"]
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
        "raison_cession", "nom_entreprise", "ca", "secteur",
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
# Lancement local
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_DEBUG", "0") == "1"
    app.run(host="0.0.0.0", port=port, debug=debug)
