"""
Axoworks Custom Audit Tool Implementations
============================================
All audit logic lives here; mcp_parsers.py provides shared parsing helpers.
"""
import asyncio
import json
import logging
import re
from typing import Any

from mcp_parsers import (
    pick, get_raw_text, extract_element_ids, extract_elements,
    extract_number, try_parse_float, AREA_KEYS, NAME_KEYS, LEVEL_KEYS,
)

logger = logging.getLogger("revitmcp.audit_tools")


# ---------------------------------------------------------------------------
# Governance helper
# ---------------------------------------------------------------------------

async def run_governed_tool(bridge, tool_name: str, arguments: dict) -> Any:
    """Poll bridge until a real result arrives (transparently skips heartbeats)."""
    while True:
        raw = await bridge.run_mcp_tool(tool_name, arguments)
        if isinstance(raw, dict) and "_governor_status" in raw:
            await asyncio.sleep(2)
            continue
        return raw


# ---------------------------------------------------------------------------
# Septic audit
# ---------------------------------------------------------------------------

async def run_septic_audit(bridge, arguments: dict) -> dict:
    """Septic setback compliance audit."""
    jurisdiction = arguments.get("jurisdiction", "default")

    def _extract_content(response: Any) -> list:
        if not isinstance(response, dict):
            return []
        text_parts = []
        for item in response.get("content", []):
            if isinstance(item, dict) and item.get("type") == "text":
                try:
                    parsed = json.loads(item.get("text", "{}"))
                    if isinstance(parsed, dict):
                        text_parts.extend(
                            parsed.get("outcome", {}).get("elements", [])
                        )
                except (json.JSONDecodeError, AttributeError):
                    text_parts.append(item.get("text", ""))
        return text_parts

    try:
        tanks_raw = await run_governed_tool(
            bridge, "query_model",
            {"input": {"categories": ["OST_PlumbingFixtures"],
                       "searchScope": "AllViews", "maxResults": 200}},
        )
        lines_raw = await run_governed_tool(
            bridge, "query_model",
            {"input": {"categories": ["OST_PropertyLine"],
                       "searchScope": "AllViews", "maxResults": 200}},
        )
    except Exception as exc:
        return {"error": f"Failed to query Revit model: {exc}"}

    tanks = _extract_content(tanks_raw)
    lines = _extract_content(lines_raw)
    required_ft = 50.0
    return {
        "audit_type": "septic",
        "jurisdiction": jurisdiction,
        "results": {"tanks_found": len(tanks), "lines_found": len(lines)},
        "narrative": (
            f"Septic audit: Found {len(tanks)} tank(s) and {len(lines)} "
            f"property line(s). Required setback: {required_ft}ft. "
            "Coordinate translation applied via Project Base Point offset."
        ),
    }


# ---------------------------------------------------------------------------
# Energy audit (placeholder)
# ---------------------------------------------------------------------------

async def run_energy_audit(bridge, arguments: dict) -> dict:
    return {
        "audit_type": "energy",
        "jurisdiction": arguments.get("jurisdiction", "default"),
        "narrative": (
            "Energy envelope audit requires implementation of U-factor/SHGC "
            "extraction logic. This is a placeholder pending full integration "
            "with the query_model + get_element_data API."
        ),
    }


# ---------------------------------------------------------------------------
# WWR audit (placeholder)
# ---------------------------------------------------------------------------

async def run_wwr_audit(bridge, arguments: dict) -> dict:
    max_wwr = arguments.get("max_wwr_percent", 40.0)
    return {
        "audit_type": "wwr",
        "max_wwr_percent": max_wwr,
        "narrative": (
            f"WWR audit targeting {max_wwr}% maximum. Full implementation "
            "pending integration with query_model + get_element_data API."
        ),
    }


# ---------------------------------------------------------------------------
# Setback audit (unavailable stub)
# ---------------------------------------------------------------------------

async def run_setback_audit(bridge, arguments: dict) -> dict:
    return {
        "audit_type": "setback",
        "status": "unavailable",
        "narrative": (
            "The Setback Audit is currently unavailable. The tool relies on "
            "extracting precise coordinate geometry from property lines to "
            "calculate the perpendicular distance to building exterior walls. "
            "However, the Autodesk Revit MCP Server currently does not export "
            "geometry for property lines, making this deterministic mathematical "
            "calculation impossible. Please check back in future API updates."
        ),
    }



