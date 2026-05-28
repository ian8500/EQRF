# Changelog

## Unreleased

- Added project README and documentation update discipline.
- Added production WSGI runtime configuration, Gunicorn run script, systemd example, `.env.example`, and health endpoint.
- Hardened production service runtime with systemd hardening, install/health/backup scripts, backup timers, upload limits, and friendly error pages.
- Fixed production-mode defaults and Admin safety warning visibility.
- Fixed PDF viewer orientation handling so landscape metadata controls initial display and Reset View.
- Added `.env` loading, stronger secret/password safety checks, and secret-key setup documentation.

Future changes should update this file when user-facing behaviour, deployment steps, data model, or admin workflows change.
