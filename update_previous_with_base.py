import json
import sys
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path

from route_utils import (
    apply_service_c_aliases,
    is_bu_key_lane,
    is_bu_related_service,
    is_rail_service,
    is_spot_key,
    is_spot_service,
    map_update_route_to_rate_card_fields,
    route_service_c_value,
    should_skip_route_update,
    trim_service,
    update_service_priority,
)
from cost_utils import merge_metrics, migrate_cost_names_in_records, normalize_container_code, normalize_cost_title


from cat_paths import PROCESSING_DIR

def to_ddmmyyyy(value):
    if not value:
        return None
    try:
        parsed = datetime.strptime(str(value), "%Y-%m-%d")
        return parsed.strftime("%d.%m.%Y")
    except ValueError:
        return str(value)


def parse_ddmmyyyy(value):
    if not value:
        return None
    try:
        return datetime.strptime(str(value), "%d.%m.%Y").date()
    except ValueError:
        return None


def parse_yyyymmdd(value):
    if not value:
        return None
    try:
        return datetime.strptime(str(value), "%Y-%m-%d").date()
    except ValueError:
        return None


def format_ddmmyyyy(date_value):
    return date_value.strftime("%d.%m.%Y")


def parse_validity_range(value):
    if not value:
        return (None, None)
    raw = str(value).strip()
    if "-" not in raw:
        return (None, None)
    start_raw, end_raw = raw.split("-", 1)
    return parse_ddmmyyyy(start_raw.strip()), parse_ddmmyyyy(end_raw.strip())


def validity_ranges_intersect(period_a_from, period_a_to, period_b_from, period_b_to):
    if not all([period_a_from, period_a_to, period_b_from, period_b_to]):
        return True
    return not (period_a_to < period_b_from or period_a_from > period_b_to)


def clamp_period_to_rate_card(period_from, period_to, card_from, card_to):
    if not (period_from and period_to):
        return (None, None)
    start = period_from
    end = period_to
    if card_from:
        start = max(start, card_from)
    if card_to:
        end = min(end, card_to)
    if start > end:
        return (None, None)
    return (start, end)


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


def get_normalized_base_rate_signature(update_record):
    for rate in update_record.get("rates", []):
        if str(rate.get("charge_code")).lower() != "base":
            continue
        container = map_container_type(rate.get("container_type"))
        if container != "22G0":
            continue
        return (
            rate.get("currency"),
            rate.get("min"),
            rate.get("rate"),
        )
    return None


def filter_container_alias_conflicts(update_records):
    grouped = {}
    for rec in update_records:
        tid = rec.get("route", {}).get("Transporeon ID") or ""
        grouped.setdefault(tid, []).append(rec)

    drop_ids = set()
    for group in grouped.values():
        if len(group) < 2:
            continue
        for idx, rec_a in enumerate(group):
            base_a = get_normalized_base_rate_signature(rec_a)
            if base_a is None:
                continue
            from_a, to_a = update_validity_period(rec_a)
            if not (from_a and to_a):
                continue
            for rec_b in group[idx + 1 :]:
                base_b = get_normalized_base_rate_signature(rec_b)
                if base_b is None or base_a == base_b:
                    continue
                from_b, to_b = update_validity_period(rec_b)
                if not (from_b and to_b):
                    continue
                if not validity_ranges_intersect(from_a, to_a, from_b, to_b):
                    continue
                for rec in (rec_a, rec_b):
                    if is_bu_key_lane(rec.get("route", {}).get("KEY")):
                        drop_ids.add(id(rec))

    return [rec for rec in update_records if id(rec) not in drop_ids]


def _build_variant_merge(group):
    """Merge distinct KEY / SERVICE_C values with '/', priority order then KEY."""
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

    Priority on conflict: SPOT > non-_BU > _BU
    - identical validity → drop lower priority; mark survivor for orange highlight
    - overlapping validity → drop lower priority (higher wins)
    - no overlap → keep both

    Slash-merged KEY/SERVICE_C are built from variant_catalog when provided
    (typically all sheets in the rate-update file); otherwise from update_records.
    Conflict dropping still applies only within update_records.
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