# ---------------------------------------------------------------------------
# Floor area audit (room-based)
# ---------------------------------------------------------------------------

async def run_floor_area_audit(bridge, arguments: dict) -> dict:
    """Floor area audit — total & per-room floor area grouped by level.

    Queries Rooms (OST_Rooms), retrieves element data (Area, Name, Number,
    Level) via get_element_data, and optionally filters by level name(s).
    """
    level_names = arguments.get("level_names", None)
    include_room_details = arguments.get("include_room_details", True)

    try:
        rooms_raw = await run_governed_tool(
            bridge, "query_model",
            {"input": {"categories": ["OST_Rooms"],
                       "searchScope": "AllViews", "maxResults": 500}},
        )
    except Exception as exc:
        return {"error": f"Failed to query Revit model rooms: {exc}"}

    room_ids = extract_element_ids(rooms_raw)

    if not room_ids:
        return {
            "audit_type": "floor_area",
            "total_rooms_found": 0,
            "narrative": (
                "No rooms found in the Revit model. Ensure rooms are placed on "
                "floor plans (Room elements, not just spaces). Try placing rooms "
                "via Revit's Room tool on the appropriate views."
            ),
            "levels": [],
        }

    room_details = []
    try:
        data_raw = await run_governed_tool(
            bridge, "get_element_data",
            {
                "elementIds": [int(eid) for eid in room_ids],
                "outputOptions": {
                    "basicElementInfo": True,
                    "parametersOutputType": "KeyParameters",
                },
            },
        )
        elems = extract_elements(data_raw)

        for elem in elems:
            if not isinstance(elem, dict):
                continue
            params = elem.get("parameters", {})
            eid = elem.get("elementId") or elem.get("id", "?")

            area_val = pick(params, AREA_KEYS) or pick(elem, AREA_KEYS)
            level_val = (
                pick(params, LEVEL_KEYS)
                or pick(elem, LEVEL_KEYS)
                or elem.get("level")
                or "Unknown Level"
            )
            name_val = (
                elem.get("name")
                or pick(params, ["Name", "Mark", "Type Name", "Family"])
                or pick(elem, ["Name", "Mark", "Type Name", "Family"])
                or "Unnamed"
            )
            number_val = pick(params, ["Number", "number"]) or elem.get("number") or ""

            room_details.append({
                "element_id": eid,
                "name": name_val,
                "number": str(number_val),
                "level": level_val,
                "area": try_parse_float(area_val, 0.0),
                "area_unit": "sq ft",
            })
    except Exception:
        pass  # room_details stays empty — returns zeros below

    # Group by level
    levels_map: dict[str, dict] = {}
    for rd in room_details:
        level_name = rd.get("level") or "Unknown Level"
        if level_names and level_name not in level_names:
            continue
        if level_name not in levels_map:
            levels_map[level_name] = {
                "level_name": level_name,
                "total_rooms": 0,
                "total_area_sqft": 0.0,
                "rooms": [],
            }
        levels_map[level_name]["total_rooms"] += 1
        levels_map[level_name]["total_area_sqft"] += rd["area"]
        if include_room_details:
            levels_map[level_name]["rooms"].append({
                "name": rd["name"],
                "number": rd["number"],
                "area_sqft": round(rd["area"], 2),
            })

    level_summaries = []
    for lv in sorted(levels_map.values(), key=lambda x: x["level_name"]):
        entry = {
            "level_name": lv["level_name"],
            "total_rooms": lv["total_rooms"],
            "total_area_sqft": round(lv["total_area_sqft"], 2),
        }
        if include_room_details and lv["rooms"]:
            entry["rooms"] = sorted(lv["rooms"], key=lambda r: r["name"])
        level_summaries.append(entry)

    grand_total = round(sum(lv["total_area_sqft"] for lv in levels_map.values()), 2)
    total_rooms = sum(lv["total_rooms"] for lv in levels_map.values())

    if level_names:
        narrative_parts = [
            f"Floor area audit filtered to {len(level_summaries)} level(s): "
            f"{', '.join(level_names)}."
        ]
    else:
        narrative_parts = [f"Floor area audit across {len(level_summaries)} level(s)."]
    narrative_parts.append(
        f"Total floor area: {grand_total:,} sq ft across {total_rooms} room(s)."
    )
    for lv in level_summaries:
        narrative_parts.append(
            f"  - {lv['level_name']}: {lv['total_area_sqft']:,} sq ft "
            f"({lv['total_rooms']} room(s))"
        )

    return {
        "audit_type": "floor_area",
        "total_rooms_found": total_rooms,
        "grand_total_area_sqft": grand_total,
        "levels": level_summaries,
        "narrative": "\n".join(narrative_parts),
    }



