import re

METRIC_ORDER = ["currency", "flat_min", "p_unit"]

COST_TITLE_RENAMES = {
    "BAF Fee (FRK)": "BAF Fee (DFT)",
    "EU ETS Fee (FRK)": "EU ETS Fee (DFT)",
}

ETSBAF_FAMILIES = ("BAF Fee", "EU ETS Fee")


def normalize_cost_title(title):
    if title in (None, ""):
        return title
    text = str(title).strip()
    return COST_TITLE_RENAMES.get(text, text)


CONTAINER_ALIASES = {
    "22SOC": "22G0",
    "42P1S": "42G0",
}

ETSBAF_CONTAINER_FALLBACKS = {
    "42P1S": "42G0",
    "22SOC": "22G0",
}


def normalize_container_code(container):
    if container in (None, ""):
        return container
    raw = str(container).replace("CNTR", "")
    if raw == "FRK":
        return "DFT"
    return CONTAINER_ALIASES.get(raw, raw)


def extract_container_from_cost_title(title):
    if title in (None, ""):
        return None
    match = re.search(r"\(([^)]+)\)", str(title))
    if not match:
        return None
    inside = match.group(1).strip()
    if re.search(r"\d{2}\.\d{2}\.\d{4}", inside):
        token = inside.split()[0].strip()
        return normalize_container_code(token)
    return normalize_container_code(inside)


def cost_family_key(cost_name):
    title = normalize_cost_title(cost_name)
    if not title:
        return None
    for prefix in ETSBAF_FAMILIES:
        if title.startswith(prefix):
            container = extract_container_from_cost_title(title)
            if container:
                return (prefix, str(container))
    return None


def costs_same_family(cost_name, base_name):
    left = cost_family_key(cost_name)
    right = cost_family_key(base_name)
    return left is not None and left == right


def resolve_etsbaf_container(lane_rates, charge_code, container, target_cost_name_fn):
    base_name = target_cost_name_fn(charge_code, container)
    if base_name and any(costs_same_family(c.get("cost_name"), base_name) for c in lane_rates):
        return container
    fallback = ETSBAF_CONTAINER_FALLBACKS.get(str(container))
    if fallback:
        alt_name = target_cost_name_fn(charge_code, fallback)
        if alt_name and any(costs_same_family(c.get("cost_name"), alt_name) for c in lane_rates):
            return fallback
    return container


def ensure_cost_container_type(cost):
    if not cost:
        return
    from_name = extract_container_from_cost_title(cost.get("cost_name"))
    if from_name:
        cost["container_type"] = from_name
        return
    if cost.get("container_type"):
        cost["container_type"] = normalize_container_code(cost.get("container_type"))


def sort_metrics(metrics):
    chosen = {m for m in metrics if m in METRIC_ORDER}
    if not chosen:
        return ["currency", "p_unit"]
    return [m for m in METRIC_ORDER if m in chosen]


def merge_metrics(existing, new_metrics):
    merged = set(existing or [])
    merged.update(new_metrics or [])
    return sort_metrics(merged)


def migrate_cost_names_in_records(records):
    for rec in records:
        for cost in rec.get("rates", []):
            name = cost.get("cost_name")
            normalized = normalize_cost_title(name)
            if normalized != name:
                cost["cost_name"] = normalized
            prolong = cost.get("cost_to_prolong")
            if prolong:
                cost["cost_to_prolong"] = normalize_cost_title(prolong)
            ensure_cost_container_type(cost)
