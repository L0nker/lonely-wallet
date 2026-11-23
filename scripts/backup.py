#!/usr/bin/env python3
"""Create consistent SQLite backups; encryption keys are excluded by default."""
import argparse
import os
import sqlite3
import shutil
import json
import hashlib
import tempfile
from datetime import datetime
from pathlib import Path


def backup_database(source: Path, target: Path) -> None:
    if not source.exists():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(source.expanduser().resolve().as_uri() + '?mode=ro', uri=True, timeout=30)
    dst = sqlite3.connect(target, timeout=30)
    try:
        src.backup(dst, pages=256, sleep=0.05)
        result = dst.execute('PRAGMA integrity_check').fetchone()[0]
        if result != 'ok':
            raise RuntimeError(f'Integridade inválida em {source.name}: {result}')
    finally:
        dst.close()
        src.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--destination', default='/backup', help='Diretório de destino')
    parser.add_argument('--include-keys', action='store_true', help='Inclui chaves de criptografia/sessão; guarde o backup em local criptografado e separado')
    args = parser.parse_args()
    data_dir = Path(os.environ.get('DATA_DIR', '/data'))
    backup_root = Path(args.destination)
    backup_root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    final_dir = backup_root / f'lonely-wallet-{stamp}'
    if final_dir.exists():
        raise FileExistsError(f'O destino do backup já existe: {final_dir}')
    staging = Path(tempfile.mkdtemp(prefix='.lonely-wallet-staging-', dir=backup_root))
    try:
        copied = []
        for name in ('ativos.db', 'carteira.db', 'tokens.db', 'api_state.db'):
            source = data_dir / name
            if source.exists():
                backup_database(source, staging / name)
                copied.append(name)
        quote_cache = data_dir / 'quotes_last_valid.json'
        if quote_cache.exists():
            with quote_cache.open('r', encoding='utf-8') as handle:
                payload = json.load(handle)
            if not isinstance(payload, dict):
                raise RuntimeError('Cache de cotações inválido: esperado um objeto JSON.')
            target = staging / 'quotes_last_valid.json'
            shutil.copy2(quote_cache, target)
            os.chmod(target, 0o600)
            copied.append('quotes_last_valid.json')
        if args.include_keys:
            secret_dir = staging / 'secrets'
            secret_dir.mkdir(mode=0o700)
            for name in ('.api-credentials.key', '.lonely-wallet-session.key'):
                source = data_dir / name
                if source.exists():
                    target = secret_dir / name
                    target.write_bytes(source.read_bytes())
                    os.chmod(target, 0o600)
                    copied.append(f'secrets/{name}')
        if not copied:
            raise RuntimeError('Nenhum arquivo de dados encontrado para backup.')
        manifest = {
            'application': 'Lonely Wallet',
            'created_at': datetime.now().astimezone().isoformat(),
            'files': copied,
            'includes_encryption_keys': bool(args.include_keys),
        }
        manifest_path = staging / 'manifest.json'
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        os.chmod(manifest_path, 0o600)
        # Atomic publication: the final folder appears only after all checks pass.
        os.replace(staging, final_dir)
        print(f'Backup concluído: {final_dir}')
        if not args.include_keys:
            print('Atenção: chaves não incluídas. Credenciais criptografadas exigem a chave original para restauração.')
        return 0
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


if __name__ == '__main__':
    raise SystemExit(main())