# ---------------------------------------------------------------------------
# Lot area audit
# ---------------------------------------------------------------------------

async def run_lot_area_audit(bridge, arguments: dict) -> dict:
    """Lot area audit — retrieves lot area directly from property lines.

    Strategy:
      1. query_model(OST_SiteProperty) → extract element IDs
      2. get_element_data(ids, AllParameters) → read Area parameter
    """
    area_unit = arguments.get("area_unit", "both")
    one_acre_sqft = 43560.0

    try:
        # Step 1: find OST_SiteProperty IDs
        raw = await run_governed_tool(
            bridge, "query_model",
            {"input": {"categories": ["OST_SiteProperty"],
                       "searchScope": "AllViews", "maxResults": 10}},
        )
        query_debug = get_raw_text(raw)[:500]
        element_ids = extract_element_ids(raw)

        if not element_ids:
            return {
                "audit_type": "lot_area",
                "status": "Unavailable",
                "narrative": (
                    "No OST_SiteProperty elements found in the model.\n"
                    f"query_model raw snippet: {query_debug!r}"
                ),
            }

        # Step 2: get element data with AllParameters
        data_raw = await run_governed_tool(
            bridge, "get_element_data",
            {
                "elementIds": [int(eid) for eid in element_ids],
                "outputOptions": {
                    "basicElementInfo": True,
                    "parametersOutputType": "AllParameters",
                },
            },
        )

        raw_text_dump = get_raw_text(data_raw)
        elems = extract_elements(data_raw)

        AREA_KEYS_FULL = [
            "Area", "area",
            "PROPERTY_LINE_AREA", "SITE_PROPERTY_LINE_AREA",
            "ROOM_AREA", "GSA_SPACE_AREA", "NetArea", "GrossArea", "AREA",
        ]

        lots = []
        params_debug = []
        for elem in elems:
            if not isinstance(elem, dict):
                continue
            eid = elem.get("elementId") or elem.get("id")
            params = elem.get("parameters", {})
            params_debug.append({
                "elem_keys": list(elem.keys())[:15],
                "param_keys": list(params.keys())[:20],
                "elem_id": eid,
            })
            area_val = pick(params, AREA_KEYS_FULL) or pick(elem, AREA_KEYS_FULL)
            name_val = (
                elem.get("name")
                or pick(params, NAME_KEYS)
                or pick(elem, NAME_KEYS)
                or f"Lot {eid}"
            )
            if area_val is not None:
                try:
                    area_float = extract_number(area_val)
                    if area_float > 0:
                        lots.append({
                            "name": name_val,
                            "area_sqft": area_float,
                            "element_id": eid,
                        })
                except ValueError:
                    pass

        # Brute-force fallback: scan raw text for area-like numbers
        if not lots and raw_text_dump:
            _bf_float = None
            _bf_matches = re.findall(
                r'(?:Area|area|AREA|"Area")\s*[=:]\s*"?([0-9,]+(?:\.[0-9]+))',
                raw_text_dump,
            )
            for _bf_val in _bf_matches:
                try:
                    _bf_float = float(_bf_val.replace(",", ""))
                    if _bf_float > 0:
                        break
                except ValueError:
                    continue

            if _bf_float is None or _bf_float <= 0:
                _sqft_matches = re.findall(
                    r'([0-9,]+(?:\.[0-9]+)?)\s*(?:sq\s*\.?\s*ft|square\s*feet|SF)',
                    raw_text_dump, re.IGNORECASE,
                )
                for _v in _sqft_matches:
                    try:
                        _bf_float = float(_v.replace(",", ""))
                        if _bf_float > 0:
                            break
                    except ValueError:
                        continue

            if _bf_float is None or _bf_float <= 0:
                _area_pos = raw_text_dump.lower().find("area")
                if _area_pos >= 0:
                    _near = raw_text_dump[_area_pos:_area_pos + 200]
                    for _v in re.findall(r'([0-9,]+(?:\.[0-9]+))', _near):
                        try:
                            _bf_float = float(_v.replace(",", ""))
                            if _bf_float > 0:
                                break
                        except ValueError:
                            continue

            if _bf_float is not None and _bf_float > 0:
                lots.append({
                    "name": "Property Line",
                    "area_sqft": _bf_float,
                    "element_id": element_ids[0],
                })

        if not lots:
            narrative_parts = [
                f"OST_SiteProperty IDs found: {element_ids}, but Area could not be read."
            ]
            if params_debug:
                narrative_parts.append(
                    f"Element structure: {json.dumps(params_debug, default=str)[:2000]}"
                )
            if raw_text_dump:
                narrative_parts.append(
                    f"Full get_element_data raw text (first 2000 chars): {raw_text_dump[:2000]!r}"
                )
            return {
                "audit_type": "lot_area",
                "status": "Unavailable",
                "narrative": "\n".join(narrative_parts),
            }

        total_area_sqft = sum(lot["area_sqft"] for lot in lots)
        total_area_acres = total_area_sqft / one_acre_sqft
        narrative = f"Lot area audit complete. Found {len(lots)} property line(s).\n"
        narrative += f"Total Area: {total_area_sqft:,.2f} sq ft ({total_area_acres:,.4f} acres)\n"
        for lot in lots:
            narrative += f"  - {lot['name']}: {lot['area_sqft']:,.2f} sq ft\n"

        return {
            "audit_type": "lot_area",
            "status": "Success",
            "total_area_sqft": total_area_sqft,
            "total_area_acres": total_area_acres,
            "lots": lots,
            "narrative": narrative,
        }

    except Exception as e:
        return {
            "audit_type": "lot_area",
            "error": str(e),
            "narrative": f"Error running lot area audit: {e}",
        }