# Backward-compatible alias
def filter_bu_service_conflicts(update_records):
    return filter_service_variant_conflicts(update_records)


def stamp_variant_fields(lane, update_record):
    """Write slash-merged KEY and SERVICE_C / SERVICE__C (not Service) onto a lane."""
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
        # Only KEY + SERVICE_C / SERVICE__C are slash-merged; leave Service alone.
        apply_service_c_aliases(route, merged_svc)

    if upd_route.get("update_highlight") == "orange":
        route["update_highlight"] = "orange"

    if changed:
        route["update_changed_fields"] = sorted(changed)


def stamp_location_fields(lane, update_record):
    """
    For rail CY-RR / RR-CY lanes, map LOCATION_NAME onto Port XOR Postal:
    combined values (e.g. ZADUR_ZAJNB) → Postal only; plain codes → Port only.
    """
    upd_route = update_record.get("route") or {}
    lane_route = lane.setdefault("route", {})
    service = (
        route_service_c_value(upd_route)
        or route_service_c_value(lane_route)
        or lane_route.get("Service")
        or upd_route.get("KEY")
        or lane_route.get("KEY")
    )
    if not is_rail_service(service):
        return

    fields = map_update_route_to_rate_card_fields(upd_route)
    changed = set(lane_route.get("update_changed_fields") or [])
    for key, value in fields.items():
        # Normalize missing port to empty string to match rate-card layout.
        if value is None:
            value = ""
        if lane_route.get(key) != value:
            lane_route[key] = value
            changed.add(key)
    if changed:
        lane_route["update_changed_fields"] = sorted(changed)


def map_container_type(update_container_type):
    if not update_container_type:
        return None
    raw = str(update_container_type).replace("CNTR", "")
    return normalize_container_code(raw)


def map_charge_name(charge_code):
    mapping = {
        "base": "Base Rate",
        "baf": "BAF Fee",
        "ets": "EU ETS Fee",
        "dthc": "Destination Terminal Handling Fee",
        "othc": "Origin Terminal Handling Fee",
        "wrf": "War Risk Fee",
        "dcfs": "Destination CFS Fee",
    }
    return mapping.get(str(charge_code).lower(), str(charge_code).upper())


def should_ignore_min(charge_code, container_type):
    # Ignore MIN only for containerized Base Rate updates (e.g. 22G0, 42G0).
    if str(charge_code).lower() != "base":
        return False
    if container_type in (None, "", "None"):
        return False
    if str(container_type) == "BASE_STAT_FRK":
        return False
    return True


def find_cost_target(lane_costs, index, candidate_name, container):
    candidate_name = normalize_cost_title(candidate_name)
    key = (candidate_name, str(container))
    if key in index:
        return lane_costs[index[key]], key

    none_key = (candidate_name, "None")
    if none_key in index:
        return lane_costs[index[none_key]], none_key

    name_matches = [
        i
        for i, c in enumerate(lane_costs)
        if normalize_cost_title(c.get("cost_name")) == candidate_name
    ]
    if len(name_matches) == 1:
        matched_key = (candidate_name, str(lane_costs[name_matches[0]].get("container_type")))
        return lane_costs[name_matches[0]], matched_key

    return None, key


def map_rate_by(container_type):
    mapping = {
        "22G0": "Container/20FT",
        "25G0": "Container/20HC",
        "42G0": "Container/40FT",
        "45G0": "Container/40HC",
        "BASE_STAT_FRK": "Weight/chargeable kg",
    }
    return mapping.get(container_type, "Weight/chargeable kg")


def build_cost_name(charge_code, container_type):
    base_name = map_charge_name(charge_code)
    if container_type:
        return normalize_cost_title(f"{base_name} ({container_type})")
    return normalize_cost_title(base_name)


def make_base_cost_template(cost):
    template = deepcopy(cost)
    template["currency"] = None
    template["flat_min"] = None
    template["p_unit"] = None
    if cost.get("update_note") == "(new)":
        template["update_note"] = "(new)"
    else:
        template.pop("update_note", None)
    return template


