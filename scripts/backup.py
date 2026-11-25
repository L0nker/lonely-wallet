#!/usr/bin/env python3
"""Create consistent, private backups of every Lonely Wallet user and portfolio database."""
import argparse
import json
import os
import shutil
import sqlite3
import tempfile
from datetime import datetime
from pathlib import Path


EXCLUDED_DIRS = {"backups", "cache", "__pycache__", ".git"}
KEY_FILES = (".api-credentials.key", ".lonely-wallet-session.key")


def backup_database(source: Path, target: Path) -> None:
    """Copy one SQLite database through SQLite's online backup API and verify it."""
    source = Path(source)
    target = Path(target)
    if not source.is_file() or source.is_symlink():
        return
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    src = sqlite3.connect(source.expanduser().resolve().as_uri() + "?mode=ro", uri=True, timeout=30)
    dst = sqlite3.connect(target, timeout=30)
    try:
        src.backup(dst, pages=256, sleep=0.05)
        result = dst.execute("PRAGMA integrity_check").fetchone()[0]
        if result != "ok":
            raise RuntimeError(f"Integridade inválida em {source.name}: {result}")
    finally:
        dst.close()
        src.close()
    os.chmod(target, 0o600)


def _database_files(data_dir: Path):
    """Find root and per-user/per-wallet SQLite files without traversing backups."""
    found = []
    for root, directories, files in os.walk(data_dir, topdown=True, followlinks=False):
        current = Path(root)
        directories[:] = [
            name for name in directories
            if name not in EXCLUDED_DIRS and not (current / name).is_symlink()
        ]
        for name in files:
            path = current / name
            if path.suffix.lower() == ".db" and path.is_file() and not path.is_symlink():
                found.append(path)
    return sorted(found, key=lambda path: path.relative_to(data_dir).as_posix())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", default="/backup", help="Diretório de destino")
    parser.add_argument(
        "--include-keys", action="store_true",
        help="Inclui chaves de criptografia/sessão; guarde o backup em local criptografado e separado",
    )
    args = parser.parse_args()

    data_dir = Path(os.environ.get("DATA_DIR", "/data")).expanduser().resolve()
    if not data_dir.is_dir():
        raise FileNotFoundError(f"Diretório de dados não encontrado: {data_dir}")

    backup_root = Path(args.destination).expanduser().resolve()
    backup_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    final_dir = backup_root / f"lonely-wallet-{stamp}"
    if final_dir.exists():
        raise FileExistsError(f"O destino do backup já existe: {final_dir}")

    staging = Path(tempfile.mkdtemp(prefix=".lonely-wallet-staging-", dir=backup_root))
    os.chmod(staging, 0o700)
    try:
        copied = []
        for source in _database_files(data_dir):
            relative = source.relative_to(data_dir)
            target = staging / relative
            backup_database(source, target)
            copied.append(relative.as_posix())

        for cache_name in ("quotes_last_valid.json", "sgs_cache.json"):
            source = data_dir / cache_name
            if source.is_file() and not source.is_symlink():
                with source.open("r", encoding="utf-8") as handle:
                    cache_payload = json.load(handle)
                if not isinstance(cache_payload, dict):
                    raise RuntimeError(f"Cache inválido: esperado um objeto JSON em {cache_name}.")
                target = staging / cache_name
                shutil.copy2(source, target)
                os.chmod(target, 0o600)
                copied.append(cache_name)

        if args.include_keys:
            secret_dir = staging / "secrets"
            secret_dir.mkdir(mode=0o700)
            for name in KEY_FILES:
                source = data_dir / name
                if source.is_file() and not source.is_symlink():
                    target = secret_dir / name
                    shutil.copy2(source, target)
                    os.chmod(target, 0o600)
                    copied.append(f"secrets/{name}")

        if not copied:
            raise RuntimeError("Nenhum arquivo de dados encontrado para backup.")

        manifest = {
            "application": "Lonely Wallet",
            "created_at": datetime.now().astimezone().isoformat(),
            "files": copied,
            "includes_all_sqlite_databases": True,
            "includes_encryption_keys": bool(args.include_keys),
        }
        manifest_path = staging / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.chmod(manifest_path, 0o600)
        # Atomic publication: the final folder appears only after all database checks pass.
        os.replace(staging, final_dir)
        print(f"Backup concluído: {final_dir}")
        if not args.include_keys:
            print("Atenção: chaves não incluídas. Credenciais criptografadas exigem a chave original para restauração.")
        return 0
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


if __name__ == "__main__":
    raise SystemExit(main())
