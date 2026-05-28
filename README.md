# Glasgow E-QRF

Glasgow E-QRF is a local Electronic Quick Reference Facility for operational checklists and PDF extracts. It is a Flask application designed for Mac development and local-network deployment, with iPads accessing the app over Wi-Fi. All operational content is stored locally inside the EQRF project.

## Overview

EQRF provides a lightweight web interface for:

- Published operational checklists.
- Categorised PDF extracts.
- General Reference / Quick Reference PDF documents.
- Local administration of PDFs, checklists, metadata, audit history, and refresh controls.

The public interface is intentionally local-content-only. It only displays checklists and extract links backed by the JSON data files and local PDF assets. The Admin interface is used to upload PDFs, manage checklist content, set governance metadata, inspect health warnings, and trigger connected clients to refresh.

The app is intended for trusted local network use, not public internet exposure.

## Current Features

- **Checklists**: nested checklist groups with large touchscreen-friendly checklist rows, progress tracking, reset controls, section dividers, and CAT A critical styling.
- **Extracts**: operationally categorised PDFs from `data/extracts.json`.
- **General Reference / Quick Reference PDFs**: uncategorised reference PDFs, including entries under `--`, root `__files__`, and qualifying legacy `MISC` entries.
- **PDF viewer**: direct local PDF viewing through vendored PDF.js files in `static/pdfjs/`; no CDN is required.
- **PDF controls**: 100% default zoom, Zoom +, Zoom -, Fit Width, Fit Height, Rotate, Reset, page count, and scrollable multi-page rendering.
- **PDF search**: in-document search is available only for General Reference / Quick Reference PDFs. Categorised operational extracts do not show search controls.
- **Responsive operational layout**: compact headers, toolbars, and fluid card grids designed for desktops, laptops, iPads, and smaller tablets.
- **Admin panel**: upload PDFs, edit extract metadata, create/edit/delete checklists, view health warnings, view audit log, and trigger client refresh.
- **Audit log**: append-only JSON audit trail viewable from Admin.
- **Governance metadata**: version, effective date, expiry date, review date, owner, status, and last updated values for extracts and checklists.
- **Production service runtime**: Gunicorn WSGI entry point, systemd service example, health check, backup scripts, and restart-after-crash service model.
- **Day/night mode**: localStorage-backed UI theme toggle across pages.
- **Local network use**: runs on a Mac, Raspberry Pi, mini PC, or other local server for iPads and desktops on the same network.
- **Client refresh behaviour**: Admin can trigger connected clients to refresh. Clients try to stay on the same page, or fall back to the nearest valid parent route.

## Technology Stack

- Python 3
- Flask
- Jinja templates
- JSON file storage
- Vendored PDF.js in `static/pdfjs/`
- `pypdf` for PDF page counts and text extraction/search cache
- Gunicorn WSGI runtime for production-style local-network serving
- pytest
- Gunicorn for Linux deployment
- systemd for Linux service management

## Project Structure

```text
app.py                  Main Flask application, routes, helpers, admin workflows.
wsgi.py                 Production WSGI entry point for Gunicorn.
data/                   Local JSON storage for extracts, checklists, audit log, and generated caches.
pdfs/                   Source PDF files served to the PDF.js viewer.
static/                 CSS, JavaScript, vendored PDF.js, images, and legacy JPG assets.
static/pdfjs/           Local PDF.js runtime files.
static/jpgs/            Legacy generated JPG pages. Not used by the current public PDF viewer.
templates/              Jinja templates for public UI, PDF viewer, login, and Admin.
tests/                  pytest test suite.
scripts/                Local helper scripts for Gunicorn startup, service install, health checks, and backups.
deploy/                 Deployment examples, including systemd service, health timer, and backup timer templates.
docs/                   Project maintenance documentation.
CLEANUP_REPORT.md       Notes from the latest conservative project cleanup pass.
requirements.txt        Python dependencies.
README.md               Project overview, setup, deployment, and maintenance guide.
AGENTS.md               Repository working instructions for future Codex runs.
CHANGELOG.md            Human-readable change history.
```

## Local Mac Development

The Flask development server is useful for local development only. It is not the recommended operational run method.

```bash
cd ~/Desktop
git clone https://github.com/ian8500/EQRF.git
cd EQRF
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python app.py
```

By default, `python app.py` listens on port `8000` and uses `FLASK_DEBUG=0` unless debug is explicitly enabled.

Useful environment variables:

```bash
export EQRF_SECRET_KEY="paste-generated-key-here"
export EQRF_PASSWORD="your-admin-password"
export FLASK_DEBUG=1
export FLASK_RUN_HOST=0.0.0.0
export FLASK_RUN_PORT=8000
export GUNICORN_WORKERS=2
export EQRF_BACKUP_DIR=backups
export EQRF_MAX_UPLOAD_MB=100
```