def make_new_cost(charge_code, container_type, template_cost=None):
    cost = {
        "cost_name": build_cost_name(charge_code, container_type),
        "container_type": container_type,
        "apply_if": "Applies if invoiced by Carrier",
        "validity_period": None,
        "cost_to_prolong": None,
        "rate_by": "",
        "rule": "Regular rule",
        "currency": None,
        "flat_min": None,
        "p_unit": None,
        "metrics": ["currency", "p_unit"],
    }
    if template_cost and template_cost.get("metrics"):
        cost["metrics"] = list(template_cost["metrics"])
    return cost


def get_lane_template(previous_records):
    if not previous_records:
        raise ValueError("Previous rate card has no records to use as template.")
    return previous_records[0]


def next_lane_number(previous_records):
    max_lane = 0
    for rec in previous_records:
        lane = rec.get("route", {}).get("Lane #")
        try:
            max_lane = max(max_lane, int(str(lane)))
        except (ValueError, TypeError):
            continue
    return str(max_lane + 1)


def next_row_number(previous_records):
    max_row = 0
    for rec in previous_records:
        try:
            max_row = max(max_row, int(rec.get("row_number", 0)))
        except (ValueError, TypeError):
            continue
    return max_row + 1


def update_costs(base_rates_template, lane_record, update_record):
    lane_costs = [make_base_cost_template(c) for c in base_rates_template]

    index = {}
    for idx, c in enumerate(lane_costs):
        key = (str(c.get("cost_name")), str(c.get("container_type")))
        index[key] = idx

    for upd in update_record.get("rates", []):
        charge_code = upd.get("charge_code")
        container = map_container_type(upd.get("container_type"))
        candidate_name = build_cost_name(charge_code, container)
        lookup_key = (candidate_name, str(container))

        target, matched_key = find_cost_target(lane_costs, index, candidate_name, container)
        if target is not None:
            is_new_cost = False
            lookup_key = matched_key
        else:
            template_cost = None
            for c in base_rates_template:
                if c.get("cost_name") == candidate_name:
                    template_cost = c
                    break
            target = make_new_cost(charge_code, container, template_cost)
            target["update_note"] = "(new)"
            lane_costs.append(target)
            index[lookup_key] = len(lane_costs) - 1
            is_new_cost = True

        target["currency"] = upd.get("currency")
        target["flat_min"] = None if should_ignore_min(charge_code, container) else upd.get("min")
        target["p_unit"] = upd.get("rate")
        if not is_new_cost:
            target["update_note"] = "(updated)"

    lane_record["rates"] = lane_costs


def apply_update_only_to_costs(lane_record, update_record, base_rates_template):
    lane_costs = lane_record.get("rates", [])
    index = {}
    for idx, c in enumerate(lane_costs):
        key = (str(c.get("cost_name")), str(c.get("container_type")))
        index[key] = idx

    template_index = {}
    for idx, c in enumerate(base_rates_template):
        key = (str(c.get("cost_name")), str(c.get("container_type")))
        template_index[key] = idx

    for upd in update_record.get("rates", []):
        charge_code = upd.get("charge_code")
        container = map_container_type(upd.get("container_type"))
        candidate_name = build_cost_name(charge_code, container)

        target, _matched_key = find_cost_target(lane_costs, index, candidate_name, container)
        if target is None:
            template_cost = next((c for c in base_rates_template if c.get("cost_name") == candidate_name), None)
            target = make_new_cost(charge_code, container, template_cost)
            target["update_note"] = "(new)"
            insert_at = template_index.get((candidate_name, str(container)))
            if insert_at is None:
                insert_at = template_index.get((candidate_name, "None"), len(lane_costs))
            lane_costs.insert(min(insert_at, len(lane_costs)), target)
            index = {
                (str(c.get("cost_name")), str(c.get("container_type"))): i
                for i, c in enumerate(lane_costs)
            }
            target, _ = find_cost_target(lane_costs, index, candidate_name, container)

        target["currency"] = upd.get("currency")
        target["flat_min"] = None if should_ignore_min(charge_code, container) else upd.get("min")
        target["p_unit"] = upd.get("rate")
        if target.get("update_note") != "(new)":
            target["update_note"] = "(updated)"

    lane_record["rates"] = lane_costs


