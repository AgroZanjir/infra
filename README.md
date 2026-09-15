# Agro Zanjir Digital — infra

How the platform is served. Two applications, one database, one web server.

| Repository | What it is |
| --- | --- |
| [AgroZanjir/backend](https://github.com/AgroZanjir/backend) | Django 6 + DRF: the lot registry, the event log, the six clusters and the ports |
| [AgroZanjir/frontend](https://github.com/AgroZanjir/frontend) | Vite + React: the public website and the eight operator panels |
| [AgroZanjir/infra](https://github.com/AgroZanjir/infra) | this one |

## The shape of it

```
                    ┌─────────────────────────────────────────┐
   https://          │ nginx                                   │
   agrozanjir.uz ───▶│  /            → /srv/agrozanjir/web     │  static bundle
                     │  /api/, /admin/, /static/ → 127.0.0.1:8000
                     └───────────────────────┬─────────────────┘
                                             │
                                   ┌─────────▼──────────┐
                                   │ gunicorn           │  3 workers
                                   │ config.wsgi        │
                                   └─────────┬──────────┘
                                             │
                                   ┌─────────▼──────────┐
                                   │ PostgreSQL 16      │
                                   └────────────────────┘
```

**One origin, on purpose.** The web client and the API answer on the same host,
which is what lets the refresh token stay a `SameSite=Lax` cookie. Splitting
them across `agrozanjir.uz` and `api.agrozanjir.uz` still works — they share a
registrable domain — but two genuinely different domains do not: the browser
will not send a Lax cookie with an XHR, sign-in appears to work and the next
reload signs the person out. If that is the topology you need, set
`REFRESH_COOKIE_SAMESITE=None` and `REFRESH_COOKIE_SECURE=True` together;
browsers accept `None` only over TLS.

## What is here

```
nginx/agrozanjir.conf        the reverse proxy and the static bundle
systemd/agrozanjir-api.service   gunicorn as a service
env/backend.env.example      every variable the API reads
env/frontend.env.example     the one the bundle is built with
postgres/docker-compose.yml  PostgreSQL 16, if you are not using a managed one
scripts/deploy.sh            pull, build, migrate, collectstatic, restart
scripts/first-run.sh         everything above plus the seeds, once
```

Nothing here is generated. Read a file before you run it — the paths, the
domain and the system user are yours to set, and each is marked `# CHANGE ME`.

## First deployment

```sh
# 0. a machine with PostgreSQL 16, Python 3.12+, Node 20+, nginx and certbot
sudo adduser --system --group agrozanjir
sudo mkdir -p /srv/agrozanjir && sudo chown agrozanjir:agrozanjir /srv/agrozanjir

# 1. the code
sudo -u agrozanjir git clone https://github.com/AgroZanjir/backend.git  /srv/agrozanjir/backend
sudo -u agrozanjir git clone https://github.com/AgroZanjir/frontend.git /srv/agrozanjir/frontend

# 2. the settings
sudo -u agrozanjir cp env/backend.env.example /srv/agrozanjir/backend/.env
sudo -u agrozanjir $EDITOR /srv/agrozanjir/backend/.env     # every CHANGE ME

# 3. the database, the bundle and the service
sudo ./scripts/first-run.sh

# 4. the web server
sudo cp nginx/agrozanjir.conf /etc/nginx/sites-available/agrozanjir
sudo ln -s /etc/nginx/sites-available/agrozanjir /etc/nginx/sites-enabled/
sudo certbot --nginx -d agrozanjir.uz -d www.agrozanjir.uz
sudo nginx -t && sudo systemctl reload nginx
```

`first-run.sh` ends by printing the accounts it created and their passwords.
That list is printed once and stored only as a hash; there is no way to read a
password back afterwards.

## Every deployment after the first

```sh
sudo ./scripts/deploy.sh
```

Pulls both repositories, installs what changed, runs migrations, rebuilds the
bundle, collects static files and restarts the API. It does **not** run the
seeds: `seed_demo` would refuse anyway, and `seed_accounts` would do nothing
unless asked to rotate.

## The four settings that are easy to get wrong

Each fails in a way that does not obviously point at it, which is why they are
listed together:

| Setting | Wrong looks like |
| --- | --- |
| `VITE_API_BASE_URL` | Vite inlines it at **build** time; changing the API's address means rebuilding the bundle, not restarting anything |
| `CORS_ALLOWED_ORIGINS` | every request fails in the browser and succeeds in `curl` |
| `CSRF_TRUSTED_ORIGINS` | reads work, writes come back 403 |
| `REFRESH_COOKIE_SAMESITE` | signing in works, reloading the page signs you out |

And one that is not a setting: **the SPA fallback**. Every unknown path must be
rewritten to `index.html`, or a reader who reloads on `/showroom/melon` gets a
404 from nginx. That is the `try_files` line in the config.

## Before it faces the internet

- **`ONEID_ADAPTER` must not be `stub`.** The stub resolves a sign-in from a
  username alone - no password, no proof - and `/auth/personas/` lists the
  usernames with their roles. Two unauthenticated requests and the caller is
  the platform owner. `check --deploy` errors on this and the deploy scripts
  stop, the endpoint refuses on its own when `DEBUG=False`, and the persona
  list comes back empty. Password sign-in is unaffected.
- `DEBUG=False` and a real `DJANGO_SECRET_KEY` — 50+ random characters, not the
  development default. With `DEBUG=False` the security settings switch on by
  themselves: TLS redirect, HSTS, secure cookies, `X-Frame-Options: DENY`.
- `manage.py check --deploy` reports **no issues**. It is the gate.
- `seed_accounts --rotate`. The passwords a pilot was demonstrated with are not
  passwords for a public host.
- Decide about `seed_demo`. It loads the illustrative pilot dataset — real
  figures for a demonstration, and nothing you want in a production database.
  A real deployment runs `seed_reference` only.
- `CACHE_URL` pointing at a shared Redis if more than one worker runs. Every
  rate limit in this project counts in the cache - the sign-in doors, the
  assistant, the contact form - and the default is each process's own memory,
  so three workers enforce three times the limit.
- Restrict `/admin/`. It is the manual-adapter surface with full read and
  write over every organisation's data, and nginx currently serves it to
  anybody who asks. An `allow`/`deny` block on the office address, or a VPN,
  costs nothing and removes the whole surface.
- PostgreSQL, not the SQLite fallback. Two tables are written on every lot
  movement, and SQLite locks the file for each one.
- Back up the database before every deploy. `lot_event` is append-only and
  hash-chained: it is the record the whole platform's credibility rests on, and
  it cannot be reconstructed from anything else.
- Decide about the assistant. It is off unless `ANTHROPIC_API_KEY` is set, and
  it says so rather than pretending. On, it costs money per question from the
  open website, which is why `ASSISTANT_BURST_RATE` and `ASSISTANT_HOUR_RATE`
  exist — and why `CACHES` should be shared if more than one worker runs, or
  each worker enforces its own copy of the limit.
- Somebody has to read the enquiries. The contact form writes to
  `website_enquiry`, visible in the admin under **Public website**. A form
  nobody reads is worse than no form, because the sender believes it arrived.
- `backend/media/` exists and belongs to the service user, and nginx serves
  `/media/`. `first-run.sh` creates it. The vault keeps the key and the
  checksum in the database and the bytes here, so this directory is part of
  the backup, not a cache — losing it loses the evidence photographs while
  leaving every row that points at them.
- The uploaded file's URL is its only guard: nginx serves `/media/` without
  asking who is asking, and the name is 128 random bits. That is the trade for
  a pilot on a filesystem. Moving the vault to S3 makes it a signed URL, and
  `Document.url` is the one place that changes.
