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

from recherche_entreprises import (
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
        # Filtres avancés
        categorie_juridique=data.get("forme_juridique", "").strip() or None,
        effectif_min=_int(data.get("effectif_min")),
        effectif_max=_int(data.get("effectif_max")),
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

    # Filtre effectif côté serveur
    companies_info = _filter_effectif(companies_info, args.effectif_min, args.effectif_max)

    # Nettoyage des champs internes (préfixe "_")
    clean = [
        {k: v for k, v in c.items() if not k.startswith("_")}
        for c in companies_info
    ]

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
# Lancement local
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_DEBUG", "0") == "1"
    app.run(host="0.0.0.0", port=port, debug=debug)
