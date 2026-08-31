import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

from route_utils import (
    should_skip_route_update,
)
from cost_utils import (
    costs_same_family,
    ensure_cost_container_type,
    extract_container_from_cost_title,
    migrate_cost_names_in_records,
    normalize_container_code,
    normalize_cost_title,
    resolve_etsbaf_container,
)


from paths import PROCESSING_DIR


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


def fmt(date_value):
    return date_value.strftime("%d.%m.%Y")


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
    return previous_json_path.with_name(f"{previous_json_path.stem}_updated_etsbaf.json")


def not_performed_path_for(previous_json_path: Path):
    return previous_json_path.with_name(f"{previous_json_path.stem}_etsbaf_not_performed.json")


def map_container(raw):
    if not raw:
        return None
    return normalize_container_code(str(raw).replace("CNTR", ""))


def map_charge(charge_code):
    code = str(charge_code).lower()
    if code == "baf":
        return "BAF Fee"
    if code == "ets":
        return "EU ETS Fee"
    return None


def target_cost_name(charge_code, container):
    charge = map_charge(charge_code)
    if not charge or not container:
        return None
    return normalize_cost_title(f"{charge} ({container})")


def rate_by_for_container(container):
    mapping = {
        "22G0": "Container/20FT",
        "25G0": "Container/20HC",
        "42G0": "Container/40FT",
        "45G0": "Container/40HC",
        "52G0": "Container/40HC",
    }
    return mapping.get(str(container), "")


def full_cost_name(base_name, start_date, end_date):
    return f"{base_name[:-1]} {fmt(start_date)}-{fmt(end_date)})" if base_name.endswith(")") else base_name


def parse_validity_period(period_str):
    if not period_str or "-" not in str(period_str):
        return (None, None)
    start_raw, end_raw = str(period_str).split("-", 1)
    return parse_ddmmyyyy(start_raw.strip()), parse_ddmmyyyy(end_raw.strip())


def periods_intersect(period_a_from, period_a_to, period_b_from, period_b_to):
    if not all([period_a_from, period_a_to, period_b_from, period_b_to]):
        return True
    return not (period_a_to < period_b_from or period_a_from > period_b_to)


def period_contains(outer_from, outer_to, inner_from, inner_to):
    if not all([outer_from, outer_to, inner_from, inner_to]):
        return False
    return outer_from <= inner_from and outer_to >= inner_to


def lane_validity(lane):
    route = lane.get("route", {})
    return parse_ddmmyyyy(route.get("Valid from")), parse_ddmmyyyy(route.get("Valid to"))


def cost_validity(cost):
    return parse_validity_period(cost.get("validity_period"))


def family_blocks(rates, base_name):
    return [c for c in rates if costs_same_family(c.get("cost_name"), base_name)]


def resolve_template_validity(all_records, base_name, upd_from, upd_to, cache=None):
    cache_key = (base_name, upd_from)
    if cache is not None and cache_key in cache:
        return cache[cache_key]

    ends = Counter()
    for rec in all_records:
        for cost in rec.get("rates", []):
            if not costs_same_family(cost.get("cost_name"), base_name):
                continue
            c_from, c_to = cost_validity(cost)
            if c_from == upd_from and c_to:
                ends[c_to] += 1
    result = (upd_from, ends.most_common(1)[0][0]) if ends else (upd_from, upd_to)
    if cache is not None:
        cache[cache_key] = result
    return result


def lane_intersects_update(lane_from, lane_to, upd_from, upd_to):
    if not all([lane_from, lane_to, upd_from, upd_to]):
        return True
    return periods_intersect(lane_from, lane_to, upd_from, upd_to)


def equipment_size_class(container):
    if not container:
        return None
    code = str(container)
    if code.startswith(("42", "45", "52")):
        return "40"
    if code.startswith(("22", "25")):
        return "20"
    return code[:2]


