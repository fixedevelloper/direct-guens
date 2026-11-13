# Camair-Co Flight Scraper

Scrape les vols et tarifs (XAF) du site Camair-Co sur 30 jours glissants pour 11 aéroports
(`BFX BZV COO DLA GOU LBV MVR NGJ NGE PNR NSI`), les stocke dans **MySQL** et les expose via une **API REST (FastAPI)**.
Le scraping est automatique (par défaut toutes les 6 h) et déclenchable à la main.

```
 Camair-Co (iframe Kiusys)         ┌────────────────────── docker compose ──────────────────────┐
        ▲  Playwright (Chromium)   │  app (FastAPI + APScheduler + Playwright) ──►  mysql 8.4     │
        └──────────────────────────┤        :8776  (127.0.0.1)                      :3306 interne  │
                                   └──────────────────────────────────────────────  127.0.0.1:3307 ┘
```

---

## 1. Prérequis du serveur

- Linux (Ubuntu 22.04/24.04 ou équivalent), **2 Go de RAM minimum** (Chromium), 5 Go de disque libre.
- Accès sortant HTTPS vers `www.camair-co.cm` et `*.kiusys.net`.
- Docker Engine 24+ et Docker Compose v2 :

```bash
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER && newgrp docker
docker --version && docker compose version
```

## 2. Installation

```bash
# 1. Copier le projet sur le serveur (git clone, scp ou rsync)
rsync -av --exclude .venv --exclude __pycache__ ./scrape_flight/ user@SERVEUR:/opt/scrape_flight/
ssh user@SERVEUR && cd /opt/scrape_flight

# 2. Configurer
cp .env.example .env
nano .env          # CHANGER les mots de passe MySQL

# 2b. Générer la clé API
sed -i "s|^API_KEY=.*|API_KEY=$(openssl rand -hex 32)|" .env
API_KEY=215860236ced3a5d90a0af3635d288606699cca1d7573b8e3429a1e80a4e1539
# 3. Construire et démarrer (le premier build télécharge l'image Playwright, ~2 Go)
docker compose up -d --build

# 4. Vérifier
docker compose ps
curl http://127.0.0.1:8776/          # {"status":"ok"}
```

Fichiers du projet : `app_scraper.py` (scraping + API), `db.py` (MySQL), `Dockerfile`, `docker-compose.yml`, `.env`.

## 3. Configuration (`.env`)

