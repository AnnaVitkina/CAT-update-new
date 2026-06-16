import json
import sys
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path


ROOT = Path(__file__).resolve().parent
PROCESSING_DIR = ROOT / "processing"


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
    return str(raw).replace("CNTR", "")


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
    return f"{charge} ({container})"


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


def find_matching_lanes(records, transporeon_id):
    return [r for r in records if r.get("route", {}).get("Transporeon ID") == transporeon_id]


def update_or_create_cost_block(lane, update_rate, upd_from, upd_to):
    container = map_container(update_rate.get("container_type"))
    base_name = target_cost_name(update_rate.get("charge_code"), container)
    if not base_name:
        return

    rates = lane.get("rates", [])
    candidates = [c for c in rates if c.get("cost_name", "").startswith(base_name[:-1])]
    if not candidates:
        # Create missing BAF/ETS block from scratch (empty->BASE->ETSBAF case).
        target_validity = f"{fmt(upd_from)}-{fmt(upd_to)}"
        new_block = {
            "cost_name": full_cost_name(base_name, upd_from, upd_to),
            "container_type": container,
            "apply_if": "Applies if invoiced by Carrier",
            "validity_period": target_validity,
            "cost_to_prolong": None,
            "rate_by": rate_by_for_container(container),
            "rule": "Regular rule",
            "currency": update_rate.get("currency"),
            "flat_min": None,
            "p_unit": update_rate.get("rate"),
            "update_note": "(new)",
        }
        rates.append(new_block)
        lane["rates"] = rates
        return

    # Choose first matching family member as template.
    template = candidates[0]

    # If existing has no validity, enrich it directly.
    if not template.get("validity_period"):
        template["validity_period"] = f"{fmt(upd_from)}-{fmt(upd_to)}"
        template["cost_name"] = full_cost_name(base_name, upd_from, upd_to)
        template["currency"] = update_rate.get("currency")
        template["p_unit"] = update_rate.get("rate")
        template["flat_min"] = None
        template["update_note"] = "(updated)"
        return

    # Find latest block of this cost family to split/version.
    family = [c for c in rates if c.get("cost_name", "").startswith(base_name[:-1])]

    target_validity = f"{fmt(upd_from)}-{fmt(upd_to)}"
    # If this exact validity already exists, update it in place (no duplicate block).
    same_validity = next((c for c in family if c.get("validity_period") == target_validity), None)
    if same_validity is not None:
        same_validity["cost_name"] = full_cost_name(base_name, upd_from, upd_to)
        same_validity["currency"] = update_rate.get("currency")
        same_validity["flat_min"] = None
        same_validity["p_unit"] = update_rate.get("rate")
        same_validity["update_note"] = "(updated)"
        lane["rates"] = rates
        return

    latest = family[-1]
    prev_validity = latest.get("validity_period")
    prev_from = None
    prev_to = None
    if isinstance(prev_validity, str) and "-" in prev_validity:
        a, b = prev_validity.split("-", 1)
        prev_from = parse_ddmmyyyy(a.strip())
        prev_to = parse_ddmmyyyy(b.strip())

    if prev_from and prev_to and upd_from > prev_from:
        trimmed_to = upd_from - timedelta(days=1)
        latest["validity_period"] = f"{fmt(prev_from)}-{fmt(trimmed_to)}"
        latest["cost_name"] = full_cost_name(base_name, prev_from, trimmed_to)
        latest["update_note"] = "(updated)"

    new_block = deepcopy(latest)
    new_block["cost_name"] = base_name
    new_block["validity_period"] = target_validity
    new_block["cost_to_prolong"] = latest.get("cost_name")
    new_block["currency"] = update_rate.get("currency")
    new_block["flat_min"] = None
    new_block["p_unit"] = update_rate.get("rate")
    new_block["update_note"] = "(new)"
    new_block["cost_name"] = full_cost_name(base_name, upd_from, upd_to)
    insert_idx = rates.index(latest) + 1
    rates.insert(insert_idx, new_block)
    lane["rates"] = rates


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
        container = str(cost.get("container_type") or "")
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
            container = str(cost.get("container_type") or "")
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

    etsbaf_updates = [r for r in updates.get("records", []) if r.get("sheet_name") == "ETSBAF"]
    if not etsbaf_updates:
        raise ValueError("No ETSBAF updates found in selected rate update JSON.")

    not_performed = []

    # Apply updates in chronological order per your versioning logic.
    etsbaf_updates.sort(
        key=lambda r: (
            str(r.get("route", {}).get("Transporeon ID", "")),
            str(r.get("route", {}).get("RATE_EFFECTIVE_DATE__C", "")),
        )
    )

    for upd in etsbaf_updates:
        route = upd.get("route", {})
        tid = route.get("Transporeon ID")
        if not tid:
            continue

        upd_from = parse_yyyymmdd(route.get("RATE_EFFECTIVE_DATE__C"))
        upd_to = parse_yyyymmdd(route.get("RATE_EXPIRATION_DATE__C"))
        if not (upd_from and upd_to):
            continue

        lanes = find_matching_lanes(records, tid)
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
                update_or_create_cost_block(lane, ur, upd_from, upd_to)
            remove_empty_updated_blocks(lane)
            cleanup_replaced_plain_costs(lane)
            dedupe_same_validity_costs(lane)
            reorder_etsbaf_costs(lane)

    previous["records"] = records
    for lane in previous["records"]:
        remove_empty_updated_blocks(lane)
        dedupe_same_validity_costs(lane)
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


if __name__ == "__main__":
    main()