def sibling_has_rate_on_validity(lane, cost, container):
    validity = cost.get("validity_period")
    if not validity or not container:
        return False
    size_class = equipment_size_class(container)
    charge_family = str(cost.get("cost_name", "")).split(" (", 1)[0]
    for other in lane.get("rates", []):
        if other is cost:
            continue
        if other.get("validity_period") != validity:
            continue
        if not str(other.get("cost_name", "")).startswith(charge_family):
            continue
        other_container = extract_container_from_cost_title(other.get("cost_name"))
        if not other_container or other_container == container:
            continue
        if equipment_size_class(other_container) != size_class:
            continue
        if other.get("p_unit") is not None:
            return True
    return False


def intersecting_blocks(family, upd_from, upd_to, lane_from, lane_to, lane):
    matched = []
    lane_overlaps_update = lane_intersects_update(lane_from, lane_to, upd_from, upd_to)
    for cost in family:
        ensure_cost_container_type(cost)
        c_from, c_to = cost_validity(cost)
        if c_from and c_to:
            if lane_from and lane_to and not periods_intersect(lane_from, lane_to, c_from, c_to):
                continue
            if not periods_intersect(c_from, c_to, upd_from, upd_to):
                continue
            container = extract_container_from_cost_title(cost.get("cost_name"))
            if not lane_overlaps_update and sibling_has_rate_on_validity(lane, cost, container):
                continue
            matched.append(cost)
        elif not cost.get("validity_period"):
            matched.append(cost)
    return matched


def apply_rate_to_block(cost, base_name, currency, rate, upd_from, upd_to, all_records, cache=None):
    ensure_cost_container_type(cost)
    if not cost.get("validity_period"):
        block_from, block_to = resolve_template_validity(all_records, base_name, upd_from, upd_to, cache)
        cost["validity_period"] = f"{fmt(block_from)}-{fmt(block_to)}"
        cost["cost_name"] = full_cost_name(base_name, block_from, block_to)
    cost["currency"] = currency
    cost["p_unit"] = rate
    cost["flat_min"] = None
    if cost.get("update_note") != "(new)":
        cost["update_note"] = "(updated)"


def create_versioned_block(lane, base_name, container, update_rate, upd_from, upd_to, all_records, template=None, cache=None):
    block_from, block_to = resolve_template_validity(all_records, base_name, upd_from, upd_to, cache)
    source = template or {}
    new_block = {
        "cost_name": full_cost_name(base_name, block_from, block_to),
        "container_type": container,
        "apply_if": source.get("apply_if") or "Applies if invoiced by Carrier",
        "validity_period": f"{fmt(block_from)}-{fmt(block_to)}",
        "cost_to_prolong": source.get("cost_to_prolong"),
        "rate_by": source.get("rate_by") or rate_by_for_container(container),
        "rule": source.get("rule") or "Regular rule",
        "currency": update_rate.get("currency"),
        "flat_min": None,
        "p_unit": update_rate.get("rate"),
        "update_note": "(new)",
    }
    lane.setdefault("rates", []).append(new_block)
    return new_block


def cleanup_non_overlapping_etsbaf_values(lane):
    lane_from, lane_to = lane_validity(lane)
    if not (lane_from and lane_to):
        return
    for cost in lane.get("rates", []):
        name = str(cost.get("cost_name", ""))
        if not (name.startswith("BAF Fee") or name.startswith("EU ETS Fee")):
            continue
        c_from, c_to = cost_validity(cost)
        if not all([c_from, c_to]):
            continue
        if periods_intersect(lane_from, lane_to, c_from, c_to):
            continue
        cost["currency"] = None
        cost["p_unit"] = None
        cost["flat_min"] = None
        cost.pop("update_note", None)


def find_matching_lanes(records, transporeon_id, rate_card_source_file):
    lane_from, lane_to = lane_validity(lane)
    if not (lane_from and lane_to):
        return
    for cost in lane.get("rates", []):
        name = str(cost.get("cost_name", ""))
        if not (name.startswith("BAF Fee") or name.startswith("EU ETS Fee")):
            continue
        c_from, c_to = cost_validity(cost)
        if not all([c_from, c_to]):
            continue
        if periods_intersect(lane_from, lane_to, c_from, c_to):
            continue
        cost["currency"] = None
        cost["p_unit"] = None
        cost["flat_min"] = None
        cost.pop("update_note", None)


