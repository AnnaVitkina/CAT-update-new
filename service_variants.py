"""
Shared SPOT / CY-CY / _BU variant merge and conflict rules.

Kept in a dedicated module so BASE and ETSBAF do not import each other.
"""

from datetime import datetime

from route_utils import (
    PRIORITY_SPOT,
    PRIORITY_STANDARD,
    apply_service_c_aliases,
    is_bu_key_lane,
    is_bu_related_service,
    is_spot_key,
    is_spot_service,
    route_service_c_value,
    update_service_priority,
)


def parse_yyyymmdd(value):
    if not value:
        return None
    try:
        return datetime.strptime(str(value), "%Y-%m-%d").date()
    except ValueError:
        return None


def validity_ranges_intersect(period_a_from, period_a_to, period_b_from, period_b_to):
    if not all([period_a_from, period_a_to, period_b_from, period_b_to]):
        return True
    return not (period_a_to < period_b_from or period_a_from > period_b_to)


def is_bu_related_update(update_record):
    route = update_record.get("route", {})
    service = route_service_c_value(route)
    if is_bu_related_service(service):
        return True
    return is_bu_key_lane(route.get("KEY"))


def is_spot_related_update(update_record):
    route = update_record.get("route", {})
    return is_spot_service(route_service_c_value(route)) or is_spot_key(route.get("KEY"))


def update_validity_period(update_record):
    route = update_record.get("route", {})
    return (
        parse_yyyymmdd(route.get("RATE_EFFECTIVE_DATE__C")),
        parse_yyyymmdd(route.get("RATE_EXPIRATION_DATE__C")),
    )


def _build_variant_merge(group):
    """Merge distinct KEY / SERVICE_C values with ' / ', priority order then KEY."""
    ordered = sorted(
        group,
        key=lambda r: (
            update_service_priority(r.get("route", {})),
            str((r.get("route") or {}).get("KEY") or ""),
        ),
    )
    keys = []
    services = []
    seen_keys = set()
    seen_services = set()
    for rec in ordered:
        route = rec.get("route") or {}
        key = str(route.get("KEY") or "").strip()
        if key and key not in seen_keys:
            seen_keys.add(key)
            keys.append(key)
        svc = str(route_service_c_value(route) or "").strip()
        if svc and svc not in seen_services:
            seen_services.add(svc)
            services.append(svc)
    return {
        "KEY": " / ".join(keys),
        "SERVICE_C": " / ".join(services),
    }


def collect_variant_merges_by_tid(records):
    """
    Build slash-merged KEY/SERVICE_C per Transporeon ID from any update rows
    (all sheets). CY-CY may appear on BASE while CY-CY_BU only on ETSBAF.
    """
    grouped = {}
    for rec in records or []:
        tid = (rec.get("route") or {}).get("Transporeon ID") or ""
        if not tid:
            continue
        grouped.setdefault(tid, []).append(rec)

    merge_by_tid = {}
    for tid, group in grouped.items():
        distinct_keys = {str((r.get("route") or {}).get("KEY") or "") for r in group}
        distinct_keys.discard("")
        distinct_svcs = {
            str(route_service_c_value(r.get("route") or {}) or "") for r in group
        }
        distinct_svcs.discard("")
        if len(distinct_keys) > 1 or len(distinct_svcs) > 1:
            merge_by_tid[tid] = _build_variant_merge(group)
    return merge_by_tid


def filter_service_variant_conflicts(update_records, variant_catalog=None):
    """
    Same Transporeon ID may carry SPOT, standard (e.g. CY-CY), and _BU variants.

    Priority: SPOT > non-_BU > _BU
    - _BU overlapping a higher variant → drop _BU (orange if same validity)
    - SPOT vs CY-CY partial overlap → keep BOTH so CY-CY covers non-overlap
      and SPOT prices win only on the intersecting segment (via later split/apply)
    - SPOT vs CY-CY identical validity → drop CY-CY; SPOT prices; Service stays CY-CY
    - no overlap → keep both

    Slash-merged KEY/SERVICE_C come from variant_catalog when provided.
    """
    grouped = {}
    for rec in update_records:
        tid = rec.get("route", {}).get("Transporeon ID") or ""
        grouped.setdefault(tid, []).append(rec)

    drop_ids = set()
    orange_ids = set()
    merge_by_tid = collect_variant_merges_by_tid(
        variant_catalog if variant_catalog is not None else update_records
    )

    for tid, group in grouped.items():
        if not tid:
            continue

        for rec_low in group:
            p_low = update_service_priority(rec_low.get("route", {}))
            low_from, low_to = update_validity_period(rec_low)
            if not (low_from and low_to):
                continue
            for rec_high in group:
                if rec_low is rec_high:
                    continue
                p_high = update_service_priority(rec_high.get("route", {}))
                if p_low <= p_high:
                    continue
                high_from, high_to = update_validity_period(rec_high)
                if not (high_from and high_to):
                    continue
                if not validity_ranges_intersect(low_from, low_to, high_from, high_to):
                    continue

                # SPOT vs CY-CY: keep CY-CY on partial overlap so validity can split.
                if p_high == PRIORITY_SPOT and p_low == PRIORITY_STANDARD:
                    if low_from == high_from and low_to == high_to:
                        drop_ids.add(id(rec_low))
                        orange_ids.add(id(rec_high))
                    continue

                drop_ids.add(id(rec_low))
                if low_from == high_from and low_to == high_to:
                    orange_ids.add(id(rec_high))

    result = []
    for rec in update_records:
        if id(rec) in drop_ids:
            continue
        route = rec.setdefault("route", {})
        tid = route.get("Transporeon ID") or ""
        merge = merge_by_tid.get(tid)
        if merge:
            route["merged_KEY"] = merge["KEY"]
            route["merged_SERVICE_C"] = merge["SERVICE_C"]
        if id(rec) in orange_ids:
            route["update_highlight"] = "orange"
        result.append(rec)
    return result


def filter_bu_service_conflicts(update_records):
    return filter_service_variant_conflicts(update_records)


def stamp_variant_fields(lane, update_record):
    """Write slash-merged KEY and SERVICE_C / SERVICE__C; Service stays CY-CY for SPOT."""
    route = lane.setdefault("route", {})
    upd_route = update_record.get("route") or {}
    changed = set(route.get("update_changed_fields") or [])

    merged_key = upd_route.get("merged_KEY")
    if merged_key and route.get("KEY") != merged_key:
        route["KEY"] = merged_key
        changed.add("KEY")

    merged_svc = upd_route.get("merged_SERVICE_C")
    if merged_svc:
        prev_svc = route_service_c_value(route)
        if prev_svc != merged_svc:
            changed.add("SERVICE_C")
        apply_service_c_aliases(route, merged_svc)

    # SPOT is CY-CY for Service display only (prices may still come from SPOT).
    merged_svc_text = str(merged_svc or "")
    if (
        is_spot_service(route_service_c_value(upd_route))
        or is_spot_key(upd_route.get("KEY"))
        or "OC_CNTR_SPOT" in merged_svc_text
        or "SPOT" in merged_svc_text
    ):
        if route.get("Service") != "CY-CY":
            route["Service"] = "CY-CY"
            changed.add("Service")

    if upd_route.get("update_highlight") == "orange":
        route["update_highlight"] = "orange"

    if changed:
        route["update_changed_fields"] = sorted(changed)