`.env.example` is included as a reference for the required values. The app loads `.env` through `python-dotenv`, then reads environment variables supplied by the shell, launch script, or systemd.

For normal development:

```bash
source venv/bin/activate
python -m pytest
python app.py
```

## Environment and Secrets

EQRF uses environment variables for local secrets. Do not hardcode or commit real secrets.

- `EQRF_SECRET_KEY`: Flask session signing key. This protects Admin login cookies from tampering.
- `EQRF_PASSWORD`: Admin login password.
- `FLASK_DEBUG`: set `1` only for development debugging. Use `0` for production-style service use.
- `.env`: local untracked environment file for Mac testing or `/opt/EQRF/.env` on Linux.
- `.env.example`: committed template with placeholders only.

Generate a strong secret key:

```bash
python scripts/generate_secret_key.py
```

Mac setup:

```bash
cp .env.example .env
python scripts/generate_secret_key.py
nano .env
```

Production Linux/Beelink setup uses:

```text
/opt/EQRF/.env
```

The systemd service reads that file through `EnvironmentFile=/opt/EQRF/.env`.

Unsafe values such as `change-me`, `change-this`, `admin`, `password`, `secret`, short secret keys, and short Admin passwords trigger startup and Admin health warnings. See [docs/SECURITY.md](docs/SECURITY.md).

## Production-Style Local Run

The proper local-network runtime is Gunicorn through `wsgi.py`.

Manual command:

```bash
source venv/bin/activate
gunicorn -w 2 -b 0.0.0.0:8000 wsgi:application
```

Both WSGI names are supported:

```bash
gunicorn wsgi:application
gunicorn wsgi:app
```

Mac/Linux helper script:

```bash
./scripts/run_production.sh
```

This starts the app at:

```text
http://127.0.0.1:8000
```

and on the local network at:

```text
http://SERVER-IP:8000
```

Health check:

```bash
curl http://127.0.0.1:8000/health
```

Expected response:

```json
{
  "status": "ok",
  "app": "EQRF",
  "mode": "production"
}
```

## Testing on iPad from Mac

1. Make sure the Mac and iPad are on the same Wi-Fi network.
2. Find the Mac IP address:

```bash
ipconfig getifaddr en0
```

3. Start EQRF with the production-style script:

```bash
source venv/bin/activate
./scripts/run_production.sh
```

4. Open the app on the iPad:

```text
http://MAC-IP:8000
```

Example:

```text
http://192.168.0.20:8000
```

The production-style script binds to `0.0.0.0` by default, so other local-network devices can reach it if the Mac firewall allows incoming connections.

## Linux / Micro Computer Deployment

EQRF can run on a Raspberry Pi, mini Linux PC, or other small local server. A wired Ethernet connection to the router is recommended for stable iPad access.

Recommended deployment shape:

- Reserve a DHCP address or configure a static IP for the server.
- Clone EQRF into `/opt/EQRF`.
- Run the app from a Python virtual environment.
- Use Gunicorn as the process runner.
- Use systemd to keep the app running after reboot.

EQRF should not be run in an open terminal for operational use. If a terminal window is closed, a foreground process stops. systemd runs EQRF as a managed service, restarts it if it crashes, and starts it again after a reboot.

Example setup:

```bash
sudo mkdir -p /opt
sudo chown "$USER":"$USER" /opt
cd /opt
git clone https://github.com/ian8500/EQRF.git
cd EQRF
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

Install Gunicorn if it is not already installed in the environment:

```bash
pip install -r requirements.txt
```

Run manually:

```bash
EQRF_SECRET_KEY="change-this" EQRF_PASSWORD="change-this" \
venv/bin/gunicorn -w 2 -b 0.0.0.0:8000 wsgi:application
```

Create the production environment file:

```bash
cp .env.example .env
nano .env
```

For production, edit at least `EQRF_SECRET_KEY` and `EQRF_PASSWORD`. `/opt/EQRF/.env` is read by the systemd service. Generate `EQRF_SECRET_KEY` with:

```bash
python scripts/generate_secret_key.py
```

An example service file is included at:

```text
deploy/eqrf.service.example
```

Copy it into place after reviewing the paths and service user:

```bash
sudo cp deploy/eqrf.service.example /etc/systemd/system/eqrf.service
sudo nano /etc/systemd/system/eqrf.service
```

The service runs:

```text
/opt/EQRF/venv/bin/gunicorn -w 2 -b 0.0.0.0:8000 wsgi:application
```

Example systemd service contents:

```ini
[Unit]
Description=EQRF local network application
After=network-online.target
Wants=network-online.target