def find_matching_lanes(records, transporeon_id, rate_card_source_file):
    lanes = [r for r in records if r.get("route", {}).get("Transporeon ID") == transporeon_id]
    return [
        lane
        for lane in lanes
        if not should_skip_route_update(lane.get("route", {}), rate_card_source_file)[0]
    ]


def update_or_create_cost_block(lane, update_rate, upd_from, upd_to, all_records, cache=None):
    container = map_container(update_rate.get("container_type"))
    container = resolve_etsbaf_container(lane.get("rates", []), update_rate.get("charge_code"), container, target_cost_name)
    base_name = target_cost_name(update_rate.get("charge_code"), container)
    if not base_name:
        return False

    lane_from, lane_to = lane_validity(lane)
    rates = lane.get("rates", [])
    family = family_blocks(rates, base_name)
    targets = intersecting_blocks(family, upd_from, upd_to, lane_from, lane_to, lane)
    if targets:
        for target in targets:
            apply_rate_to_block(
                target,
                base_name,
                update_rate.get("currency"),
                update_rate.get("rate"),
                upd_from,
                upd_to,
                all_records,
                cache,
            )
        return True

    if lane_from and lane_to and not periods_intersect(lane_from, lane_to, upd_from, upd_to):
        return False

    if lane_from and lane_to:
        block_from, block_to = resolve_template_validity(all_records, base_name, upd_from, upd_to, cache)
        if not periods_intersect(lane_from, lane_to, block_from, block_to):
            return False

    template = family[-1] if family else None
    create_versioned_block(lane, base_name, container, update_rate, upd_from, upd_to, all_records, template, cache)
    return True


def container_sort_key(container_value):
    if not container_value:
        return 999
    raw = str(container_value).replace("CNTR", "")
    order = {"22G0": 22, "25G0": 25, "42G0": 42, "45G0": 45, "52G0": 52}
    return order.get(raw, 999)


def cost_group_sort_key(cost_name):
    name = str(cost_name or "")
    if name.startswith("BAF Fee"):
        return 0
    if name.startswith("EU ETS Fee"):
        return 1
    return 2


def validity_sort_key(validity_period):
    if not validity_period or "-" not in str(validity_period):
        return 99999999
    start = str(validity_period).split("-", 1)[0].strip()
    d = parse_ddmmyyyy(start)
    if d is None:
        return 99999999
    return int(d.strftime("%Y%m%d"))


def reorder_etsbaf_costs(lane):
    rates = lane.get("rates", [])
    if not rates:
        return

    groups = []
    others = []
    for idx, cost in enumerate(rates):
        group = cost_group_sort_key(cost.get("cost_name"))
        if group in (0, 1):
            groups.append((idx, cost))
        else:
            others.append((idx, cost))

    # Desired order: per container -> BAF(all validities) -> EU ETS(all validities)
    groups.sort(
        key=lambda x: (
            container_sort_key(x[1].get("container_type")),
            cost_group_sort_key(x[1].get("cost_name")),
            validity_sort_key(x[1].get("validity_period")),
            str(x[1].get("cost_name", "")),
            x[0],
        )
    )

    if not groups:
        return

    first_group_pos = min(i for i, _ in groups)
    filtered = [c for i, c in enumerate(rates) if i < first_group_pos or cost_group_sort_key(c.get("cost_name")) == 2]
    ordered_groups = [c for _, c in groups]
    lane["rates"] = filtered[:first_group_pos] + ordered_groups + filtered[first_group_pos:]


