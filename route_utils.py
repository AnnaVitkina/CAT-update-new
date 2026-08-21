import re
from pathlib import Path


CNSHA_PORT_VALUE = "CNSHA/CNSHG/CNSGH"


def trim_service(service_value):
    service = "" if service_value is None else str(service_value)
    if service.startswith("OC_CNTR_"):
        service = service[len("OC_CNTR_") :]
    elif service.startswith("OC_CNTTR_"):
        service = service[len("OC_CNTTR_") :]
    if service.endswith("_BU"):
        service = service[: -len("_BU")]
    return service


def rate_card_includes_fr(source_file):
    if not source_file:
        return False
    stem = Path(str(source_file)).stem.upper()
    return bool(re.search(r"(?:^|[_\s\-])FR(?:[_\s\-]|$)", stem))


def route_destination_country(route):
    for key in (
        "DESTINATION_COUNTRY__C",
        "Destination Country",
        "DESTINATION_COUNTRY",
    ):
        value = route.get(key)
        if value not in (None, ""):
            return str(value).strip().upper()
    return ""


def route_service_value(route):
    return route.get("SERVICE__C") or route.get("Service") or route.get("SERVICE_C")


def is_cfs_cfs_route(route):
    service = trim_service(route_service_value(route))
    if service == "CFS-CFS":
        return True
    return "CFS-CFS" in str(route_service_value(route) or "")


def split_location_name(location):
    if location in (None, ""):
        return ("", "")
    raw = str(location).strip()
    if "_" in raw:
        port, postal = raw.split("_", 1)
        return (port.strip(), postal.strip())
    return (raw, "")


def format_port_for_rate_card(location):
    port, _ = split_location_name(location)
    if port == "CNSHA":
        return CNSHA_PORT_VALUE
    return port or None


def format_postal_for_rate_card(location):
    _, postal = split_location_name(location)
    return postal


def should_skip_route_update(route, rate_card_source_file):
    if is_cfs_cfs_route(route):
        return True, "CFS-CFS service"

    destination = route_destination_country(route)
    includes_fr = rate_card_includes_fr(rate_card_source_file)

    if includes_fr:
        if destination != "FR":
            return True, "Only FR destinations are covered by FR rate card"
    elif destination == "FR":
        return True, "FR destination not covered by rate card"

    return False, None


def map_update_route_to_rate_card_fields(route):
    origin_location = route.get("ORIGIN_LOCATION_NAME__C")
    destination_location = route.get("DESTINATION_LOCATION_NAME__C")
    return {
        "Origin Port": format_port_for_rate_card(origin_location),
        "Origin Postal Code": format_postal_for_rate_card(origin_location),
        "Destination Port": format_port_for_rate_card(destination_location),
        "Destination Postal Code": format_postal_for_rate_card(destination_location),
    }
