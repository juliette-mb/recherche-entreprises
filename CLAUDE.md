# Deal Sourcing Platform — CRM M&A France

## Vision produit
Plateforme de deal sourcing M&A permettant de trouver, enrichir et gérer
des cibles de rachat (vendeurs) et des acheteurs potentiels.

## Utilisateurs
2-3 professionnels M&A (banquiers d'affaires, avocats)

## Fonctionnalités cibles

### Onglet Recherche Vendeurs
- Recherche d'entreprises françaises à racheter via API Pappers
- Filtres : secteur, région, CA min/max, résultat net min/max, âge dirigeant,
  nom/prénom dirigeant, nom entreprise, département, forme juridique, effectif min/max
- Sélection des résultats → ajout dans la base Vendeurs

### Onglet Recherche Acheteurs
- Recherche de contacts par entreprise + fonction via Fullenrich Search API
- Ex: "M&A Director chez Cegid"
- Sélection des résultats → ajout dans la base Acheteurs

### Onglet Base Vendeurs
- CRM des cibles à céder
- Fiche : nom entreprise, SIREN, CA, résultat net, secteur, dirigeant, âge, email,
  téléphone, site web, lien Pappers, raison de cession, notes, statut
- Statuts : prospect → contacté → intéressé → mandat signé
- Enrichissement email + mobile via Fullenrich Enrich API (sélection par ligne)
- Choix du type d'enrichissement : email / téléphone / les deux
- Bouton appel mobile (tel://)
- Export CSV

### Onglet Base Acheteurs
- CRM des acheteurs potentiels
- Fiche : nom, prénom, titre, entreprise, email, téléphone,
  secteurs d'intérêt, taille de cibles, notes, statut
- Statuts : prospect → contacté → intéressé → signé
- Bouton appel mobile (tel://)
- Export CSV

## Stack technique
- Backend : Flask (Python)
- Base de données : Supabase (PostgreSQL)
- APIs : Pappers, Fullenrich (Search + Enrich)
- Déploiement : Railway
- Auth : login par mot de passe simple

## Design
Sobre, professionnel, orienté desktop et mobile

---

## État actuel (v1)

L'outil existant implémente la partie **Recherche Vendeurs** :
- Interface web Flask avec login par mot de passe
- Formulaire de recherche (filtres de base + filtres avancés)
- Résultats en tableau avec sélection par checkbox
- Enrichissement Fullenrich avec choix email/téléphone/les deux
- Export CSV
- Déployé sur Railway : https://web-production-31c6.up.railway.app

## APIs utilisées

### Pappers API
- **Documentation** : https://www.pappers.fr/api/documentation
- **Base URL** : `https://api.pappers.fr/v2`
- **Endpoints** :
  - `GET /recherche` — recherche avec filtres (denomination, nom_dirigeant,
    prenom_dirigeant, region, code_naf, chiffre_affaires_min/max, age_dirigeant_min,
    departement, categorie_juridique, date_creation_min, statut_rcs, entreprise_cessee)
  - `GET /entreprise` — détail entreprise (SIREN) → finances, representants
- **Notes** :
  - Région : codes INSEE numériques (ex: "11" pour Île-de-France)
  - Dirigeants dans `/entreprise` : champ `representants` (pas `dirigeants`)
  - Résultat net : `finances[0].resultat`

### Fullenrich API v2
- **Base URL** : `https://app.fullenrich.com/api/v2`
- **Endpoints** :
  - `GET /account/credits` → `{balance: N}`
  - `POST /contact/enrich/bulk` → `{enrichment_id: "uuid"}`
  - `GET /contact/enrich/bulk/{id}` → `{status, data, cost}`
- **enrich_fields** : `["contact.emails"]`, `["contact.phones"]`, ou les deux
- **Statuts polling** : CREATED, IN_PROGRESS, FINISHED, CANCELED,
  CREDITS_INSUFFICIENT, RATE_LIMIT

## Clés API (Railway env vars)
- `PAPPERS_API_KEY` — lue via `_pappers_key()` (PAPPERS_TOKEN || PAPPERS_API_KEY)
- `FULLENRICH_API_KEY` — lue via `_fullenrich_key()`
- `APP_PASSWORD` — mot de passe login
- `SECRET_KEY` — clé Flask sessions

## Structure du projet
```
recherche-entreprises/
├── app.py                     # Flask : routes, helpers
├── recherche_entreprises.py   # Core : search_pappers, extract_company_info, Fullenrich
├── templates/
│   ├── login.html
│   └── index.html             # UI complète (formulaire + tableau + enrichissement)
├── requirements.txt           # requests, flask, gunicorn, python-dotenv
├── Procfile
├── railway.toml
├── .env                       # Local (gitignored)
└── CLAUDE.md
```