| Variable | Défaut | Rôle |
|---|---|---|
| `MYSQL_ROOT_PASSWORD`, `MYSQL_PASSWORD` | `rootpass`, `camair` | **À changer en production** |
| `API_KEY` | *(obligatoire)* | Clé exigée dans l'en-tête `X-API-Key` (≥ 16 car.). Générer : `openssl rand -hex 32` |
| `ENABLE_DOCS` | `false` | `true` expose `/docs` (Swagger) ; à éviter en production |
| `MYSQL_PORT` | `3307` | Port MySQL exposé sur `127.0.0.1` de l'hôte |
| `APP_BIND` / `APP_PORT` | `127.0.0.1` / `8776` | Interface et port de l'API |
| `SCRAPE_INTERVAL_HOURS` | `6` | Fréquence du scraping automatique (mode `auto`) |
| `REFRESH_DAYS` | `5` | Mode refresh : aujourd'hui → J+4, tarifs remis à jour à chaque cycle |
| `EXTEND_DAYS` | `5` | Mode extend : nombre max de jours manquants ajoutés par cycle (jusqu'à `DAYS_AHEAD`) |
| `FULL_SCRAPE_DAY` / `FULL_SCRAPE_HOUR` | `sun` / `2` | Scraping complet hebdomadaire (jour et heure, fuseau du conteneur : UTC) |
| `RUN_ON_STARTUP` | `false` | Lance un scraping dès le démarrage |
| `CONCURRENCY` | `2` | Navigateurs en parallèle (plus = plus rapide, plus risqué : blocage, RAM) |
| `DAYS_AHEAD` | `30` | Nombre de jours scrapés |
| `AIRPORTS` | les 11 codes | Restreindre pour un test (`DLA,NSI`) |

Après modification : `docker compose up -d`.

### Modes de scraping

| Mode | Fenêtre | Quand |
|---|---|---|
| `auto` (défaut) | `refresh` + `extend` | toutes les `SCRAPE_INTERVAL_HOURS` ; devient `full` au tout premier lancement (base vide) |
| `refresh` | aujourd'hui → J+4 | tarifs proches du départ remis à jour |
| `extend` | jours manquants après la dernière date couverte (5 max, plafonné à J+`DAYS_AHEAD`) | maintient la fenêtre glissante de 30 jours |
| `full` | 30 jours | une fois par semaine (`FULL_SCRAPE_DAY`), ou à la demande |

Fonctionnement : pour chaque trajet, **une sonde** récupère la liste des dates avec vols ; seuls ces jours sont recherchés.
Les trajets inexistants sont mémorisés (revérifiés après 7 jours) et ne sont plus sondés. Chaque jour est enregistré dès qu'il est
lu : un échec (timeout, 429/503) **ne supprime jamais** les données existantes. Les vols passés sont purgés chaque jour.
Le premier scraping complet reste long (plusieurs heures) ; les cycles `auto` suivants sont bien plus courts.
Un seul scraping tourne à la fois ; un cycle planifié qui tombe pendant un autre est ignoré.

## 4. Exploitation

```bash
docker compose logs -f app                      # logs
KEY=$(grep ^API_KEY .env | cut -d= -f2)
curl -X POST -H "X-API-Key: $KEY" "http://127.0.0.1:8776/api/scrape/start?mode=refresh"   # auto | refresh | extend | full
curl -H "X-API-Key: $KEY" http://127.0.0.1:8776/api/scrape/status          # état
docker compose restart app
docker compose down                             # arrêt, données conservées
docker compose down -v                          # arrêt + SUPPRESSION des données
git pull && docker compose up -d --build        # mise à jour du code
```

Sauvegarde MySQL :
```bash
docker compose exec -T mysql sh -c 'mysqldump -uroot -p"$MYSQL_ROOT_PASSWORD" camair' > backup_$(date +%F).sql
```
Les conteneurs ont `restart: unless-stopped` : ils redémarrent avec le serveur (Docker activé : `sudo systemctl enable docker`).

## 5. Sécurité

- **Authentification par clé API** : toutes les routes `/api/*` exigent l'en-tête `X-API-Key: <API_KEY>`
  (401 sinon). Seule `GET /` (statut minimal) est publique. L'application **refuse de démarrer** sans `API_KEY`.
  Les pages `/docs` et `/openapi.json` sont désactivées par défaut.
- **Réseau** : l'API écoute sur `127.0.0.1` (`APP_BIND`) et n'est exposée que par nginx en HTTPS sur un sous-domaine (voir section 5bis).
- **Clé** : gardez `.env` hors du dépôt (`chmod 600 .env`), une clé distincte par application cliente si possible,
  et renouvelez-la en cas de fuite (changer `API_KEY` puis `docker compose up -d`).
- **MySQL** est publié seulement sur `127.0.0.1:3307` : ne l'ouvrez pas dans le pare-feu.
- Respectez les conditions d'utilisation du site Camair-Co.

---

## 5bis. Sous-domaine + reverse proxy nginx (HTTPS)

Sous-domaine utilisé : `scrapper.guenstravel.com`.

1. **DNS** : créer un enregistrement `A` (et `AAAA` si IPv6) `scrapper` (zone `guenstravel.com`) → IP publique du serveur (actuellement `145.239.75.143`). Vérifier : `dig +short scrapper.guenstravel.com`.
2. **Pare-feu** : ouvrir seulement 80 et 443 (`sudo ufw allow 'Nginx Full'`). Ne PAS ouvrir 8776 ni 3307.
3. **Installer nginx et certbot** :
   ```bash
   sudo apt install -y nginx certbot python3-certbot-nginx
   ```
4. **Configurer le site** avec `deploy/scrapper.guenstravel.com.conf` (déjà adapté au sous-domaine) :
   ```bash
   sudo cp deploy/scrapper.guenstravel.com.conf /etc/nginx/sites-available/scrapper.guenstravel.com
   sudo ln -s /etc/nginx/sites-available/scrapper.guenstravel.com /etc/nginx/sites-enabled/
   ```
   **Attention** : si vous laissez certbot modifier le fichier (`--nginx`), il greffe le SSL sur le premier bloc `server` et casse la config (boucle de redirection, plus de proxy). Préférez `sudo certbot certonly --webroot -w /var/www/html -d scrapper.guenstravel.com`, puis installez le fichier fourni tel quel.
   Le fichier référence des certificats qui n'existent pas encore : émettre d'abord le certificat en mode standalone/webroot, ou
   commenter temporairement le bloc `443`, puis :
   ```bash
   sudo nginx -t && sudo systemctl reload nginx
   sudo certbot --nginx -d scrapper.guenstravel.com        # certificat + renouvellement automatique
   sudo nginx -t && sudo systemctl reload nginx
   ```
5. **Tester** :
   ```bash
   curl -i https://scrapper.guenstravel.com/api/flights                    # 401 (pas de clé)
   curl -H "X-API-Key: $KEY" "https://scrapper.guenstravel.com/api/flights?origin=DLA&limit=2"   # 200
   curl -I http://scrapper.guenstravel.com/                                # 301 vers HTTPS
   ```

Ce que fait la configuration : redirection HTTP→HTTPS, HSTS, limitation à 10 req/s par IP, transmission de l'IP réelle,
exposition de `/api/` uniquement (`/docs` et le reste en 404), et possibilité de restreindre par IP (`allow`/`deny`).
Les applications distantes appellent alors `https://scrapper.guenstravel.com/api/flights` avec `X-API-Key`.
Les applications du même serveur peuvent continuer à utiliser `http://127.0.0.1:8776`.

---

## 6. Utiliser les données depuis une autre application du serveur

Trois options, de la plus simple à la plus directe.

### Option A — API HTTP (recommandée)

Application sur l'hôte (hors Docker) → `http://127.0.0.1:8776`, avec l'en-tête `X-API-Key` sur chaque appel.

| Endpoint | Description |
|---|---|
| `GET /` | Statut minimal (public) |
| `GET /api/info` | Infos du service |
| `GET /api/flights?origin=DLA&destination=NSI&date=2026-11-20&limit=100&offset=0` | Vols (tous les filtres sont optionnels) |
| `GET /api/scrape/status` | État du scraping |
| `POST /api/scrape/start?mode=auto\|refresh\|extend\|full` | Lance un scraping (409 si déjà en cours) |
| `GET /api/scrape/coverage` | Plage de dates couverte, jours par statut, trajets connus/inexistants |

Réponse de `/api/flights` :
```json
{"count": 3, "returned": 3, "flights": [{
  "origin": "NSI", "destination": "DLA", "origin_city": "Yaounde", "destination_city": "Douala",
  "flight_number": "QC0207", "flight_date": "2026-11-12", "departure_time": "20:30",
  "arrival_time": "21:15", "arrival_date": "2026-11-12", "duration": "45m", "stops": 0,
  "price_xaf": 104770.0, "availability": "available", "scraped_at": "2026-11-12T18:06:24"}]}
```
(`scraped_at` est en UTC.) Doc interactive `/docs` : uniquement si `ENABLE_DOCS=true`.

```python
import requests
r = requests.get("http://127.0.0.1:8776/api/flights", headers={"X-API-Key": API_KEY}, params={"origin": "DLA", "destination": "NSI", "date": "2026-11-20"}, timeout=10)
for f in r.json()["flights"]:
    print(f["flight_number"], f["departure_time"], f["price_xaf"])
```
```js
const res = await fetch("http://127.0.0.1:8776/api/flights?origin=DLA&destination=NSI", { headers: { "X-API-Key": process.env.FLIGHTS_API_KEY } });
const { flights } = await res.json();
```

**Application dans un autre conteneur Docker** : `127.0.0.1` y désigne le conteneur lui-même. Deux solutions :

1. Rejoindre le réseau du scraper. Dans le `docker-compose.yml` de l'autre application :
   ```yaml
   services:
     mon-app:
       image: ...
       environment:
         FLIGHTS_API: http://app:8000        # nom du service "app"
         FLIGHTS_API_KEY: ${FLIGHTS_API_KEY}  # même valeur que API_KEY
       networks: [default, camair]
   networks:
     camair:
       external: true
       name: scrape_flight_default           # <dossier>_default ; vérifier avec `docker network ls`
   ```
2. Ou passer par l'hôte : `extra_hosts: ["host.docker.internal:host-gateway"]` puis `http://host.docker.internal:8776`.
   Cela exige `APP_BIND=0.0.0.0` (ou l'IP du bridge Docker) et un pare-feu qui bloque le port 8776 depuis l'extérieur (`ufw deny 8776`).

### Option B — Lecture directe de la base MySQL

Utile pour des jointures ou des requêtes SQL. Créez un **utilisateur en lecture seule** (ne donnez jamais le compte d'écriture) :

```bash
docker compose exec mysql mysql -uroot -p camair -e "
  CREATE USER 'flights_ro'@'%' IDENTIFIED BY 'mot-de-passe-fort';
  GRANT SELECT ON camair.flights TO 'flights_ro'@'%';"
```
- Depuis l'hôte : `mysql -h127.0.0.1 -P3307 -uflights_ro -p camair`
  ou JDBC `jdbc:mysql://127.0.0.1:3307/camair`, SQLAlchemy `mysql+pymysql://flights_ro:***@127.0.0.1:3307/camair`.
- Depuis un autre conteneur : rejoindre le réseau (voir ci-dessus) et utiliser l'hôte `mysql`, port `3306`.

Table `flights` (clé d'unicité : `origin, destination, flight_date, flight_number, departure_time`) :
`origin, destination, origin_city, destination_city, flight_number, flight_date, departure_time, arrival_time, arrival_date, duration, stops, price_xaf, availability, scraped_at`.

```sql
-- Vol direct le moins cher DLA → NSI sur la période
SELECT flight_date, flight_number, departure_time, price_xaf
FROM flights WHERE origin='DLA' AND destination='NSI' AND stops=0
ORDER BY price_xaf LIMIT 5;
```
Ne **jamais écrire** dans cette table depuis une autre application : le scraper en est propriétaire et supprime
les lignes qu'il ne revoit plus.

### Option C — Fraîcheur des données

Pour savoir si les données sont récentes avant de les utiliser : `GET /api/scrape/status` (`last_finished_at`, `last_error`)
ou `SELECT MAX(scraped_at) FROM flights`. Les données ne sont mises à jour qu'à la fin d'un cycle réussi : en cas d'échec, elles restent celles du cycle précédent.

---

## 7. Dépannage

| Symptôme | Cause / solution |
|---|---|
| `address already in use` sur 3307/8776 | Changer `MYSQL_PORT` / `APP_PORT` dans `.env` |
| `flights_count: 0` après un scraping | Voir `docker compose logs app` ; si le site a changé, ajuster le parseur / les sélecteurs dans `app_scraper.py` (captures dans `debug/` du conteneur) |
| Beaucoup de `429/503` ou de timeouts | Baisser `CONCURRENCY`, augmenter `DELAY_MIN/DELAY_MAX` |
| Chromium plante / OOM | Ajouter de la RAM ou mettre `CONCURRENCY=1` |
| L'API répond 401 | En-tête `X-API-Key` absent ou différent de `API_KEY` |
| Le conteneur `app` s'arrête au démarrage | `API_KEY` absente ou < 16 caractères |
| L'API répond 409 sur `/start` | Un scraping est déjà en cours (voir `/api/scrape/status`) |
