"""Utilidades de línea de órdenes.

    python -m app.cli init
    python -m app.cli createuser admin --role admin
    python -m app.cli passwd admin
    python -m app.cli listusers
    python -m app.cli maintenance
    python -m app.cli check
    python -m app.cli recompute
    python -m app.cli secret
"""

from __future__ import annotations

import argparse
import getpass
import secrets
import sys

from sqlalchemy import select

from . import maintenance as maintenance_mod
from . import settings_store
from .config import get_config
from .database import init_db, session_scope
from .files_service import recompute_usage, usage_of
from .models import Role, User
from .security import check_password_policy, hash_password
from .storage import format_size, storage_root


def _ask_password(username: str) -> str:
    config = get_config()
    while True:
        password = getpass.getpass("Contraseña: ")
        if password != getpass.getpass("Repite la contraseña: "):
            print("No coinciden.", file=sys.stderr)
            continue
        errors = check_password_policy(password, username, None, config)
        if errors:
            print(" ".join(errors), file=sys.stderr)
            continue
        return password


def cmd_init(args: argparse.Namespace) -> int:
    storage_root()
    print("Base de datos y almacenamiento preparados.")
    return 0


def cmd_createuser(args: argparse.Namespace) -> int:
    config = get_config()
    with session_scope() as db:
        if db.scalar(select(User).where(User.username == args.username)):
            print(f"El usuario '{args.username}' ya existe.", file=sys.stderr)
            return 1

        password = args.password or _ask_password(args.username)
        errors = check_password_policy(password, args.username, args.email, config)
        if errors:
            print(" ".join(errors), file=sys.stderr)
            return 1

        user = User(
            username=args.username,
            email=(args.email or "").lower() or None,
            password_hash=hash_password(password, config),
            role=Role(args.role),
            is_approved=True,
            is_active=True,
        )
        db.add(user)
        print(f"Usuario '{args.username}' creado con rol {args.role}.")
    return 0


def cmd_passwd(args: argparse.Namespace) -> int:
    config = get_config()
    with session_scope() as db:
        user = db.scalar(select(User).where(User.username == args.username))
        if user is None:
            print(f"No existe '{args.username}'.", file=sys.stderr)
            return 1
        password = args.password or _ask_password(args.username)
        errors = check_password_policy(password, user.username, user.email, config)
        if errors:
            print(" ".join(errors), file=sys.stderr)
            return 1
        user.password_hash = hash_password(password, config)
        print("Contraseña actualizada.")
    return 0


def cmd_listusers(args: argparse.Namespace) -> int:
    config = get_config()
    with session_scope() as db:
        rows = list(db.scalars(select(User).order_by(User.username)))
        if not rows:
            print("No hay usuarios.")
            return 0
        print(f"{'USUARIO':<20} {'ROL':<8} {'ESTADO':<12} {'USO':>12}")
        for user in rows:
            usage = usage_of(user, config)
            state = "activo" if user.is_active else "desactivado"
            if not user.is_approved:
                state = "pendiente"
            used = f"{format_size(usage.used)}"
            print(f"{user.username:<20} {user.role.value:<8} {state:<12} {used:>12}")
    return 0


def cmd_recompute(args: argparse.Namespace) -> int:
    config = get_config()
    with session_scope() as db:
        for user in db.scalars(select(User)):
            total = recompute_usage(db, user, config)
            print(f"{user.username}: {format_size(total)}")
    return 0


def cmd_maintenance(args: argparse.Namespace) -> int:
    result = maintenance_mod.run_once()
    print(", ".join(f"{key}={value}" for key, value in result.items()))
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    """Muestra la configuración efectiva y los avisos de seguridad."""
    with session_scope() as db:
        config = settings_store.apply_overrides(db)

    print(f"Instancia:  {config.app.name} — {config.app.base_url}")
    print(f"Base datos: {config.database.url}")
    print(f"Blobs:      {storage_root(config)}")
    print(f"Red:        modo={config.network.trusted_mode} "
          f"ámbito={config.network.trusted_scope} "
          f"redes={', '.join(config.network.trusted_networks) or 'ninguna'}")
    print(f"Registro:   {'abierto' if config.auth.registration_enabled else 'cerrado'}"
          f"{' con aprobación' if config.auth.require_admin_approval else ''}")
    print(f"Enlaces:    {'activados' if config.sharing.public_links_enabled else 'desactivados'}")

    warnings = config.validate_runtime()
    if warnings:
        print("\nAvisos:")
        for warning in warnings:
            print(f"  ! {warning}")
        return 1
    print("\nSin avisos.")
    return 0


def cmd_secret(args: argparse.Namespace) -> int:
    print(secrets.token_urlsafe(64))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="app.cli", description="Utilidades de Drive")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="crear el esquema y los directorios").set_defaults(func=cmd_init)

    create = sub.add_parser("createuser", help="crear una cuenta")
    create.add_argument("username")
    create.add_argument("--email", default="")
    create.add_argument("--role", default="user", choices=[r.value for r in Role])
    create.add_argument("--password", help="si se omite, se pide por teclado")
    create.set_defaults(func=cmd_createuser)

    passwd = sub.add_parser("passwd", help="cambiar la contraseña de una cuenta")
    passwd.add_argument("username")
    passwd.add_argument("--password")
    passwd.set_defaults(func=cmd_passwd)

    sub.add_parser("listusers", help="listar cuentas").set_defaults(func=cmd_listusers)
    sub.add_parser("recompute", help="recalcular el espacio usado").set_defaults(func=cmd_recompute)
    sub.add_parser("maintenance", help="ejecutar la limpieza ahora").set_defaults(func=cmd_maintenance)
    sub.add_parser("check", help="revisar la configuración").set_defaults(func=cmd_check)
    sub.add_parser("secret", help="generar una clave para security.secret_key").set_defaults(func=cmd_secret)

    args = parser.parse_args(argv)
    # Todas las órdenes salvo la generación de secretos tocan la base de datos,
    # y crear el esquema es idempotente.
    if args.command != "secret":
        init_db()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