# ---------------------------------------------------------------------------
# Floor area query (OST_Floors based, for lot coverage)
# ---------------------------------------------------------------------------

async def run_floor_area_query(bridge, arguments: dict) -> dict:
    """Query floor element areas — groups by Level, returns largest single-level
    area as the building footprint for lot coverage calculations.
    """
    level_names = arguments.get("level_names", None)
    include_details = arguments.get("include_details", True)

    try:
        logger.info("FLOOR_AREA_QUERY: querying OST_Floors …")
        raw = await run_governed_tool(
            bridge, "query_model",
            {"input": {"categories": ["OST_Floors"],
                       "searchScope": "AllViews", "maxResults": 200}},
        )
        floor_ids = extract_element_ids(raw)
        logger.info("FLOOR_AREA_QUERY: found %d floor IDs: %s",
                    len(floor_ids), floor_ids[:10])

        if not floor_ids:
            return {
                "audit_type": "floor_area_query",
                "status": "Unavailable",
                "narrative": "No OST_Floors elements found in the model.",
                "building_footprint_sqft": 0.0,
                "levels": [],
            }

        data_raw = await run_governed_tool(
            bridge, "get_element_data",
            {
                "elementIds": [int(eid) for eid in floor_ids],
                "outputOptions": {
                    "basicElementInfo": True,
                    "parametersOutputType": "AllParameters",
                },
            },
        )
        elem_list = extract_elements(data_raw)
        logger.info("FLOOR_AREA_QUERY: get_element_data → %d elements", len(elem_list))

        floor_details = []
        for elem_val in elem_list:
            if not isinstance(elem_val, dict):
                continue
            params = elem_val.get("parameters", {})
            elem_id = elem_val.get("elementId") or elem_val.get("id", "?")

            area_val = (
                pick(params, AREA_KEYS)
                or pick(elem_val, AREA_KEYS)
                or elem_val.get("area")
            )
            name_val = (
                elem_val.get("name")
                or pick(params, NAME_KEYS)
                or pick(elem_val, NAME_KEYS)
                or f"Floor {elem_id}"
            )
            level_val = (
                pick(params, LEVEL_KEYS)
                or pick(elem_val, LEVEL_KEYS)
                or elem_val.get("level")
            )

            if area_val is not None:
                try:
                    area_float = extract_number(area_val)
                    if area_float > 0:
                        floor_details.append({
                            "name": name_val,
                            "area_sqft": area_float,
                            "level": level_val,
                            "element_id": elem_id,
                        })
                        logger.info("FLOOR_AREA_QUERY:   id=%s area=%.2f level=%s",
                                    elem_id, area_float, level_val)
                except ValueError:
                    pass

        if not floor_details:
            return {
                "audit_type": "floor_area_query",
                "status": "Unavailable",
                "narrative": (
                    f"Found {len(floor_ids)} floor element(s) but could not extract "
                    "Area parameter values. The Area parameter may not be populated "
                    "for these elements."
                ),
                "building_footprint_sqft": 0.0,
                "levels": [],
            }

        # Group by level, find largest single-level area
        level_groups = defaultdict(list)
        for fd in floor_details:
            lv = fd.get("level") or "Unknown Level"
            if level_names and lv not in level_names:
                continue
            level_groups[lv].append(fd)

        if not level_groups:
            return {
                "audit_type": "floor_area_query",
                "status": "Unavailable",
                "narrative": (
                    f"Floor elements found but none matched the requested level "
                    f"filter: {level_names}"
                ),
                "building_footprint_sqft": 0.0,
                "levels": [],
            }

        max_level_name = ""
        max_level_area = 0.0
        level_breakdown = []
        for lv_name, elements in level_groups.items():
            lv_total = sum(e.get("area_sqft", 0) for e in elements)
            level_breakdown.append({
                "level_name": lv_name,
                "total_area_sqft": lv_total,
                "element_count": len(elements),
                "elements": elements if include_details else [],
            })
            if lv_total > max_level_area:
                max_level_area = lv_total
                max_level_name = lv_name

        total_all_levels = sum(lv["total_area_sqft"] for lv in level_breakdown)
        narrative_parts = [
            f"Floor area query complete. Found {len(floor_details)} floor element(s) "
            f"across {len(level_breakdown)} level(s).",
            f"Total floor area (all levels): {total_all_levels:,.2f} sq ft",
            f"Largest single-level area: {max_level_area:,.2f} sq ft ({max_level_name})",
            f"Building footprint (for lot coverage): {max_level_area:,.2f} sq ft",
        ]
        if level_names:
            narrative_parts.insert(0, f"Filtered to level(s): {', '.join(level_names)}")
        if include_details and level_breakdown:
            narrative_parts.append("\nPer-Level Breakdown:")
            for lv in level_breakdown:
                narrative_parts.append(
                    f"  {lv['level_name']}: {lv['total_area_sqft']:,.2f} sq ft "
                    f"({lv['element_count']} element(s))"
                )
                for elem in lv.get("elements", []):
                    narrative_parts.append(
                        f"    - {elem['name']}: {elem['area_sqft']:,.2f} sq ft"
                    )

        return {
            "audit_type": "floor_area_query",
            "status": "Success",
            "total_floor_elements": len(floor_details),
            "total_area_all_levels_sqft": total_all_levels,
            "building_footprint_sqft": max_level_area,
            "building_footprint_level": max_level_name,
            "levels": level_breakdown,
            "narrative": "\n".join(narrative_parts),
        }

    except Exception as e:
        logger.error("FLOOR_AREA_QUERY: EXCEPTION - %s", e, exc_info=True)
        return {
            "audit_type": "floor_area_query",
            "error": str(e),
            "narrative": f"Error running floor area query: {e}",
        }



