import json
import sys
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path

from route_utils import (
    map_update_route_to_rate_card_fields,
    should_skip_route_update,
    trim_service,
)
from cost_utils import merge_metrics, migrate_cost_names_in_records, normalize_container_code, normalize_cost_title


ROOT = Path(__file__).resolve().parent
PROCESSING_DIR = ROOT / "processing"

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


def is_bu_related_service(service_value):
    return str(service_value or "").endswith("_BU")


def is_bu_related_update(update_record):
    route = update_record.get("route", {})
    service = route.get("SERVICE__C")
    if is_bu_related_service(service):
        return True
    key = str(route.get("KEY") or "")
    return "_CY-CY_BU-" in key or "_CY-CY_BU_" in key


def filter_bu_service_conflicts(update_records):
    grouped = {}
    for rec in update_records:
        tid = rec.get("route", {}).get("Transporeon ID") or ""
        grouped.setdefault(tid, []).append(rec)

    drop_ids = set()
    for tid, group in grouped.items():
        if not tid:
            continue
        has_standard = any(not is_bu_related_update(r) for r in group)
        if not has_standard:
            continue
        for rec in group:
            if is_bu_related_update(rec):
                drop_ids.add(id(rec))

    return [rec for rec in update_records if id(rec) not in drop_ids]


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
    lane = {
        "row_number": next_row_number(previous_records),
        "route": {
            "Lane #": next_lane_number(previous_records),
            "Transporeon ID": route.get("Transporeon ID"),
            "KEY": route.get("KEY"),
            "Carrier": route.get("CARRIER"),
            "SERVICE": "not PRECARRIAGE/ONCARRIAGE",
            "SERVICE__C": route.get("SERVICE__C"),
            "Service": trim_service(route.get("SERVICE__C")),
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
        },
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
    update_records = [r for r in updates.get("records", []) if r.get("sheet_name") == "BASE"]
    if not update_records:
        raise ValueError("No BASE records found in rate update file.")
    update_records = filter_bu_service_conflicts(update_records)
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

        inserted_offset = 0
        applied = False
        for base_idx in matching_idxs:
            idx = base_idx + inserted_offset
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
                for seg_idx, segment_item in enumerate(segments):
                    segment = segment_item["lane"]
                    if segment_item["kind"] == "update":
                        apply_update_only_to_costs(segment, upd, base_rates_template)
                    if seg_idx == 0:
                        previous_records[idx] = segment
                    else:
                        previous_records.insert(idx + seg_idx, segment)
                inserted_offset += len(segments) - 1
            else:
                applied = True
                lane.setdefault("route", {})["update_note"] = "(updated)"
                lane["route"]["update_source"] = "BASE"
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
    main()
