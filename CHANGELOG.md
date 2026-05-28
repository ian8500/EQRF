# Changelog

## Unreleased

- Added project README and documentation update discipline.
- Added production WSGI runtime configuration, Gunicorn run script, systemd example, `.env.example`, and health endpoint.
- Hardened production service runtime with systemd hardening, install/health/backup scripts, backup timers, upload limits, and friendly error pages.
- Fixed production-mode defaults and Admin safety warning visibility.
- Fixed PDF viewer orientation handling so landscape metadata controls initial display and Reset View.
- Added `.env` loading, stronger secret/password safety checks, and secret-key setup documentation.
- Cleaned project structure by untracking local build artifacts, removing obsolete templates/static demo files, and adding `CLEANUP_REPORT.md`.
- Improved responsive layout for desktop, iPad, and tablet use with compact headers/toolbars and fluid content grids.
- Hardened Admin authentication with CSRF protection, password-hash support, failed-login rate limiting, audit logging, and secure session cookie defaults.

Future changes should update this file when user-facing behaviour, deployment steps, data model, or admin workflows change.
