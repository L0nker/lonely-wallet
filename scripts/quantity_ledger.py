"""Chronological position-quantity validation independent of financial returns."""


def quantity_ledger_breaks_position(snapshots, operations, proposed_operation=None, exclude_id=None):
    """Return True when any sale makes the position negative in its time segment.

    Imported balance snapshots are authoritative opening balances for their date.
    Every later snapshot resets the running quantity, but operations after that
    snapshot continue to be validated. This avoids ignoring sales after a second
    or later imported snapshot.
    """
    proposed_operation = proposed_operation or None
    proposed_raw = ''
    if proposed_operation:
        proposed_raw = str(proposed_operation.get('data_execucao') or proposed_operation.get('data') or '')
        if not proposed_raw:
            return True

    excluded_raw = ''
    if exclude_id is not None:
        for operation in operations:
            if int(operation.get('id') or 0) == int(exclude_id):
                excluded_raw = str(operation.get('data_execucao') or operation.get('data') or '')
                break

    affected_dates = [raw[:10] for raw in (proposed_raw, excluded_raw) if raw]
    anchor_day = min(affected_dates) if affected_dates else '9999-12-31'

    normalized_snapshots = []
    for snapshot in snapshots:
        day = str(snapshot.get('data_base') or '')[:10]
        if not day:
            continue
        normalized_snapshots.append((day, float(snapshot.get('quantidade') or 0)))
    normalized_snapshots.sort(key=lambda item: item[0])

    base_index = -1
    for index, (day, _) in enumerate(normalized_snapshots):
        if day <= anchor_day:
            base_index = index
        else:
            break

    if base_index >= 0:
        active_snapshot_day, quantity = normalized_snapshots[base_index]
        future_snapshots = normalized_snapshots[base_index + 1:]
    else:
        active_snapshot_day, quantity = '', 0.0
        future_snapshots = normalized_snapshots

    events = []
    for operation in operations:
        operation_id = int(operation.get('id') or 0)
        if exclude_id is not None and operation_id == int(exclude_id):
            continue
        raw = str(operation.get('data_execucao') or operation.get('data') or '')
        if not raw:
            continue
        if active_snapshot_day and raw[:10] <= active_snapshot_day:
            continue
        events.append((raw, 0, operation_id, str(operation.get('lado') or ''),
                       float(operation.get('quantidade') or 0)))

    if proposed_operation:
        proposed_time = proposed_raw if 'T' in proposed_raw else proposed_raw + 'T10:00:00'
        events.append((proposed_time, 1, 2**63 - 1,
                       str(proposed_operation.get('lado') or 'compra'),
                       float(proposed_operation.get('quantidade') or 0)))
    events.sort(key=lambda event: (event[0], event[1], event[2]))

    snapshot_index = 0
    for raw, _, _, side, amount in events:
        event_day = raw[:10]
        # Treat a daily imported snapshot as the end-of-day position. Operations
        # on that date are already represented by its quantity and are skipped.
        while snapshot_index < len(future_snapshots) and future_snapshots[snapshot_index][0] <= event_day:
            active_snapshot_day, quantity = future_snapshots[snapshot_index]
            snapshot_index += 1
        if active_snapshot_day and event_day <= active_snapshot_day:
            continue
        if side == 'compra':
            quantity += amount
        elif side == 'venda':
            quantity -= amount
            if quantity < -1e-8:
                return True
    return False
