# Cleanup Report

## Summary

This cleanup pass reviewed the Flask routes, active templates, static assets, dependencies, tests, deployment files, scripts, docs, and local generated artifacts. The patch keeps operational JSON/PDF/JPG content intact and removes only files that were clearly not part of the active application surface.

## Files Removed From Active Tracking

- Removed tracked local environment/build artifacts from Git tracking:
  - `.DS_Store`
  - `.env.save`
  - `__pycache__/`
  - `tests/__pycache__/`
  - `venv/`
  - `static/.DS_Store`
  - `static/images/.DS_Store`
- Removed unreferenced legacy templates:
  - `templates/admin_nav_snippet.html`
  - `templates/checklist_admin.html`
  - `templates/edit_checklist.html`
  - `templates/index.html`
  - `templates/new_checklist.html`
  - `templates/pdf_viewer.html`
  - `templates/register_pdf.html`
- Removed unused static image/demo files:
  - `static/images/Glasgow.jpg`
  - `static/images/PDF Documents.html`

## Code Removed Or Left In Place

- No live Flask route was removed.
- No operational data helper was removed.
- Legacy JPG helper code remains because Admin health checks still report legacy JPG status and orphan JPGs.
- The old General Reference title/filename search route was already absent; current search remains the in-document `/viewer-search` route for General Reference PDFs only.

## Dependencies

- `requirements.txt` was reviewed.
- No dependency was removed in this pass.
- `pdf2image` and `Pillow` are not present.
- `pypdf` remains required for page counts, orientation detection, and PDF text search cache.
- `python-dotenv` remains required for `.env` loading.
- `gunicorn` remains required for the production WSGI runtime.

## Files Intentionally Kept

- `data/`, including `extracts.json`, `checklists.json`, and `audit_log.json`.
- `pdfs/`, including all uploaded/source PDFs.
- `static/pdfjs/`, used by the active direct PDF viewer.
- `static/jpgs/`, legacy generated page images. The public viewer no longer depends on these, but Admin health still reports legacy JPG state and the files may be useful for audit/recovery.
- `config-openmaptiles.json` and `process-openmaptiles.lua`. They are not referenced by the Flask app, but they may be user-held map tooling assets, so they were left untouched.
- `deploy/`, `scripts/`, `docs/`, `tests/`, `README.md`, `AGENTS.md`, and `CHANGELOG.md`.

## Remaining Technical Debt

- `app.py` is still large and contains route handlers, data helpers, health checks, and admin workflows in one module. A future maintainability pass could split this into small modules or blueprints.
- Legacy JPG metadata and health reporting are retained for backwards compatibility. A future migration could remove JPG reporting after confirming `static/jpgs/` is no longer operationally useful.
- The checked-in `static/jpgs/` folder is large. It should be reviewed manually before any removal because it is historical operational content.
- CSS and JavaScript are still monolithic. They are active and tested, but could be split by area once the UI stabilises.

## Manual Cleanup Recommendations

- Recreate `venv/` locally with `python3 -m venv venv` and `pip install -r requirements.txt` if needed.
- Consider whether `config-openmaptiles.json` and `process-openmaptiles.lua` belong in EQRF or another repository.
- Review `static/jpgs/` after a period of direct-PDF viewer operation; remove manually only if all legacy recovery/audit needs are satisfied.