def build_new_lane(update_record, previous_records, base_rates_template, card_from=None, card_to=None):
    route = update_record.get("route", {})
    upd_from = parse_yyyymmdd(route.get("RATE_EFFECTIVE_DATE__C"))
    upd_to = parse_yyyymmdd(route.get("RATE_EXPIRATION_DATE__C"))
    eff_from, eff_to = clamp_period_to_rate_card(upd_from, upd_to, card_from, card_to)
    if not (eff_from and eff_to):
        return None

    location_fields = map_update_route_to_rate_card_fields(route)
    # SERVICE_C / KEY may be slash-merged; Service stays a single trimmed value.
    service_c = route.get("merged_SERVICE_C") or route_service_c_value(route)
    key_value = route.get("merged_KEY") or route.get("KEY")
    service_display = trim_service(route_service_c_value(route))
    lane_route = {
        "Lane #": next_lane_number(previous_records),
        "Transporeon ID": route.get("Transporeon ID"),
        "KEY": key_value,
        "Carrier": route.get("CARRIER"),
        "SERVICE": "not PRECARRIAGE/ONCARRIAGE",
        "Service": service_display,
        "Valid from": format_ddmmyyyy(eff_from),
        "Valid to": format_ddmmyyyy(eff_to),
        "Origin Port": location_fields["Origin Port"],
        "Origin Postal Code": location_fields["Origin Postal Code"],
        "ORIGIN_COUNTRY__C": route.get("ORIGIN_COUNTRY__C"),
        "Destination Port": location_fields["Destination Port"],
        "Destination Postal Code": location_fields["Destination Postal Code"],
        "DESTINATION_COUNTRY__C": route.get("DESTINATION_COUNTRY__C"),
        "update_note": "(new)",
        "update_source": "BASE",
    }
    apply_service_c_aliases(lane_route, service_c)
    if route.get("update_highlight") == "orange":
        lane_route["update_highlight"] = "orange"

    # Preserve previous rate-card column order for SERVICE_C / SERVICE__C.
    if previous_records:
        template_keys = list(previous_records[0].get("route", {}).keys())
        ordered = {}
        for key in template_keys:
            if key in lane_route:
                ordered[key] = lane_route.pop(key)
        ordered.update(lane_route)
        lane_route = ordered

    lane = {
        "row_number": next_row_number(previous_records),
        "route": lane_route,
        "rates": [],
    }
    update_costs(base_rates_template, lane, update_record)
    return lane


def find_insert_index_for_new_cost(base_rates_template, new_cost):
    cost_name = str(new_cost.get("cost_name", ""))
    if cost_name.startswith("Base Rate ("):
        last_war_risk = -1
        for i, c in enumerate(base_rates_template):
            existing_name = str(c.get("cost_name", ""))
            if existing_name.startswith("War Risk Fee (") and "BAF" not in existing_name and "ETS" not in existing_name:
                last_war_risk = i
        if last_war_risk >= 0:
            return last_war_risk + 1
    return len(base_rates_template)


def ensure_global_cost_layout(previous_records, base_rates_template, update_records):
    existing_keys = {
        (str(c.get("cost_name")), str(c.get("container_type"))) for c in base_rates_template
    }

    required = []
    for upd in update_records:
        for rate in upd.get("rates", []):
            charge_code = rate.get("charge_code")
            container = map_container_type(rate.get("container_type"))
            new_cost = make_new_cost(charge_code, container)
            key = (str(new_cost.get("cost_name")), str(new_cost.get("container_type")))
            if key not in existing_keys:
                required.append((key, new_cost))
                existing_keys.add(key)

    new_cost_names = []
    for _, new_cost in required:
        insert_at = find_insert_index_for_new_cost(base_rates_template, new_cost)
        base_rates_template.insert(insert_at, new_cost)
        new_cost_names.append(str(new_cost.get("cost_name")))

        for record in previous_records:
            if "rates" not in record or not isinstance(record["rates"], list):
                record["rates"] = []
            inserted = make_base_cost_template(new_cost)
            inserted["update_note"] = "(new)"
            record["rates"].insert(insert_at, inserted)
    return new_cost_names


def list_json_files():
    files = sorted(PROCESSING_DIR.glob("*.json"))
    if not files:
        raise FileNotFoundError(f"No JSON files found in {PROCESSING_DIR}")
    return files