def cleanup_replaced_plain_costs(lane):
    rates = lane.get("rates", [])
    if not rates:
        return

    # If a validity-suffixed cost exists for a family/container, remove empty plain stub.
    to_remove = []
    for idx, cost in enumerate(rates):
        name = str(cost.get("cost_name", ""))
        container = str(cost.get("container_type", ""))
        if "BAF Fee (" not in name and "EU ETS Fee (" not in name:
            continue
        # plain form like "BAF Fee (22G0)"
        is_plain = bool(name.endswith(f"({container})"))
        if not is_plain:
            continue

        has_versioned = any(
            (other is not cost)
            and str(other.get("container_type", "")) == container
            and str(other.get("cost_name", "")).startswith(name[:-1])
            and ("-" in str(other.get("validity_period", "")))
            for other in rates
        )
        is_empty_stub = (
            cost.get("currency") is None
            and cost.get("flat_min") is None
            and cost.get("p_unit") is None
        )
        if has_versioned and is_empty_stub:
            to_remove.append(idx)

    for idx in reversed(to_remove):
        rates.pop(idx)
    lane["rates"] = rates


def cleanup_plain_stubs_globally(records):
    # If versioned costs exist for a family/container anywhere,
    # remove empty plain stubs of that family/container everywhere.
    versioned_families = set()
    for rec in records:
        for cost in rec.get("rates", []):
            name = str(cost.get("cost_name", ""))
            container = str(cost.get("container_type", ""))
            if not container:
                continue
            if (name.startswith("BAF Fee (") or name.startswith("EU ETS Fee (")) and "-" in str(cost.get("validity_period", "")):
                family_key = f"{name.split('(')[0].strip()} ({container}"
                versioned_families.add(family_key)

    for rec in records:
        rates = rec.get("rates", [])
        filtered = []
        for cost in rates:
            name = str(cost.get("cost_name", ""))
            container = str(cost.get("container_type", ""))
            is_plain = name in {f"BAF Fee ({container})", f"EU ETS Fee ({container})"}
            family_key = f"{name.split('(')[0].strip()} ({container}" if container else ""
            is_empty = (
                cost.get("currency") is None
                and cost.get("flat_min") is None
                and cost.get("p_unit") is None
                and not cost.get("validity_period")
            )
            if is_plain and is_empty and family_key in versioned_families:
                continue
            filtered.append(cost)
        rec["rates"] = filtered


def dedupe_container_for_cost(cost):
    return str(
        extract_container_from_cost_title(cost.get("cost_name"))
        or cost.get("container_type")
        or ""
    )


def dedupe_same_validity_costs(lane):
    rates = lane.get("rates", [])
    if not rates:
        return

    def family_of(name):
        n = str(name or "")
        if n.startswith("BAF Fee"):
            return "BAF Fee"
        if n.startswith("EU ETS Fee"):
            return "EU ETS Fee"
        return None

    chosen = {}
    # Keep deterministic order by scanning left-to-right.
    for idx, cost in enumerate(rates):
        family = family_of(cost.get("cost_name"))
        container = dedupe_container_for_cost(cost)
        validity = str(cost.get("validity_period") or "")
        if family is None or not container or not validity:
            continue
        key = (family, container, validity)
        if key not in chosen:
            chosen[key] = idx
            continue

        prev_idx = chosen[key]
        prev = rates[prev_idx]
        cur_name = str(cost.get("cost_name") or "")
        prev_name = str(prev.get("cost_name") or "")

        # Prefer normalized name that contains explicit validity range in title.
        # Example prefer "BAF Fee (25G0 01.04.2026-14.05.2026)" over "... as of 01.04.2026".
        cur_has_range = "-" in cur_name
        prev_has_range = "-" in prev_name
        cur_has_asof = " as of " in cur_name.lower()
        prev_has_asof = " as of " in prev_name.lower()

        pick_cur = False
        if cur_has_range and not prev_has_range:
            pick_cur = True
        elif cur_has_range == prev_has_range:
            if prev_has_asof and not cur_has_asof:
                pick_cur = True

        if pick_cur:
            chosen[key] = idx

    keep_idxs = set(chosen.values())
    rebuilt = []
    for idx, cost in enumerate(rates):
        family = str(cost.get("cost_name") or "")
        if family.startswith("BAF Fee") or family.startswith("EU ETS Fee"):
            container = dedupe_container_for_cost(cost)
            validity = str(cost.get("validity_period") or "")
            if container and validity:
                if idx not in keep_idxs:
                    continue
        rebuilt.append(cost)
    lane["rates"] = rebuilt


