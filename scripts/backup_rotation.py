"""Small, stdlib-only rotation helper for pre-delete backup folders."""
from pathlib import Path


def rotate_predelete_backups(backup_root, keep=10, logger=None):
    """Keep the newest verified pre-delete backup directories.

    Only real directories matching ``pre-exclusao-*`` are considered. A
    filesystem race or an individual removal failure does not invalidate the
    newly-created backup and does not prevent other old backups from rotating.
    """
    root = Path(backup_root)
    keep = max(1, int(keep))
    candidates = []
    try:
        paths = list(root.glob('pre-exclusao-*'))
    except OSError as exc:
        if logger:
            logger.warning('Não foi possível listar backups preventivos antigos: %s', type(exc).__name__)
        return []
    for path in paths:
        try:
            if path.is_dir() and not path.is_symlink():
                candidates.append((path.stat().st_mtime_ns, path.name, path))
        except OSError as exc:
            if logger:
                logger.warning('Não foi possível inspecionar backup preventivo %s: %s', path.name, type(exc).__name__)
    candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
    removed = []
    import shutil
    for _, _, path in candidates[keep:]:
        try:
            shutil.rmtree(path)
            removed.append(path.name)
        except OSError as exc:
            if logger:
                logger.warning('Não foi possível remover backup preventivo antigo %s: %s', path.name, type(exc).__name__)
    return removed