def choose_file(files, prompt, cli_arg_index):
    if len(sys.argv) > cli_arg_index:
        raw = sys.argv[cli_arg_index].strip()
        if raw.isdigit():
            idx = int(raw) - 1
            if 0 <= idx < len(files):
                return files[idx]
            raise ValueError(f"Index out of range for argument {cli_arg_index}: {raw}")
        candidate = PROCESSING_DIR / raw
        if candidate.exists():
            return candidate
        raise FileNotFoundError(f"File not found for argument {cli_arg_index}: {raw}")

    print(prompt)
    for i, file_path in enumerate(files, start=1):
        print(f"{i}. {file_path.name}")
    selected = input("Enter file number: ").strip()
    if not selected.isdigit():
        raise ValueError("Please enter a valid number.")
    idx = int(selected) - 1
    if not (0 <= idx < len(files)):
        raise ValueError("Selected number is out of range.")
    return files[idx]


def output_path_for(previous_json_path: Path):
    return previous_json_path.with_name(f"{previous_json_path.stem}_updated.json")


def renumber_rows_and_lanes(records):
    if not records:
        return
    try:
        start_row = int(records[0].get("row_number", 8))
    except (TypeError, ValueError):
        start_row = 8
    for idx, rec in enumerate(records):
        rec["row_number"] = start_row + idx
        rec.setdefault("route", {})["Lane #"] = str(idx + 1)


def intersection_period(lane_from, lane_to, upd_from, upd_to):
    if not all([lane_from, lane_to, upd_from, upd_to]):
        return (None, None)
    start = max(lane_from, upd_from)
    end = min(lane_to, upd_to)
    if start > end:
        return (None, None)
    return (start, end)


def needs_validity_split(lane_from, lane_to, upd_from, upd_to):
    int_from, int_to = intersection_period(lane_from, lane_to, upd_from, upd_to)
    if int_from is None:
        return False
    return not (int_from == lane_from and int_to == lane_to)


def build_lane_segments(lane, lane_from, lane_to, upd_from, upd_to, card_from=None, card_to=None):
    int_from, int_to = intersection_period(lane_from, lane_to, upd_from, upd_to)
    if int_from is None:
        return []
    int_from, int_to = clamp_period_to_rate_card(int_from, int_to, card_from, card_to)
    if int_from is None:
        return []

    segments = []
    if lane_from < int_from:
        prefix = deepcopy(lane)
        prefix["route"]["Valid to"] = format_ddmmyyyy(int_from - timedelta(days=1))
        prefix["route"]["update_note"] = "(updated)"
        prefix["route"]["update_source"] = "BASE"
        prefix["route"]["update_changed_fields"] = ["Valid to"]
        segments.append({"kind": "prefix", "lane": prefix})

    update_segment = deepcopy(lane)
    update_segment["route"]["Valid from"] = format_ddmmyyyy(int_from)
    update_segment["route"]["Valid to"] = format_ddmmyyyy(int_to)
    update_segment["route"]["update_note"] = "(new)"
    update_segment["route"]["update_source"] = "BASE"
    update_segment["route"].pop("update_changed_fields", None)
    segments.append({"kind": "update", "lane": update_segment})

    if int_to < lane_to:
        suffix = deepcopy(lane)
        suffix["route"]["Valid from"] = format_ddmmyyyy(int_to + timedelta(days=1))
        suffix["route"]["Valid to"] = format_ddmmyyyy(lane_to)
        suffix["route"]["update_note"] = "(new)"
        suffix["route"]["update_source"] = "BASE"
        suffix["route"].pop("update_changed_fields", None)
        segments.append({"kind": "suffix", "lane": suffix})

    return segments


