# Recherche Entreprises Françaises à Racheter

## Description

Outil en ligne de commande Python qui permet d'identifier des entreprises françaises potentiellement à racheter, en combinant les données financières de l'API Pappers avec l'enrichissement des contacts dirigeants via Fullenrich.

## Fonctionnement

1. **Saisie des critères** : l'utilisateur entre en ligne de commande les filtres de recherche
2. **Recherche Pappers** : interrogation de l'API Pappers pour trouver les entreprises correspondantes
3. **Collecte des données** : pour chaque entreprise, récupération du nom, SIREN, CA, effectif, adresse, site web, nom et âge du dirigeant
4. **Enrichissement Fullenrich** : récupération de l'email et du mobile de chaque dirigeant
5. **Export CSV** : toutes les données sont exportées dans un fichier CSV horodaté

## Critères de recherche disponibles

| Critère | Description | Exemple |
|---|---|---|
| `--secteur` | Secteur d'activité (mot-clé ou code NAF) | `plomberie` ou `4322A` |
| `--region` | Région française | `Île-de-France` |
| `--ca-min` | Chiffre d'affaires minimum (en €) | `2000000` |
| `--ca-max` | Chiffre d'affaires maximum (en €) | `6000000` |
| `--age-min-dirigeant` | Âge minimum du dirigeant (en années) | `55` |
| `--max-resultats` | Nombre maximum de résultats (défaut: 100) | `50` |
| `--output` | Nom du fichier CSV de sortie | `resultats.csv` |

## Colonnes du CSV exporté

- `nom_entreprise`
- `siren`
- `chiffre_affaires`
- `effectif`
- `adresse`
- `site_web`
- `nom_dirigeant`
- `age_dirigeant`
- `email_dirigeant`
- `mobile_dirigeant`

## Installation

```bash
pip install -r requirements.txt
```

## Utilisation

```bash
python recherche_entreprises.py \
  --secteur "plomberie" \
  --region "Île-de-France" \
  --ca-min 2000000 \
  --ca-max 6000000 \
  --age-min-dirigeant 55
```

### Autres exemples

```bash
# Recherche par code NAF
python recherche_entreprises.py --secteur "4322A" --region "Bretagne" --ca-min 500000

# Limiter le nombre de résultats et choisir le fichier de sortie
python recherche_entreprises.py --secteur "boulangerie" --max-resultats 50 --output boulangeries.csv

# Sans filtre de région
python recherche_entreprises.py --secteur "menuiserie" --ca-min 1000000 --ca-max 5000000 --age-min-dirigeant 60
```

## APIs utilisées

### Pappers API
- **Documentation** : https://www.pappers.fr/api/documentation
- **Base URL** : `https://api.pappers.fr/v2`
- **Endpoints utilisés** :
  - `GET /entreprises` — recherche d'entreprises avec filtres
  - `GET /entreprise` — détail d'une entreprise (SIREN)

### Fullenrich API
- **Documentation** : https://fullenrich.com
- **Base URL** : `https://api.fullenrich.com`
- **Endpoint utilisé** :
  - `POST /v1/enrich` — enrichissement d'un contact (nom + entreprise → email + mobile)

## Structure du projet

```
recherche-entreprises/
├── CLAUDE.md                  # Ce fichier
├── requirements.txt           # Dépendances Python
├── recherche_entreprises.py   # Script principal
└── resultats/                 # Dossier de sortie des CSV (créé automatiquement)
```

## Clés API

Les clés API sont définies directement dans le script. Pour les modifier, éditer les constantes en haut de `recherche_entreprises.py` :

```python
PAPPERS_API_KEY = "..."
FULLENRICH_API_KEY = "..."
```

## Notes

- L'API Pappers retourne les données financières les plus récentes disponibles au registre
- Fullenrich peut ne pas trouver de contact pour tous les dirigeants ; les champs seront vides dans ce cas
- Le script respecte les rate limits des APIs avec des délais entre les requêtes
- Les résultats sont paginés côté Pappers (100 entreprises par page maximum)