def remove_empty_updated_blocks(lane):
    rates = lane.get("rates", [])
    if not rates:
        return
    filtered = []
    for cost in rates:
        is_marked = cost.get("update_note") in {"(new)", "(updated)"}
        is_empty = (
            cost.get("currency") is None
            and cost.get("flat_min") is None
            and cost.get("p_unit") is None
        )
        if is_marked and is_empty:
            continue
        filtered.append(cost)
    lane["rates"] = filtered


def main():
    files = list_json_files()
    previous_json = choose_file(files, "Choose previous rate card JSON to update (ETSBAF):", 1)
    rate_update_json = choose_file(files, "Choose rate update JSON source (ETSBAF):", 2)

    previous = json.loads(previous_json.read_text(encoding="utf-8"))
    updates = json.loads(rate_update_json.read_text(encoding="utf-8"))
    records = previous.get("records", [])
    migrate_cost_names_in_records(records)

    etsbaf_updates = [r for r in updates.get("records", []) if r.get("sheet_name") == "ETSBAF"]
    if not etsbaf_updates:
        raise ValueError("No ETSBAF updates found in selected rate update JSON.")

    rate_card_source = previous.get("source_file")
    etsbaf_updates = [
        upd
        for upd in etsbaf_updates
        if not should_skip_route_update(upd.get("route", {}), rate_card_source)[0]
    ]

    not_performed = []

    # Apply updates in chronological order per your versioning logic.
    etsbaf_updates.sort(
        key=lambda r: (
            str(r.get("route", {}).get("Transporeon ID", "")),
            str(r.get("route", {}).get("RATE_EFFECTIVE_DATE__C", "")),
        )
    )

    validity_cache = {}

    for upd in etsbaf_updates:
        route = upd.get("route", {})
        tid = route.get("Transporeon ID")
        if not tid:
            continue

        upd_from = parse_yyyymmdd(route.get("RATE_EFFECTIVE_DATE__C"))
        upd_to = parse_yyyymmdd(route.get("RATE_EXPIRATION_DATE__C"))
        if not (upd_from and upd_to):
            continue

        lanes = find_matching_lanes(records, tid, rate_card_source)
        if not lanes:
            not_performed.append(
                {
                    "reason": "Transporeon ID not found in previous rate card",
                    "transporeon_id": tid,
                    "update_row": upd,
                }
            )
            continue

        for lane in lanes:
            for ur in upd.get("rates", []):
                if str(ur.get("charge_code", "")).lower() not in {"baf", "ets"}:
                    continue
                update_or_create_cost_block(lane, ur, upd_from, upd_to, records, validity_cache)
            remove_empty_updated_blocks(lane)
            cleanup_replaced_plain_costs(lane)
            dedupe_same_validity_costs(lane)
            cleanup_non_overlapping_etsbaf_values(lane)
            reorder_etsbaf_costs(lane)

    previous["records"] = records
    for lane in previous["records"]:
        remove_empty_updated_blocks(lane)
        dedupe_same_validity_costs(lane)
        cleanup_non_overlapping_etsbaf_values(lane)
    cleanup_plain_stubs_globally(previous["records"])
    previous["record_count"] = len(records)
    previous["update_context"] = {
        "source_rate_update_file": str(rate_update_json),
        "sheet_used": "ETSBAF",
    }

    out_path = output_path_for(previous_json)
    out_path.write_text(json.dumps(previous, indent=2), encoding="utf-8")

    np_path = not_performed_path_for(previous_json)
    np_payload = {
        "sheet_used": "ETSBAF",
        "source_rate_update_file": str(rate_update_json),
        "not_performed_count": len(not_performed),
        "items": not_performed,
    }
    np_path.write_text(json.dumps(np_payload, indent=2), encoding="utf-8")

    print(f"Created ETSBAF updated file: {out_path}")
    print(f"Created ETSBAF not-performed file: {np_path}")
    if not_performed:
        print(f"ETSBAF not-performed count: {len(not_performed)}")


if __name__ == "__main__":
    main()