def main():
    files = list_json_files()
    previous_json = choose_file(files, "Choose previous rate card JSON to update:", 1)
    rate_update_json = choose_file(files, "Choose rate update JSON to apply (BASE sheet):", 2)

    previous = json.loads(previous_json.read_text(encoding="utf-8"))
    updates = json.loads(rate_update_json.read_text(encoding="utf-8"))

    previous_records = previous.get("records", [])
    migrate_cost_names_in_records(previous_records)
    all_update_records = updates.get("records", []) or []
    update_records = [r for r in all_update_records if r.get("sheet_name") == "BASE"]
    if not update_records:
        raise ValueError("No BASE records found in rate update file.")
    # Merge KEY/SERVICE across all sheets (e.g. CY-CY on BASE + CY-CY_BU on ETSBAF).
    update_records = filter_service_variant_conflicts(
        update_records, variant_catalog=all_update_records
    )
    update_records = filter_container_alias_conflicts(update_records)
    rate_card_source = previous.get("source_file")
    update_records = [
        upd
        for upd in update_records
        if not should_skip_route_update(upd.get("route", {}), rate_card_source)[0]
    ]

    template_record = get_lane_template(previous_records) if previous_records else {"rates": []}
    base_rates_template = template_record.get("rates", [])
    new_cost_names = ensure_global_cost_layout(previous_records, base_rates_template, update_records)
    card_from, card_to = parse_validity_range(previous.get("rate_card_validity"))

    for upd in update_records:
        tid = upd.get("route", {}).get("Transporeon ID")
        if not tid:
            continue

        upd_from = parse_yyyymmdd(upd.get("route", {}).get("RATE_EFFECTIVE_DATE__C"))
        upd_to = parse_yyyymmdd(upd.get("route", {}).get("RATE_EXPIRATION_DATE__C"))
        if card_from and card_to and upd_from and upd_to:
            if not validity_ranges_intersect(upd_from, upd_to, card_from, card_to):
                continue
        matching_idxs = [
            i
            for i, rec in enumerate(previous_records)
            if rec.get("route", {}).get("Transporeon ID") == tid
            and not should_skip_route_update(rec.get("route", {}), rate_card_source)[0]
        ]

        if not matching_idxs:
            new_lane = build_new_lane(upd, previous_records, base_rates_template, card_from, card_to)
            if new_lane is not None:
                previous_records.append(new_lane)
            continue

        applied = False
        for idx in matching_idxs:
            lane = previous_records[idx]
            lane_from = parse_ddmmyyyy(lane.get("route", {}).get("Valid from"))
            lane_to = parse_ddmmyyyy(lane.get("route", {}).get("Valid to"))
            if not (upd_from and upd_to and lane_from and lane_to):
                continue

            if lane_to < upd_from or lane_from > upd_to:
                continue

            if needs_validity_split(lane_from, lane_to, upd_from, upd_to):
                segments = build_lane_segments(
                    lane, lane_from, lane_to, upd_from, upd_to, card_from, card_to
                )
                if not segments:
                    continue
                applied = True
                for segment_item in segments:
                    if segment_item["kind"] == "update":
                        stamp_variant_fields(segment_item["lane"], upd)
                        stamp_location_fields(segment_item["lane"], upd)
                        apply_update_only_to_costs(
                            segment_item["lane"], upd, base_rates_template
                        )
                # Keep the first segment in place (usually the shortened original).
                # Append any additional split-created lanes at the end of the file.
                previous_records[idx] = segments[0]["lane"]
                for segment_item in segments[1:]:
                    previous_records.append(segment_item["lane"])
            else:
                applied = True
                lane.setdefault("route", {})["update_note"] = "(updated)"
                lane["route"]["update_source"] = "BASE"
                stamp_variant_fields(lane, upd)
                stamp_location_fields(lane, upd)
                apply_update_only_to_costs(lane, upd, base_rates_template)

        if not applied:
            new_lane = build_new_lane(upd, previous_records, base_rates_template, card_from, card_to)
            if new_lane is not None:
                previous_records.append(new_lane)

    renumber_rows_and_lanes(previous_records)

    previous["records"] = previous_records
    previous["record_count"] = len(previous_records)
    previous["update_context"] = {
        "source_rate_update_file": str(rate_update_json),
        "sheet_used": "BASE",
        "new_cost_names": new_cost_names,
    }
    target_output = output_path_for(previous_json)
    target_output.write_text(json.dumps(previous, indent=2), encoding="utf-8")
    print(f"Created updated file: {target_output}")
    print(f"Source previous: {previous_json}")
    print(f"Source rate update: {rate_update_json}")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback

        traceback.print_exc()
        raise
