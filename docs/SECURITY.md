# EQRF Security Notes

EQRF is designed for a trusted local network, but Admin sessions still need a strong Flask secret key and a non-obvious Admin password.

## EQRF_SECRET_KEY

`EQRF_SECRET_KEY` signs Flask session cookies. It protects Admin login sessions from cookie tampering. It must be unique per installation, long, random, and never committed to GitHub.

Generate one:

```bash
python scripts/generate_secret_key.py
```

The command prints a 64-character random hex string.

## EQRF_PASSWORD

`EQRF_PASSWORD` is the Admin login password. Use a real local Admin password, not `admin`, `password`, `change-me`, or another placeholder.

## Mac .env Setup

From the repo root:

```bash
cp .env.example .env
python scripts/generate_secret_key.py
```

Edit `.env`:

```text
EQRF_SECRET_KEY=paste-generated-key-here
EQRF_PASSWORD=your-admin-password
FLASK_DEBUG=0
FLASK_RUN_HOST=0.0.0.0
FLASK_RUN_PORT=8000
GUNICORN_WORKERS=2
EQRF_BACKUP_DIR=backups
```

`.env` is ignored by Git. Do not commit it.

## Linux / Beelink .env Setup

Production service deployments should use:

```text
/opt/EQRF/.env
```

Create it from the example:

```bash
cd /opt/EQRF
cp .env.example .env
python scripts/generate_secret_key.py
nano .env
```

After changing `.env`, restart the service:

```bash
sudo systemctl restart eqrf
sudo systemctl status eqrf
```

## Warnings

EQRF prints startup warnings and shows Admin production safety warnings when:

- `EQRF_SECRET_KEY` is missing, short, or a known placeholder.
- `EQRF_PASSWORD` is missing, short, or a known placeholder.
- `FLASK_DEBUG=1` is enabled.

Use `FLASK_DEBUG=0` for production-style Gunicorn/systemd operation.
