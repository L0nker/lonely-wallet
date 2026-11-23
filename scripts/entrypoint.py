#!/usr/local/bin/python
"""Prepare the persistent data volume once, then drop privileges."""
import os
import pwd
import grp
import sys
import tempfile
from pathlib import Path

DATA = Path(os.environ.get('DATA_DIR', '/data'))
USER = 'app'
GROUP = 'app'
MARKER = DATA / '.lonely-wallet-permissions-v1'


def migrate_permissions(data_dir: Path, uid: int, gid: int) -> None:
    """Apply ownership to the persistent volume or fail without marking success."""
    root = Path(data_dir)
    root.mkdir(parents=True, exist_ok=True)
    for current_root, dirs, files in os.walk(root, followlinks=False):
        paths = [Path(current_root)] + [Path(current_root) / name for name in dirs + files]
        for path in paths:
            try:
                os.chown(path, uid, gid, follow_symlinks=False)
            except FileNotFoundError:
                # A concurrently removed transient file does not need ownership.
                continue
            except OSError as exc:
                raise RuntimeError(f'Não foi possível ajustar permissões de {path.name}: {exc.__class__.__name__}') from exc


def _write_marker(uid: int, gid: int) -> None:
    """Write marker atomically, and only after it already has correct ownership."""
    DATA.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=MARKER.name + '.', suffix='.tmp', dir=str(DATA))
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            handle.write('permissions-migrated-v1\n')
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.chown(temporary, uid, gid, follow_symlinks=False)
        os.replace(temporary, MARKER)
        try:
            directory_fd = os.open(DATA, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            pass
    except Exception:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def main():
    if len(sys.argv) < 2:
        raise SystemExit('Comando do container não informado.')
    if os.geteuid() == 0:
        DATA.mkdir(parents=True, exist_ok=True)
        user_info = pwd.getpwnam(USER)
        uid = user_info.pw_uid
        gid = grp.getgrnam(GROUP).gr_gid
        # Gunicorn 25+ creates a control socket under XDG_RUNTIME_DIR (or
        # $HOME/.gunicorn). The entrypoint drops privileges, so both paths
        # must point to directories writable by the unprivileged app user.
        app_home = Path(user_info.pw_dir)
        app_home.mkdir(parents=True, exist_ok=True)
        os.chown(app_home, uid, gid)
        os.chmod(app_home, 0o700)
        runtime_dir = Path(os.environ.get('XDG_RUNTIME_DIR', '/tmp/lonely-wallet-runtime'))
        runtime_dir.mkdir(parents=True, exist_ok=True)
        os.chown(runtime_dir, uid, gid)
        os.chmod(runtime_dir, 0o700)
        os.environ['HOME'] = str(app_home)
        os.environ['XDG_RUNTIME_DIR'] = str(runtime_dir)
        if not MARKER.exists():
            migrate_permissions(DATA, uid, gid)
            _write_marker(uid, gid)
        os.setgroups([])
        os.setgid(gid)
        os.setuid(uid)
    os.execvp(sys.argv[1], sys.argv[1:])


if __name__ == '__main__':
    main()
