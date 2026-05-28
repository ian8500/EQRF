#!/usr/bin/env python3
"""Print a secure random Flask secret key for EQRF."""

import secrets


def main() -> None:
    print(secrets.token_hex(32))


if __name__ == '__main__':
    main()
