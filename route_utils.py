import re
from pathlib import Path


CNSHA_PORT_VALUE = "CNSHA/CNSHG/CNSGH"
CNTXG_PORT_VALUE = "CNTXG/CNTSN"


def route_service_c_value(route_or_row):
    """Treat SERVICE_C and SERVICE__C as the same field."""
    if not route_or_row:
        return None
    for key in ("SERVICE__C", "SERVICE_C", "Service"):
        value = route_or_row.get(key)
        if value not in (None, ""):
            # Prefer raw service codes over trimmed display "Service".
            if key == "Service":
                continue
            return value
    for key in ("SERVICE__C", "SERVICE_C"):
        if key in route_or_row:
            return route_or_row.get(key)
    return None


def apply_service_c_aliases(route, service_value):
    """Write both rate-card and update-file spellings of SERVICE_C."""
    route["SERVICE_C"] = service_value
    route["SERVICE__C"] = service_value
    return route


def trim_service(service_value):
    service = "" if service_value is None else str(service_value)
    # Display form matches Transporeon / existing RA Service column.
    if service.startswith("OC_RRBB") or service == "RRBB":
        return "RRBB"
    if service.startswith("OC_LOLO") or service == "LOLO":
        return "LOLO"
    if service.startswith("OC_CNTR_"):
        service = service[len("OC_CNTR_") :]
    elif service.startswith("OC_CNTTR_"):
        service = service[len("OC_CNTTR_") :]
    # SPOT shares CY-CY identity: Service column stays CY-CY (prices may still be SPOT).
    if service == "SPOT":
        return "CY-CY"
    if service.endswith("_BU"):
        service = service[: -len("_BU")]
    return service


def trim_service_for_display(service_value):
    """Strip OC_CNTR_ prefix but keep _BU markers; SPOT displays as CY-CY."""
    service = "" if service_value is None else str(service_value).strip()
    if service.startswith("OC_RRBB") or service == "RRBB":
        return "RRBB"
    if service.startswith("OC_LOLO") or service == "LOLO":
        return "LOLO"
    if service.startswith("OC_CNTR_"):
        service = service[len("OC_CNTR_") :]
    elif service.startswith("OC_CNTTR_"):
        service = service[len("OC_CNTTR_") :]
    if service == "SPOT":
        return "CY-CY"
    return service


def trim_service_for_transporeon(service_value, key=None):
    key_str = str(key or "")
    if "OC_RRBB" in key_str:
        return "RRBB"
    if "OC_LOLO" in key_str:
        return "LOLO"

    service = "" if service_value is None else str(service_value)
    if service.startswith("OC_RRBB"):
        return "RRBB"
    if service.startswith("OC_LOLO"):
        return "LOLO"
    if service.startswith("OC_CNTR_"):
        service = service[len("OC_CNTR_") :]
    elif service.startswith("OC_CNTTR_"):
        service = service[len("OC_CNTTR_") :]
    # SPOT shares CY-CY Transporeon identity (same as CY-CY / CY-CY_BU).
    if service == "SPOT" or "OC_CNTR_SPOT" in key_str:
        return "CY-CY"
    if service.endswith("_BU"):
        service = service[: -len("_BU")]
    return service


def is_spot_service(service_value):
    service = str(service_value or "")
    return service == "OC_CNTR_SPOT" or service.endswith("_SPOT") or service == "SPOT"


def is_spot_key(key):
    return "OC_CNTR_SPOT" in str(key or "")


def is_bu_related_service(service_value):
    return str(service_value or "").endswith("_BU")


def build_transporeon_id(row_map):
    carrier_code = "" if row_map.get("CARRIER") is None else str(row_map.get("CARRIER"))
    service = trim_service_for_transporeon(route_service_c_value(row_map), row_map.get("KEY"))
    origin = (
        ""
        if row_map.get("ORIGIN_LOCATION_NAME__C") is None
        else str(row_map.get("ORIGIN_LOCATION_NAME__C"))
    )
    destination = (
        ""
        if row_map.get("DESTINATION_LOCATION_NAME__C") is None
        else str(row_map.get("DESTINATION_LOCATION_NAME__C"))
    )
    return carrier_code + service + origin + destination