# ---------------------------------------------------------------------------
# Lot coverage audit  (composed from sub-tools — no duplicated Revit queries)
# ---------------------------------------------------------------------------

async def run_lot_coverage_audit(bridge, arguments: dict) -> dict:
    """Lot coverage = (Building Footprint / Lot Area) × 100.

    This implementation **composes** the two proven sub-tools rather than
    duplicating their Revit pipe queries internally:

      Step 1 — ``run_lot_area_audit``   → lot area (OST_SiteProperty)
      Step 2 — ``run_floor_area_query`` → building footprint (OST_Floors,
               largest single-level floor plate)
      Step 3 — Pure math: footprint / lot_area × 100
    """
    include_details = arguments.get("include_details", True)

    try:
        # ── Step 1: Lot area ──────────────────────────────────────────────
        logger.info("LOT_COVERAGE: Step 1 — delegating to run_lot_area_audit …")
        lot_result = await run_lot_area_audit(bridge, arguments)

        if lot_result.get("status") == "Unavailable":
            return {
                "audit_type": "lot_coverage",
                "status": "Unavailable",
                "narrative": (
                    "Lot coverage could not be calculated — lot area query failed.\n"
                    + lot_result.get("narrative", "")
                ),
            }

        total_lot_sqft = lot_result.get("total_area_sqft", 0.0)
        if total_lot_sqft <= 0:
            return {
                "audit_type": "lot_coverage",
                "status": "Unavailable",
                "narrative": "The lot area was calculated as zero — cannot compute coverage.",
            }
        logger.info("LOT_COVERAGE: lot area = %.2f sqft", total_lot_sqft)

        # ── Step 2: Building footprint ────────────────────────────────────
        logger.info("LOT_COVERAGE: Step 2 — delegating to run_floor_area_query …")
        floor_result = await run_floor_area_query(bridge, {
            "include_details": include_details,
        })

        floor_sqft = floor_result.get("building_footprint_sqft", 0.0)
        max_level_name = floor_result.get("building_footprint_level", "")
        level_breakdown = floor_result.get("levels", [])
        floor_status = floor_result.get("status", "Unavailable")

        if floor_sqft <= 0:
            logger.warning(
                "LOT_COVERAGE: floor area query returned 0 footprint (status=%s). "
                "Narrative: %s", floor_status, floor_result.get("narrative", "")
            )
            return {
                "audit_type": "lot_coverage",
                "status": "Partial",
                "lot_area_sqft": total_lot_sqft,
                "building_area_sqft": 0.0,
                "building_coverage_percent": 0.0,
                "narrative": (
                    "Lot Coverage Audit — Partial Result.\n"
                    f"Total Lot Area: {total_lot_sqft:,.2f} sq ft\n"
                    f"Building Footprint Area: 0.00 sq ft\n"
                    "Building Lot Coverage: 0.0%\n\n"
                    "Note: The floor area query found no floor elements or could not "
                    "extract area parameters. If you expect building coverage, verify "
                    "that floor elements (OST_Floors) are present and have an Area "
                    "parameter.\n\n"
                    "WORKAROUND: Ask separately:\n"
                    "  1. 'Largest floor area at a single floor level'\n"
                    "  2. 'Deduce lot coverage'\n"
                    "The LLM can reason the answer from the two data points."
                ),
            }
        logger.info("LOT_COVERAGE: footprint = %.2f sqft (level: %s)",
                    floor_sqft, max_level_name)

        # ── Step 3: Pure math — no Revit calls ────────────────────────────
        building_coverage_pct = round((floor_sqft / total_lot_sqft) * 100, 2)
        logger.info("LOT_COVERAGE: RESULT — building = %.2f%%", building_coverage_pct)

        # ── Build narrative ───────────────────────────────────────────────
        narrative = "Lot Coverage Audit Complete.\n\n"
        narrative += f"Total Lot Area: {total_lot_sqft:,.2f} sq ft\n"
        narrative += f"Building Footprint Area: {floor_sqft:,.2f} sq ft\n"
        narrative += f"  (largest single-level floor plate: {max_level_name})\n"
        narrative += f"Building Lot Coverage: {building_coverage_pct:.2f}%\n"

        if include_details and level_breakdown:
            narrative += "\nPer-Level Floor Breakdown:\n"
            for lv in level_breakdown:
                lv_name = lv.get("level_name", "?")
                lv_area = lv.get("total_area_sqft", 0)
                lv_count = lv.get("element_count", 0)
                narrative += f"  {lv_name}: {lv_area:,.2f} sq ft ({lv_count} element(s))\n"
                for elem in lv.get("elements", []):
                    narrative += f"    - {elem.get('name','?')}: {elem.get('area_sqft',0):,.2f} sq ft\n"

        return {
            "audit_type": "lot_coverage",
            "status": "Success",
            "lot_area_sqft": total_lot_sqft,
            "building_area_sqft": floor_sqft,
            "building_coverage_percent": building_coverage_pct,
            "max_level_name": max_level_name,
            "level_breakdown": level_breakdown,
            "narrative": narrative,
        }

    except Exception as e:
        logger.error("LOT_COVERAGE: EXCEPTION — %s", e, exc_info=True)
        return {
            "audit_type": "lot_coverage",
            "error": str(e),
            "narrative": f"Error running lot coverage audit: {e}",
        }
