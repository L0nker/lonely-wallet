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


def main():
    if os.geteuid() == 0:
        DATA.mkdir(parents=True, exist_ok=True)
        uid = pwd.getpwnam(USER).pw_uid
        gid = grp.getgrnam(GROUP).gr_gid
        # Existing installations are migrated once; subsequent starts avoid
        # recursively scanning a potentially large portfolio directory.
        if not MARKER.exists():
            for root, dirs, files in os.walk(DATA):
                try:
                    os.chown(root, uid, gid)
                except PermissionError:
                    pass
                for name in dirs + files:
                    path = os.path.join(root, name)
                    try:
                        os.chown(path, uid, gid, follow_symlinks=False)
                    except (PermissionError, FileNotFoundError):
                        pass
            MARKER.touch(mode=0o600, exist_ok=True)
            os.chown(MARKER, uid, gid)
        os.setgroups([])
        os.setgid(gid)
        os.setuid(uid)
    os.execvp(sys.argv[1], sys.argv[1:])


if __name__ == '__main__':
    main()