def is_bu_key_lane(key):
    key_str = str(key or "")
    return "_BU-" in key_str or "_CY-CY_BU-" in key_str or "_CY-CY_BU_" in key_str


# Lower number = higher priority when validity conflicts on the same Transporeon ID.
PRIORITY_SPOT = 0
PRIORITY_STANDARD = 1
PRIORITY_BU = 2


def update_service_priority(route):
    service = route_service_c_value(route)
    key = (route or {}).get("KEY")
    if is_spot_service(service) or is_spot_key(key):
        return PRIORITY_SPOT
    if is_bu_related_service(service) or is_bu_key_lane(key):
        return PRIORITY_BU
    return PRIORITY_STANDARD


def is_d_cy_route(route_or_row):
    """Door-CY (OC_CNTR_D-CY) inland/door services are out of scope."""
    if not route_or_row:
        return False
    key = str(route_or_row.get("KEY") or "")
    service = str(route_service_c_value(route_or_row) or route_or_row.get("Service") or "")
    if "OC_CNTR_D-CY" in key or "OC_CNTR_D-CY" in service:
        return True
    trimmed = trim_service(service)
    return trimmed == "D-CY" or trimmed.startswith("D-CY")


def should_skip_rate_update_route(row_map):
    key = str(row_map.get("KEY") or "")
    if "OC_RRBB_SPOT" in key:
        return True, "OC_RRBB SPOT lane with PRP rates is out of scope"
    if is_d_cy_route(row_map):
        return True, "D-CY (door) service is out of scope"
    return False, None


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
    return route_service_c_value(route) or route.get("Service") or route.get("SERVICE_C")


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
    if port == "CNTXG":
        return CNTXG_PORT_VALUE
    return port or None


def format_postal_for_rate_card(location):
    """
    Combined location codes (PORT_POSTAL), e.g. ZADUR_ZAJNB / USCHI_USILM, go to
    Postal Code as the full value. Plain port codes do not use Postal.
    Port and Postal are mutually exclusive for a given side.
    """
    raw = str(location or "").strip()
    if "_" in raw:
        return raw
    return ""


def is_rail_service(service_value):
    """CY-RR / RR-CY (incl. _BU) rail services that use PORT_POSTAL location codes."""
    raw = str(service_value or "")
    if not raw:
        return False
    # Check raw / KEY-like tokens before trimming.
    if "CY-RR" in raw or "RR-CY" in raw:
        return True
    trimmed = trim_service(raw)
    return trimmed in {"CY-RR", "RR-CY"}


def should_skip_route_update(route, rate_card_source_file):
    if is_cfs_cfs_route(route):
        return True, "CFS-CFS service"
    if is_d_cy_route(route):
        return True, "D-CY (door) service is out of scope"

    destination = route_destination_country(route)
    includes_fr = rate_card_includes_fr(rate_card_source_file)

    if includes_fr:
        if destination != "FR":
            return True, "Only FR destinations are covered by FR rate card"
    elif destination == "FR":
        return True, "FR destination not covered by rate card"

    return False, None


def location_to_port_field(location):
    """Port column only when location is a plain port (no '_'); else empty."""
    raw = str(location or "").strip()
    if not raw or "_" in raw:
        return ""
    return format_port_for_rate_card(raw) or ""


def location_to_postal_field(location):
    """Postal column only for combined PORT_POSTAL values; else empty."""
    return format_postal_for_rate_card(location)


def map_update_route_to_rate_card_fields(route):
    """
    Map update location codes onto rate-card port/postal columns.

    For each side (origin / destination), only one column is filled:
      - plain code (e.g. CNTXG, GBSOU)     → Port (with aliases), Postal empty
      - combined (e.g. ZADUR_ZAJNB)         → Postal = full value, Port empty
    """
    origin_location = route.get("ORIGIN_LOCATION_NAME__C")
    destination_location = route.get("DESTINATION_LOCATION_NAME__C")
    return {
        "Origin Port": location_to_port_field(origin_location),
        "Origin Postal Code": location_to_postal_field(origin_location),
        "Destination Port": location_to_port_field(destination_location),
        "Destination Postal Code": location_to_postal_field(destination_location),
    }
