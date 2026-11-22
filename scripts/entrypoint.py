#!/usr/local/bin/python
"""Prepare the persistent data volume once, then drop privileges."""
import os
import pwd
import grp
import sys
from pathlib import Path

DATA = Path(os.environ.get('DATA_DIR', '/data'))
USER = 'app'
GROUP = 'app'
MARKER = DATA / '.lonely-wallet-permissions-v1'


def migrate_permissions(data_dir=DATA, uid=None, gid=None, marker=None):
    """Recursively assign volume ownership; any failure aborts startup."""
    data_dir = Path(data_dir)
    marker = Path(marker) if marker is not None else data_dir / MARKER.name
    if uid is None:
        uid = pwd.getpwnam(USER).pw_uid
    if gid is None:
        gid = grp.getgrnam(GROUP).gr_gid
    data_dir.mkdir(parents=True, exist_ok=True)
    if Path(marker).exists():
        return
    try:
        for root, dirs, files in os.walk(data_dir, followlinks=False):
            os.chown(root, uid, gid)
            for name in dirs + files:
                path = os.path.join(root, name)
                os.chown(path, uid, gid, follow_symlinks=False)
    except OSError as exc:
        raise RuntimeError(f'Falha ao migrar permissões do volume persistente: {exc}') from exc
    _write_marker(uid, gid, marker=marker)


def _write_marker(uid, gid, marker=None):
    """Atomically publish the permissions-migrated marker after ownership succeeds."""
    marker = Path(marker or MARKER)
    marker.parent.mkdir(parents=True, exist_ok=True)
    temp_marker = marker.with_name(marker.name + '.tmp')
    try:
        fd = os.open(str(temp_marker), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, b'ok\n')
            os.fsync(fd)
        finally:
            os.close(fd)
        os.chown(temp_marker, uid, gid)
        os.replace(temp_marker, marker)
    except Exception:
        try:
            temp_marker.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def main():
    if os.geteuid() == 0:
        DATA.mkdir(parents=True, exist_ok=True)
        uid = pwd.getpwnam(USER).pw_uid
        gid = grp.getgrnam(GROUP).gr_gid
        migrate_permissions(DATA, uid, gid, MARKER)
        os.setgroups([])
        os.setgid(gid)
        os.setuid(uid)
    os.execvp(sys.argv[1], sys.argv[1:])


if __name__ == '__main__':
    main()
