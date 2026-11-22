"""Atomic SQLite migration helpers for the persistent Lonely Wallet volume."""
import os
import sqlite3
import tempfile
from pathlib import Path


def migrate_legacy_database(legacy_path, destination_path):
    """Copy a legacy SQLite database to a validated temporary file, then publish it.

    The destination is never created as a partial copy. If backup or integrity
    validation fails, the temporary file is removed and the caller can safely
    retry migration on the next application startup.
    """
    legacy = Path(legacy_path)
    destination = Path(destination_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(
        prefix='.lonely-wallet-migration-', suffix='.sqlite3', dir=str(destination.parent)
    )
    os.close(fd)
    src = None
    dst = None
    try:
        legacy_uri = legacy.resolve().as_uri() + '?mode=ro'
        src = sqlite3.connect(legacy_uri, uri=True, timeout=30)
        dst = sqlite3.connect(temp_path, timeout=30)
        src.execute('PRAGMA busy_timeout=30000')
        dst.execute('PRAGMA busy_timeout=30000')
        src.backup(dst, pages=256, sleep=0.05)
        result = dst.execute('PRAGMA integrity_check').fetchone()
        if not result or result[0] != 'ok':
            raise RuntimeError('Falha de integridade na migração do SQLite legado.')
        dst.commit()
        dst.close()
        dst = None
        src.close()
        src = None
        with open(temp_path, 'rb') as handle:
            os.fsync(handle.fileno())
        os.replace(temp_path, destination)
        try:
            dir_fd = os.open(str(destination.parent), os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            # File publication is already atomic; some filesystems do not fsync dirs.
            pass
    except Exception:
        if dst is not None:
            dst.close()
        if src is not None:
            src.close()
        try:
            os.remove(temp_path)
        except FileNotFoundError:
            pass
        raise
