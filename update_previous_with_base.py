import json
import sys
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path


ROOT = Path(__file__).resolve().parent
PROCESSING_DIR = ROOT / "processing"

def trim_service(service_value):
    service = "" if service_value is None else str(service_value)
    if service.startswith("OC_CNTR_"):
        service = service[len("OC_CNTR_") :]
    elif service.startswith("OC_CNTTR_"):
        service = service[len("OC_CNTTR_") :]
    if service.endswith("_BU"):
        service = service[: -len("_BU")]
    return service


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


def map_container_type(update_container_type):
    if not update_container_type:
        return None
    raw = str(update_container_type)
    return raw.replace("CNTR", "")


def map_charge_name(charge_code):
    mapping = {
        "base": "Base Rate",
        "baf": "BAF Fee",
        "ets": "EU ETS Fee",
        "dthc": "Destination Terminal Handling Fee",
        "othc": "Origin Terminal Handling Fee",
        "wrf": "War Risk Fee",
    }
    return mapping.get(str(charge_code).lower(), str(charge_code).upper())


def should_ignore_min(charge_code, container_type):
    # Ignore MIN for containerized Base Rate updates.
    return str(charge_code).lower() == "base" and container_type not in (None, "", "None")


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
        return f"{base_name} ({container_type})"
    return base_name


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


def make_new_cost(charge_code, container_type):
    return {
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
    }


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

        if lookup_key in index:
            target = lane_costs[index[lookup_key]]
            is_new_cost = False
        else:
            target = make_new_cost(charge_code, container)
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
        key = (candidate_name, str(container))

        if key in index:
            target = lane_costs[index[key]]
        else:
            target = make_new_cost(charge_code, container)
            target["update_note"] = "(new)"
            insert_at = template_index.get(key, len(lane_costs))
            lane_costs.insert(insert_at, target)
            index = {
                (str(c.get("cost_name")), str(c.get("container_type"))): i
                for i, c in enumerate(lane_costs)
            }
            target = lane_costs[index[key]]

        target["currency"] = upd.get("currency")
        target["flat_min"] = None if should_ignore_min(charge_code, container) else upd.get("min")
        target["p_unit"] = upd.get("rate")
        if target.get("update_note") != "(new)":
            target["update_note"] = "(updated)"

    lane_record["rates"] = lane_costs


def build_new_lane(update_record, previous_records, base_rates_template):
    route = update_record.get("route", {})
    lane = {
        "row_number": next_row_number(previous_records),
        "route": {
            "Lane #": next_lane_number(previous_records),
            "Transporeon ID": route.get("Transporeon ID"),
            "KEY": route.get("KEY"),
            "Carrier": route.get("CARRIER"),
            "SERVICE": "not PRECARRIAGE/ONCARRIAGE",
            "SERVICE_C": route.get("SERVICE__C"),
            "Service": trim_service(route.get("SERVICE__C")),
            "Valid from": to_ddmmyyyy(route.get("RATE_EFFECTIVE_DATE__C")),
            "Valid to": to_ddmmyyyy(route.get("RATE_EXPIRATION_DATE__C")),
            "Origin Port": route.get("ORIGIN_LOCATION_NAME__C"),
            "ORIGIN_COUNTRY__C": route.get("ORIGIN_COUNTRY__C"),
            "Destination Port": route.get("DESTINATION_LOCATION_NAME__C"),
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


def main():
    files = list_json_files()
    previous_json = choose_file(files, "Choose previous rate card JSON to update:", 1)
    rate_update_json = choose_file(files, "Choose rate update JSON to apply (BASE sheet):", 2)

    previous = json.loads(previous_json.read_text(encoding="utf-8"))
    updates = json.loads(rate_update_json.read_text(encoding="utf-8"))

    previous_records = previous.get("records", [])
    update_records = [r for r in updates.get("records", []) if r.get("sheet_name") == "BASE"]
    if not update_records:
        raise ValueError("No BASE records found in rate update file.")

    template_record = get_lane_template(previous_records) if previous_records else {"rates": []}
    base_rates_template = template_record.get("rates", [])
    new_cost_names = ensure_global_cost_layout(previous_records, base_rates_template, update_records)

    for upd in update_records:
        tid = upd.get("route", {}).get("Transporeon ID")
        if not tid:
            continue

        upd_from = parse_yyyymmdd(upd.get("route", {}).get("RATE_EFFECTIVE_DATE__C"))
        upd_to = parse_yyyymmdd(upd.get("route", {}).get("RATE_EXPIRATION_DATE__C"))
        matching_idxs = [
            i for i, rec in enumerate(previous_records) if rec.get("route", {}).get("Transporeon ID") == tid
        ]

        if not matching_idxs:
            new_lane = build_new_lane(upd, previous_records, base_rates_template)
            previous_records.append(new_lane)
            continue

        inserted_offset = 0
        for base_idx in matching_idxs:
            idx = base_idx + inserted_offset
            lane = previous_records[idx]
            lane_from = parse_ddmmyyyy(lane.get("route", {}).get("Valid from"))
            lane_to = parse_ddmmyyyy(lane.get("route", {}).get("Valid to"))
            if not (upd_from and upd_to and lane_from and lane_to):
                continue

            if lane_to < upd_from or lane_from > upd_to:
                continue

            # If lane starts before update period and intersects it, split lane.
            if lane_from < upd_from <= lane_to:
                lane["route"]["Valid to"] = format_ddmmyyyy(upd_from - timedelta(days=1))
                lane.setdefault("route", {})["update_note"] = "(updated)"
                lane["route"]["update_source"] = "BASE"
                lane["route"]["update_changed_fields"] = ["Valid to"]

                new_lane = deepcopy(lane)
                new_lane["route"]["Valid from"] = format_ddmmyyyy(upd_from)
                new_lane["route"]["Valid to"] = format_ddmmyyyy(upd_to)
                new_lane.setdefault("route", {})["update_note"] = "(new)"
                new_lane["route"]["update_source"] = "BASE"
                new_lane["route"].pop("update_changed_fields", None)
                apply_update_only_to_costs(new_lane, upd, base_rates_template)

                previous_records.insert(idx + 1, new_lane)
                inserted_offset += 1
            else:
                lane.setdefault("route", {})["update_note"] = "(updated)"
                lane["route"]["update_source"] = "BASE"
                apply_update_only_to_costs(lane, upd, base_rates_template)

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