[Service]
User=eqrf
Group=eqrf
WorkingDirectory=/opt/EQRF
EnvironmentFile=/opt/EQRF/.env
ExecStart=/opt/EQRF/venv/bin/gunicorn -w 2 -b 0.0.0.0:8000 wsgi:application
Restart=always
RestartSec=5
KillSignal=SIGTERM
TimeoutStopSec=30

# Hardening
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ReadWritePaths=/opt/EQRF/data /opt/EQRF/pdfs /opt/EQRF/static /opt/EQRF/backups
LimitNOFILE=4096

[Install]
WantedBy=multi-user.target
```

After enabling the service:

```bash
sudo systemctl daemon-reload
sudo systemctl enable eqrf
sudo systemctl start eqrf
sudo systemctl status eqrf
```

Useful service commands:

```bash
sudo systemctl status eqrf
sudo systemctl restart eqrf
sudo systemctl stop eqrf
sudo systemctl enable eqrf
journalctl -u eqrf -f
```

The helper script can install/update the service after confirmation:

```bash
./scripts/install_linux_service.sh
```

iPads then open:

```text
http://SERVER-IP:8000
```

Health check:

```bash
./scripts/check_health.sh
```

Optional health-check timer examples are included at:

```text
deploy/eqrf-healthcheck.service.example
deploy/eqrf-healthcheck.timer.example
```

They can be copied into `/etc/systemd/system/` if you want systemd to call `/health` periodically and restart the `eqrf` service when unhealthy.

## Admin Panel Guide

Open the login page from the top navigation:

```text
/login
```

Set the admin password with `EQRF_PASSWORD`. A successful login sets the session as admin and reveals Admin navigation.

Admin capabilities:

- Upload PDF extracts.
- Choose an existing category, type a new category path, or store as General Reference.
- Edit extract metadata.
- Create, edit, preview, and delete checklists.
- View registered extract and checklist trees.
- View content health warnings.
- View the audit log.
- Trigger connected clients to refresh.

Content governance fields:

- `version`
- `effective_date`
- `expiry_date`
- `review_date`
- `owner`
- `status`
- `last_updated`

Date fields support `YYYY-MM-DD` or `N/A`. `N/A` means no date restriction. Status values are:

- `published`
- `draft`
- `hidden`
- `archived`

Public pages only show published and currently effective content. Draft, hidden, archived, future-effective, expired, missing, or invalid content remains visible in Admin for repair.

## Content Data Model

EQRF uses JSON files rather than a database.

### Extracts

Extract metadata is stored in:

```text
data/extracts.json
```

Categorised extracts are nested by folder/category. Files live in `__files__` lists.

Example:

```json
{
  "AIR": {
    "SID": {
      "__files__": [
        {
          "pdf": "SID.pdf",
          "title": "SID",
          "orientation": "portrait",
          "page_count": 12,
          "version": "1.0",
          "effective_date": "2026-05-27",
          "expiry_date": "N/A",
          "review_date": "N/A",
          "owner": "N/A",
          "status": "published",
          "last_updated": "2026-05-27T12:00:00Z",
          "source": "admin"
        }
      ]
    }
  }
}
```

Legacy extract entries may still be strings:

```json
{
  "AIR": {
    "__files__": ["Parking.pdf"]
  }
}
```

The app normalises string entries in memory and keeps backwards compatibility.

General Reference / uncategorised extracts are collected from:

- the special `--` key
- root `__files__`
- qualifying legacy `MISC` entries used for blank-category uploads

### Checklists

Checklist data is stored in:

```text
data/checklists.json
```

Legacy checklist format:

```json
{
  "A380": {
    "Runway 05": [
      "--- Arrival ---",
      "Check item"
    ]
  }
}
```

Current metadata-aware checklist format:

```json
{
  "A380": {
    "Runway 05": {
      "__type__": "checklist",
      "metadata": {
        "title": "Runway 05",
        "version": "1.0",
        "effective_date": "2026-05-27",
        "expiry_date": "N/A",
        "review_date": "N/A",
        "owner": "N/A",
        "status": "published",
        "last_updated": "2026-05-27T12:00:00Z"
      },
      "items": [
        "--- Arrival ---",
        "Check item",
        "CAT A ONLY"
      ]
    }
  }
}
```

Legacy list checklists continue to render. Admin saves convert edited checklists into the metadata-aware format.

### Audit Log

Audit entries are stored in:

```text
data/audit_log.json
```

The audit log is append-only from the UI perspective.

## PDF Handling

The current viewer architecture uses original source PDFs directly.

- Source PDFs are stored in `pdfs/`.
- Public PDF viewing uses local PDF.js files in `static/pdfjs/`.
- The viewer route renders `templates/extracts_viewer.html`.
- The PDF file is served through `/pdfs/<filename>`.
- `/pdfs/<filename>` only serves registered local PDFs.
- Flask `send_from_directory(..., conditional=True)` is used so browsers can make efficient local requests.
- Public extract validity depends on the source PDF existing and governance metadata being public.
- Extract orientation metadata (`portrait` or `landscape`) controls the initial PDF.js display orientation. Reset View returns to that tagged orientation.

JPG rendering is no longer part of the runtime viewer or upload workflow.

Legacy JPG files may still exist in:

```text
static/jpgs/
```

Those files should not be treated as the primary viewing source. They can remain in the repository or backups for legacy compatibility, but missing JPGs are not a public visibility blocker.

In-document PDF search:

- Available only for General Reference / Quick Reference PDFs.
- Uses the current opened PDF only.
- Does not search categorised extracts.
- Does not search checklists.
- Does not perform OCR.
- Uses embedded PDF text via `pypdf` and a local JSON cache in `data/pdf_text_cache.json`.

Categorised extracts are displayed cleanly without search controls.

## Audit Log

The audit log is stored at:

```text
data/audit_log.json
```

Logged actions include:

- admin login success
- admin logout
- upload extract
- delete extract
- delete extract category
- refresh extract metadata
- create checklist
- edit checklist
- delete checklist
- update extract metadata
- update checklist metadata
- trigger client refresh

Admins can view recent activity from the Admin dashboard and the full audit page at:

```text
/admin/audit
```

## Security / Local Network Notes

EQRF is intended for trusted local-network use only.

Important notes:

- Do not expose the app directly to the public internet.
- Change the default admin password with `EQRF_PASSWORD`.
- Set a strong `EQRF_SECRET_KEY`.
- Admin health warnings are shown if `EQRF_SECRET_KEY` or `EQRF_PASSWORD` are missing, short, or known unsafe placeholder values.
- Never commit `.env` to GitHub.
- Keep the server operating system patched.
- Restrict access to the server and repository files.
- Back up `data/`, `pdfs/`, and any legacy assets regularly.
- Use local HTTPS only if your deployment requires it and you understand certificate management for the local network.
- Admin, upload, delete, metadata edit, trigger refresh, and audit routes require login.
- Destructive operations are POST-only and login-protected. A lightweight CSRF token layer is still a future hardening item.
- Uploads are capped by `EQRF_MAX_UPLOAD_MB`, defaulting to 100 MB.

## Backup

Back up at least:

- `data/`
- `pdfs/`
- `static/jpgs/` if legacy JPG assets still matter to your deployment

Example backup command:

```bash
./scripts/backup_eqrf.sh
```

Backups are written to `EQRF_BACKUP_DIR`, defaulting to `backups/`.

Backup names look like:

```text
backups/eqrf-backup-20260527-235900.tar.gz
```

The backup script keeps the latest 20 archives by default. Override with:

```bash
EQRF_BACKUP_KEEP=40 ./scripts/backup_eqrf.sh
```

Optional daily systemd backup examples:

```text
deploy/eqrf-backup.service.example
deploy/eqrf-backup.timer.example
```

Store backups away from the EQRF server as well as locally.

## Tests

Run tests from an activated virtual environment:

```bash
source venv/bin/activate
python -m pytest
```

The current suite covers public routes, Admin auth, content filtering, metadata helpers, audit log behaviour, PDF serving, PDF viewer structure, search restrictions, refresh target handling, and checklist critical-line rendering.

## Development Workflow

Typical workflow:

```bash
git checkout main
git pull origin main
git checkout -b feature/name
python -m pytest
git add .
git commit -m "Meaningful message"
git push origin feature/name
```

Keep commits small and focused. Do not remove operational PDFs, JSON data, audit logs, or checklist content unless explicitly requested.

The repository should not track local virtual environments, bytecode caches, `.DS_Store` files, `.env`, or backup archives. These are covered by `.gitignore`.

## Documentation Discipline

Every functional change must include a README review.

If a change affects setup, deployment, routes, Admin behaviour, PDF handling, data structure, dependencies, or user workflow, `README.md` must be updated in the same commit.

If `README.md` does not need updating, the commit or PR summary should explicitly say:

```text
README reviewed — no update required.
```

Keep this README accurate with the current app behaviour. Do not allow it to describe features that no longer exist.

## Roadmap

Possible future improvements:

- PWA/iPad install mode.
- Role separation beyond the current lightweight admin session.
- Automated backups.
- Richer content health checks.
- Version archive and rollback tools.
- Improved PDF navigation and document outline support.
- Local HTTPS if operationally required.
