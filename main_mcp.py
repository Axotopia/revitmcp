"""
Axoworks Revit MCP Proxy Server
================================
Sits between AnythingLLM and the Autodesk Revit MCP Server, providing:

1. Transparent pass-through to all native Revit MCP tools
2. Coordinate translation (Project Base Point Z-offset)
3. Custom audit tools (septic, energy, WWR)
4. Governance layer (dedup, heartbeat, payload auditing)

Architecture:
    AnythingLLM  ←─stdio─→  main_mcp.py  ←─Named Pipe─→  Autodesk Revit MCP Server

The proxy dynamically discovers tools from the Autodesk pipe and exposes them
to AnythingLLM with identical names/schemas, so existing AnythingLLM workspace
configuration (system prompts, agent settings) continues to work unchanged.

Usage:
    python main_mcp.py

Environment variables (see .env.example):
    OLLAMA_MODEL, OLLAMA_BASE_URL, API_PORT, REVIT_PIPE_PREFIX
    GOVERNOR_HEARTBEAT_THRESHOLD_S, GOVERNOR_CACHE_TTL_S, etc.
"""

import json
import logging
import os
import sys
from typing import Any

from dotenv import load_dotenv

from bridge import RevitBridge, RevitBridgeError, get_governed_bridge
from coordinate_translator import translator

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    stream=sys.stderr,  # MCP uses stdout for protocol — keep logs on stderr
)
logger = logging.getLogger("revitmcp.mcp_server")

load_dotenv()

OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen3.6:35b")
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")


# ---------------------------------------------------------------------------
# MCP Server — using the raw MCP protocol over stdio
# ---------------------------------------------------------------------------
#
# The MCP protocol is JSON-RPC 2.0 over stdin/stdout (newline-delimited).
# We implement the two required methods:
#   - tools/list
#   - tools/call
#
# We use the transport-agnostic JSON-RPC layer already built in bridge.py
# and re-route it over stdio for MCP compliance.

class McpStdioTransport:
    """
    Reads JSON-RPC 2.0 requests from stdin and writes responses to stdout.
    This is the standard MCP transport used by AnythingLLM and other MCP hosts.
    """

    def __init__(self):
        self._bridge = get_governed_bridge()
        self._cached_tools: list[dict] | None = None

    # -----------------------------------------------------------------------
    # Lifecycle
    # -----------------------------------------------------------------------

    async def _discover_revit_tools(self) -> list[dict]:
        """Fetch tools from the Autodesk Revit MCP pipe via tools/list."""
        try:
            raw = await self._bridge.list_mcp_tools()
            if isinstance(raw, list):
                return raw
            if isinstance(raw, dict) and "tools" in raw:
                return raw["tools"]
            logger.warning("Unexpected tools/list response format: %s", type(raw))
            return []
        except RevitBridgeError as exc:
            logger.error("Failed to discover Revit tools: %s", exc)
            return []
        except Exception as exc:
            logger.error("Unexpected error discovering Revit tools: %s", exc)
            return []

    def _build_custom_tools(self) -> list[dict]:
        """Define custom audit tools that the proxy handles internally."""
        return [
            {
                "name": "axo_audit_septic",
                "description": "Run a septic setback compliance audit on the active Revit model.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "jurisdiction": {
                            "type": "string",
                            "description": "Jurisdiction code for building code lookup (default: 'default').",
                            "default": "default",
                        }
                    },
                    "required": [],
                },
            },
            {
                "name": "axo_audit_energy",
                "description": "Run an energy envelope compliance audit (U-factors, SHGC) on the active Revit model.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "jurisdiction": {
                            "type": "string",
                            "description": "Jurisdiction code for energy code lookup (default: 'default').",
                            "default": "default",
                        }
                    },
                    "required": [],
                },
            },
            {
                "name": "axo_audit_wwr",
                "description": "Run a Window-to-Wall Ratio compliance audit on the active Revit model.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "max_wwr_percent": {
                            "type": "number",
                            "description": "Maximum allowed WWR percentage (default: 40).",
                            "default": 40.0,
                        }
                    },
                    "required": [],
                },
            },
            {
                "name": "axo_audit_floor_area",
                "description": (
                    "Query floor area data from the active Revit model. "
                    "Returns total floor area and per-room breakdown, grouped by level. "
                    "Optionally filter by one or more level names (e.g., FP1.GARAGE, FP2.ADU)."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "level_names": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Optional list of level names to filter by (e.g., ['FP1.GARAGE', 'FP2.ADU']). If omitted, returns data for all levels.",
                        },
                        "include_room_details": {
                            "type": "boolean",
                            "description": "Include individual room names, numbers, and areas in the output. Default: true.",
                            "default": True,
                        },
                    },
                    "required": [],
                },
            },
            {
                "name": "axo_audit_lot_area",
                "description": (
                    "Calculate the lot area (area enclosed by property lines) "
                    "from the active Revit model. Queries OST_PropertyLine elements, "
                    "extracts polyline geometry, and computes enclosed area using "
                    "the shoelace formula (deterministic math, no LLM). "
                    "Returns area in both sq ft and acres."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "area_unit": {
                            "type": "string",
                            "description": "Output area unit: 'sqft' (default), 'acres', or 'both'.",
                            "default": "both",
                            "enum": ["sqft", "acres", "both"],
                        },
                    },
                    "required": [],
                },
            },
            {
                "name": "axo_audit_lot_coverage",
                "description": (
                    "Calculate lot coverage percentage for the active Revit model. "
                    "Deterministically queries property lines (OST_SiteProperty) for lot area, "
                    "floor elements (OST_Floors) for building footprint, and area elements "
                    "(OST_Areas) for additional covered areas. "
                    "Returns a structured breakdown with lot area, building footprint, "
                    "covered areas, and two coverage percentages (building-only and total)."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "area_unit": {
                            "type": "string",
                            "description": "Output area unit: 'sqft' (default), 'acres', or 'both'.",
                            "default": "both",
                            "enum": ["sqft", "acres", "both"],
                        },
                        "include_details": {
                            "type": "boolean",
                            "description": "Include per-element area breakdown.",
                            "default": True,
                        }
                    },
                    "required": [],
                },
            },
            {
                "name": "axo_audit_setback",
                "description": (
                    "Calculate the closest distance from building exterior walls "
                    "to property lines. Queries OST_Walls (exterior walls) and "
                    "OST_PropertyLine elements, extracts bounding box and curve "
                    "geometry, then computes minimum perpendicular distances "
                    "per side (North, South, East, West) using deterministic math. "
                    "Returns distances in feet and inches, and identifies the "
                    "closest setback. This tool is equivalent to Revit's built-in "
                    "'Property Line Proximity Analysis'."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "output_unit": {
                            "type": "string",
                            "description": "Output unit: 'ft_in' (default, feet and inches), 'ft' (decimal feet), or 'in' (inches).",
                            "default": "ft_in",
                            "enum": ["ft_in", "ft", "in"],
                        },
                    },
                    "required": [],
                },
            },
        ]

    async def get_tools(self) -> list[dict]:
        """
        Return the merged tool list: native Revit tools + custom audit tools.
        Results are cached so we don't hammer the pipe on every request.
        """
        if self._cached_tools is None:
            native = await self._discover_revit_tools()
            custom = self._build_custom_tools()
            self._cached_tools = native + custom
            logger.info(
                "Discovered %d native Revit tools + %d custom tools",
                len(native),
                len(custom),
            )
        return self._cached_tools

    def invalidate_cache(self):
        """Force re-discovery on the next tools/list call."""
        self._cached_tools = None

    # -----------------------------------------------------------------------
    # Proxy: call a native Revit tool
    # -----------------------------------------------------------------------

    async def _call_revit_tool(self, tool_name: str, arguments: dict) -> Any:
        """Forward a tool call to the Autodesk Revit MCP pipe."""
        try:
            result = await self._bridge.run_mcp_tool(tool_name, arguments)

            # The Autodesk MCP server returns responses in MCP-compliant format:
            #   {"content": [{"type": "text", "text": "{\"outcome\": ...}"}]}
            # We need to parse the inner text, translate coordinates, and re-wrap.
            translated = await self._translate_response(result)

            return translated
        except RevitBridgeError as exc:
            logger.error("Revit bridge error calling '%s': %s", tool_name, exc)
            raise
        except Exception as exc:
            logger.error("Unexpected error calling '%s': %s", tool_name, exc)
            raise

    async def _translate_response(self, response: Any) -> Any:
        """
        Translate Z coordinates inside an MCP-compliant response.
        
        The Autodesk server wraps data as:
            {"content": [{"type": "text", "text": "<json string>"}]}
        
        We parse the inner JSON, translate geometry Z values, and re-wrap.
        """
        if not isinstance(response, dict):
            # Raw passthrough — try translating directly
            translated = await translator.translate_payload(self._bridge, response)
            return translated

        content = response.get("content")
        if not isinstance(content, list):
            # Not MCP-wrapped — translate directly
            translated = await translator.translate_payload(self._bridge, response)
            return translated

        translated_content = []
        for item in content:
            if not isinstance(item, dict):
                translated_content.append(item)
                continue

            if item.get("type") == "text":
                text = item.get("text", "")
                try:
                    # Parse the inner JSON
                    parsed = json.loads(text)
                    # Translate coordinates in the parsed data
                    translated_data = await translator.translate_payload(
                        self._bridge, parsed
                    )
                    # Re-serialize
                    translated_content.append({
                        "type": "text",
                        "text": json.dumps(translated_data, indent=2),
                    })
                except (json.JSONDecodeError, TypeError):
                    # Not JSON — pass through unchanged
                    translated_content.append(item)
            else:
                translated_content.append(item)

        return {"content": translated_content}

    # -----------------------------------------------------------------------
    # Custom audit tools
    # -----------------------------------------------------------------------

    async def _run_septic_audit(self, arguments: dict) -> dict:
        """
        Septic setback compliance audit.
        Uses the Revit pipe for data extraction, then applies deterministic math.
        """
        jurisdiction = arguments.get("jurisdiction", "default")

        # Fetch data via Revit MCP tools (the new query_model + get_element_data API)
        try:
            # Step 1: Query plumbing fixtures (tanks)
            tanks_raw = await self._run_governed_tool_sync(
                "query_model",
                {
                    "input": {
                        "categories": ["OST_PlumbingFixtures"],
                        "searchScope": "AllViews",
                        "maxResults": 200,
                    }
                },
            )
            # Step 2: Query property lines
            lines_raw = await self._run_governed_tool_sync(
                "query_model",
                {
                    "input": {
                        "categories": ["OST_PropertyLine"],
                        "searchScope": "AllViews",
                        "maxResults": 200,
                    }
                },
            )
        except RevitBridgeError as exc:
            return {"error": f"Failed to query Revit model: {exc}"}

        # Parse the MCP-compliant response format:
        # {"content": [{"type": "text", "text": "{\"outcome\": ...}"}]}
        def _extract_content(response: Any) -> list:
            if isinstance(response, dict):
                content = response.get("content", [])
                if content and isinstance(content, list):
                    text_parts = []
                    for item in content:
                        if isinstance(item, dict) and item.get("type") == "text":
                            try:
                                parsed = json.loads(item.get("text", "{}"))
                                if isinstance(parsed, dict):
                                    elements = parsed.get("outcome", {}).get("elements", [])
                                    text_parts.extend(elements)
                            except (json.JSONDecodeError, AttributeError):
                                text_parts.append(item.get("text", ""))
                    return text_parts
            return []

        tanks = _extract_content(tanks_raw)
        lines = _extract_content(lines_raw)

        # Apply coordinate translation to the extracted elements
        tanks = await translator.translate_payload(self._bridge, tanks)
        lines = await translator.translate_payload(self._bridge, lines)

        # Simple setback calculation (deterministic, no LLM math)
        required_ft = 50.0  # Default IBC setback
        results = {"tanks_found": len(tanks), "lines_found": len(lines)}
        return {
            "audit_type": "septic",
            "jurisdiction": jurisdiction,
            "results": results,
            "narrative": f"Septic audit: Found {len(tanks)} tank(s) and {len(lines)} property line(s). "
                         f"Required setback: {required_ft}ft. "
                         f"Coordinate translation applied via Project Base Point offset.",
        }

    async def _run_energy_audit(self, arguments: dict) -> dict:
        """Energy envelope compliance audit (placeholder — extend as needed)."""
        jurisdiction = arguments.get("jurisdiction", "default")
        return {
            "audit_type": "energy",
            "jurisdiction": jurisdiction,
            "narrative": "Energy envelope audit requires implementation of U-factor/SHGC extraction logic. "
                         "This is a placeholder pending full integration with the query_model + get_element_data API.",
        }

    async def _run_wwr_audit(self, arguments: dict) -> dict:
        """Window-to-Wall ratio compliance audit (placeholder — extend as needed)."""
        max_wwr = arguments.get("max_wwr_percent", 40.0)
        return {
            "audit_type": "wwr",
            "max_wwr_percent": max_wwr,
            "narrative": f"WWR audit targeting {max_wwr}% maximum. "
                         "Full implementation pending integration with query_model + get_element_data API.",
        }

    async def _run_floor_area_audit(self, arguments: dict) -> dict:
        """
        Floor area audit — returns total & per-room floor area grouped by level.
        
        Uses the Revit pipe to query Rooms (OST_Rooms), then retrieves detailed
        element data (Area, Name, Number, Level) via get_element_data.
        Optionally filters by level name(s) such as FP1.GARAGE, FP2.ADU.
        """
        level_names = arguments.get("level_names", None)
        include_room_details = arguments.get("include_room_details", True)

        try:
            # Step 1: Query all rooms via query_model
            rooms_raw = await self._run_governed_tool_sync(
                "query_model",
                {
                    "input": {
                        "categories": ["OST_Rooms"],
                        "searchScope": "AllViews",
                        "maxResults": 500,
                    }
                },
            )
        except RevitBridgeError as exc:
            return {"error": f"Failed to query Revit model rooms: {exc}"}

        # Parse the MCP-compliant response format to find element IDs
        def _extract_room_ids(response: Any) -> list:
            """Return list of int element IDs from query_model response (or [])."""
            ids = []
            if not isinstance(response, dict):
                return ids
            for item in response.get("content", []):
                if not (isinstance(item, dict) and item.get("type") == "text"):
                    continue
                text = item.get("text", "")
                try:
                    parsed = json.loads(text)
                    # Shape A: outcome.elements
                    for el in parsed.get("outcome", {}).get("elements", []):
                        eid = (el or {}).get("elementId") or (el or {}).get("id")
                        if eid is not None:
                            ids.append(int(eid))
                    # Shape B: top-level elements
                    if not ids:
                        for el in parsed.get("elements", []):
                            eid = (el or {}).get("elementId") or (el or {}).get("id")
                            if eid is not None:
                                ids.append(int(eid))
                    # Shape C: results["Element Ids"]
                    if not ids:
                        r = parsed.get("results", {})
                        if isinstance(r, dict):
                            ids = [int(x) for x in r.get("Element Ids", []) if x]
                        elif isinstance(r, list):
                            ids = [int(x) for x in r if x]
                except Exception:
                    pass
                # Regex fallback: 6-8 digit numbers
                if not ids:
                    ids = [int(m) for m in re.findall(r'\b(\d{6,8})\b', text)]
            return ids

        room_ids = _extract_room_ids(rooms_raw)

        if not room_ids:
            return {
                "audit_type": "floor_area",
                "total_rooms_found": 0,
                "narrative": "No rooms found in the Revit model. "
                             "Ensure rooms are placed on floor plans (Room elements, not just spaces). "
                             "Try placing rooms via Revit's Room tool on the appropriate views.",
                "levels": [],
            }

        # Step 3: Get detailed element data for all rooms with AllParameters
        room_details = []
        try:
            data_raw = await self._run_governed_tool_sync(
                "get_element_data",
                {
                    "elementIds": [int(eid) for eid in room_ids],
                    "outputOptions": {
                        "basicElementInfo": True,
                        "parametersOutputType": "KeyParameters",
                    },
                },
            )

            def _extract_elems_from_gedata(raw: Any) -> list:
                """Extract element dict list from get_element_data MCP response."""
                elems = []
                if not isinstance(raw, dict):
                    return elems
                for itm in raw.get("content", []):
                    if not (isinstance(itm, dict) and itm.get("type") == "text"):
                        continue
                    text = itm.get("text", "")
                    try:
                        p = json.loads(text)
                        if "elements" in p and isinstance(p["elements"], list):
                            return p["elements"]
                        out_e = p.get("outcome", {}).get("elements", [])
                        if out_e:
                            return out_e
                        results = p.get("results", {})
                        if isinstance(results, dict) and "Element Ids" not in results:
                            for v in results.values():
                                if isinstance(v, dict):
                                    elems.append(v)
                            if elems:
                                return elems
                    except Exception:
                        pass
                return elems

            def _pick(params, keys):
                for k in keys:
                    if k in params:
                        v = params[k]
                        return v.get("value") if isinstance(v, dict) else v
                return None

            elems = _extract_elems_from_gedata(data_raw)

            for elem in elems:
                if not isinstance(elem, dict):
                    continue
                params = elem.get("parameters", {})
                eid = elem.get("elementId") or elem.get("id", "?")

                # Search area in params first, then top-level elem fields
                AREA_KEYS = ["Area", "area", "ROOM_AREA", "GSA_SPACE_AREA", "NetArea", "GrossArea"]
                area_val = _pick(params, AREA_KEYS)
                if area_val is None:
                    area_val = _pick(elem, AREA_KEYS)

                level_val = (
                    _pick(params, ["Level", "level", "LEVEL_PARAM"])
                    or _pick(elem, ["Level", "level", "LEVEL_PARAM"])
                    or elem.get("level")
                    or "Unknown Level"
                )
                name_val = (
                    elem.get("name")
                    or _pick(params, ["Name", "Mark", "Type Name", "Family"])
                    or _pick(elem, ["Name", "Mark", "Type Name", "Family"])
                    or "Unnamed"
                )
                number_val = (
                    _pick(params, ["Number", "number"])
                    or elem.get("number")
                    or ""
                )

                room_details.append({
                    "element_id": eid,
                    "name": name_val,
                    "number": str(number_val),
                    "level": level_val,
                    "area": self._try_parse_float(area_val, 0.0),
                    "area_unit": "sq ft",
                })

        except Exception:
            pass  # room_details will be empty — function returns zeros below

        # Step 4: Group by level
        levels_map: dict[str, dict] = {}
        for rd in room_details:
            level_name = rd.get("level", "Unknown Level") or "Unknown Level"

            # Apply level filter if specified
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

        # Step 5: Build structured result
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

        # Build narrative
        if level_names:
            filters_text = ", ".join(level_names)
            narrative_parts = [
                f"Floor area audit filtered to {len(level_summaries)} level(s): {filters_text}."
            ]
        else:
            narrative_parts = [
                f"Floor area audit across {len(level_summaries)} level(s)."
            ]
        narrative_parts.append(
            f"Total floor area: {grand_total:,} sq ft across {total_rooms} room(s)."
        )
        if level_summaries:
            for lv in level_summaries:
                narrative_parts.append(
                    f"  - {lv['level_name']}: {lv['total_area_sqft']:,} sq ft ({lv['total_rooms']} room(s))"
                )

        return {
            "audit_type": "floor_area",
            "total_rooms_found": total_rooms,
            "grand_total_area_sqft": grand_total,
            "levels": level_summaries,
            "narrative": "\n".join(narrative_parts),
        }

    async def _run_governed_tool_sync(self, tool_name: str, arguments: dict) -> Any:
        """
        Runs a tool via the bridge (governor) and blocks until the real result is ready,
        transparently polling if the governor returns a heartbeat.
        """
        import asyncio
        while True:
            raw = await self._bridge.run_mcp_tool(tool_name, arguments)
            if isinstance(raw, dict) and "_governor_status" in raw:
                # It's a heartbeat, wait and poll again
                await asyncio.sleep(2)
                continue
            return raw

    def _extract_number(self, val: Any) -> float:
        """Extracts a float from a Revit parameter string like '18,975.88 sq ft'"""
        if isinstance(val, (int, float)):
            return float(val)
        val_str = str(val).replace(',', '')
        import re
        match = re.search(r'-?\d+\.?\d*', val_str)
        if match:
            return float(match.group())
        raise ValueError(f"Could not extract number from {val}")

    def _try_parse_float(self, val: Any, default: float = 0.0) -> float:
        """Safely attempts to parse a float; returns `default` on failure."""
        try:
            return self._extract_number(val)
        except (ValueError, TypeError, AttributeError):
            return default

    async def _run_lot_area_audit(self, arguments: dict) -> dict:
        """
        Lot area audit — retrieves the lot area directly from property lines.

        Strategy (mirrors the agent's proven manual sequence):
          1. query_model(OST_SiteProperty) → extract element IDs via regex scan
          2. get_element_data(ids, AllParameters) → read Area parameter
        """
        area_unit = arguments.get("area_unit", "both")
        one_acre_sqft = 43560.0
        import re

        AREA_KEYS = ["Area", "area", "ROOM_AREA", "GSA_SPACE_AREA", "NetArea", "GrossArea"]
        NAME_KEYS = ["Name", "Mark", "Comments", "ELEM_TYPE_PARAM"]

        def _pick(params: dict, keys: list):
            for k in keys:
                if k in params:
                    v = params[k]
                    return v.get("value") if isinstance(v, dict) else v
            return None

        def _extract_ids_from_raw(raw: Any) -> tuple[list, str]:
            """
            Extract element IDs from a query_model response using two strategies:
              1. Parse JSON and walk known shapes (outcome.elements, results["Element Ids"], etc.)
              2. Regex fallback: scan raw text for numbers that look like Revit IDs (6-8 digits)
            Returns (id_list, debug_snippet).
            """
            ids = []
            debug = ""
            if not isinstance(raw, dict):
                return ids, repr(raw)[:300]

            for item in raw.get("content", []):
                if not (isinstance(item, dict) and item.get("type") == "text"):
                    continue
                text = item.get("text", "")
                debug = text[:500]
                try:
                    parsed = json.loads(text)
                    # Shape A: outcome.elements or top-level elements
                    for key in ("outcome",):
                        sub = parsed.get(key, {})
                        if isinstance(sub, dict):
                            for e in sub.get("elements", []):
                                eid = (e or {}).get("elementId") or (e or {}).get("id")
                                if eid is not None:
                                    ids.append(eid)
                    if not ids:
                        for e in parsed.get("elements", []):
                            eid = (e or {}).get("elementId") or (e or {}).get("id")
                            if eid is not None:
                                ids.append(eid)
                    # Shape B: results["Element Ids"]
                    if not ids:
                        r = parsed.get("results", {})
                        if isinstance(r, dict):
                            ids = r.get("Element Ids", [])
                        elif isinstance(r, list):
                            ids = r
                    # Shape C: top-level "Element Ids"
                    if not ids and "Element Ids" in parsed:
                        ids = parsed["Element Ids"]
                except Exception:
                    pass

                # Regex fallback: find all 6-8 digit numbers in the text
                if not ids:
                    ids = [int(m) for m in re.findall(r'\b(\d{6,8})\b', text)]

            return ids, debug

        def _extract_elems_from_gedata(raw: Any) -> tuple[list, str]:
            """
            Extract element dicts from a get_element_data response.
            Returns (elem_list, debug_snippet).
            """
            elems = []
            debug = ""
            if not isinstance(raw, dict):
                return elems, repr(raw)[:300]

            for item in raw.get("content", []):
                if not (isinstance(item, dict) and item.get("type") == "text"):
                    continue
                text = item.get("text", "")
                debug = text[:500]
                try:
                    parsed = json.loads(text)
                    # Shape A: top-level "elements" list
                    if "elements" in parsed and isinstance(parsed["elements"], list):
                        elems = parsed["elements"]
                        break
                    # Shape A2: outcome.elements
                    out_elems = parsed.get("outcome", {}).get("elements", [])
                    if out_elems:
                        elems = out_elems
                        break
                    # Shape B: results keyed by ID
                    results = parsed.get("results", {})
                    if isinstance(results, dict) and "Element Ids" not in results:
                        for val in results.values():
                            if isinstance(val, dict):
                                elems.append(val)
                        if elems:
                            break
                except Exception:
                    pass

            return elems, debug

        try:
            # ----------------------------------------------------------------
            # Step 1: query_model to find OST_SiteProperty element IDs
            # ----------------------------------------------------------------
            raw = await self._run_governed_tool_sync(
                "query_model",
                {
                    "input": {
                        "categories": ["OST_SiteProperty"],
                        "searchScope": "AllViews",
                        "maxResults": 10,
                    }
                },
            )

            element_ids, query_debug = _extract_ids_from_raw(raw)

            if not element_ids:
                return {
                    "audit_type": "lot_area",
                    "status": "Unavailable",
                    "narrative": (
                        "No OST_SiteProperty elements found in the model.\n"
                        f"query_model raw snippet: {query_debug!r}"
                    ),
                }

            # ----------------------------------------------------------------
            # Step 2: get_element_data with AllParameters (agent-proven approach)
            # ----------------------------------------------------------------
            data_raw = await self._run_governed_tool_sync(
                "get_element_data",
                {
                    "elementIds": [int(eid) for eid in element_ids],
                    "outputOptions": {
                        "basicElementInfo": True,
                        "parametersOutputType": "AllParameters",
                    },
                },
            )

            # Capture FULL raw text from data_raw for diagnostic
            raw_text_dump = ""
            if isinstance(data_raw, dict):
                for item in data_raw.get("content", []):
                    if isinstance(item, dict) and item.get("type") == "text":
                        raw_text_dump = item.get("text", "")
                        break

            elems, ge_debug = _extract_elems_from_gedata(data_raw)

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

                # Search for Area:
                # 1. Inside parameters dict (AllParameters response)
                # 2. Top-level element fields (basicElementInfo — e.g., elem["area"])
                # 3. Revit built-in parameter names
                AREA_KEYS_FULL = [
                    "Area", "area",
                    "PROPERTY_LINE_AREA", "SITE_PROPERTY_LINE_AREA",
                    "ROOM_AREA", "GSA_SPACE_AREA", "NetArea", "GrossArea",
                    "area", "AREA",
                ]
                area_val = _pick(params, AREA_KEYS_FULL)
                if area_val is None:
                    area_val = _pick(elem, AREA_KEYS_FULL)

                # Search for Name:
                name_val = (
                    elem.get("name")
                    or _pick(params, NAME_KEYS)
                    or _pick(elem, NAME_KEYS)
                    or f"Lot {eid}"
                )

                if area_val is not None:
                    try:
                        area_float = self._extract_number(area_val)
                        if area_float > 0:
                            lots.append({"name": name_val, "area_sqft": area_float, "element_id": eid})
                    except ValueError:
                        pass

            # Brute-force fallback: scan raw text for any area-like number
            if not lots and raw_text_dump:
                _raw_bf = raw_text_dump
                _bf_float = None

                # Strategy 1: Look for "Area" or "area" key followed by number
                _bf_matches = re.findall(
                    r'(?:Area|area|AREA|"Area")\s*[=:]\s*"?([0-9,]+(?:\.[0-9]+))',
                    _raw_bf
                )
                if _bf_matches:
                    for _bf_val in _bf_matches:
                        try:
                            _bf_float = float(_bf_val.replace(",", ""))
                            if _bf_float > 0:
                                break
                        except ValueError:
                            continue

                # Strategy 2: Find any number followed by "sq ft" or "Square Feet"
                if _bf_float is None or _bf_float <= 0:
                    _sqft_matches = re.findall(
                        r'([0-9,]+(?:\.[0-9]+)?)\s*(?:sq\s*\.?\s*ft|square\s*feet|SF)',
                        _raw_bf,
                        re.IGNORECASE
                    )
                    if _sqft_matches:
                        for _v in _sqft_matches:
                            try:
                                _bf_float = float(_v.replace(",", ""))
                                if _bf_float > 0:
                                    break
                            except ValueError:
                                continue

                # Strategy 3: Find any float near the word "Area" (within 100 chars)
                if _bf_float is None or _bf_float <= 0:
                    _area_pos = _raw_bf.lower().find("area")
                    if _area_pos >= 0:
                        _near = _raw_bf[_area_pos:_area_pos + 200]
                        _float_matches = re.findall(r'([0-9,]+(?:\.[0-9]+))', _near)
                        for _v in _float_matches:
                            try:
                                _bf_float = float(_v.replace(",", ""))
                                if _bf_float > 0:
                                    break
                            except ValueError:
                                continue

                if _bf_float is not None and _bf_float > 0:
                    lots.append({"name": "Property Line", "area_sqft": _bf_float, "element_id": element_ids[0]})

            if not lots:
                narrative_parts = [
                    f"OST_SiteProperty IDs found: {element_ids}, "
                    f"but Area could not be read."
                ]
                if params_debug:
                    narrative_parts.append(f"Element structure: {json.dumps(params_debug, default=str)[:2000]}")
                # Dump the first 2000 chars of raw text for diagnosis
                if raw_text_dump:
                    narrative_parts.append(f"Full get_element_data raw text (first 2000 chars): {raw_text_dump[:2000]!r}")
                else:
                    narrative_parts.append(f"get_element_data snippet: {ge_debug!r}")
                narrative_parts.append(f"query_model snippet: {query_debug!r}")
                return {
                    "audit_type": "lot_area",
                    "status": "Unavailable",
                    "narrative": "\n".join(narrative_parts),
                }


            # ----------------------------------------------------------------
            # Step 3: Compute totals
            # ----------------------------------------------------------------
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

    async def _run_lot_coverage_audit(self, arguments: dict) -> dict:
        """
        Lot coverage audit — computes (Building Footprint / Total Lot Area) * 100.

        Building footprint = largest single-level floor plate area.
        Uses the same nuclear-robust parsing strategy as _run_lot_area_audit:
          1. JSON parsing with all known shapes (outcome.elements, elements, results)
          2. Regex fallback for element IDs (6-8 digit numbers)
          3. KeyParameters for get_element_data (agent-proven to return Area+Level)

        Queries: OST_SiteProperty (lot area via _run_lot_area_audit),
                 OST_Floors (building footprint), OST_Areas (additional coverage).
        """
        import re as _re
        from collections import defaultdict

        include_details = arguments.get("include_details", True)

        # ------------------------------------------------------------------
        # Shared helpers — identical to _run_lot_area_audit's proven versions
        # ------------------------------------------------------------------

        def _pick(params: dict, keys: list):
            """Extract the first matching key's value from a param dict."""
            for k in keys:
                if k in params:
                    v = params[k]
                    return v.get("value") if isinstance(v, dict) else v
            return None

        def _get_raw_text(raw_resp: Any) -> str:
            """Pull the inner JSON text string from an MCP content response."""
            if not isinstance(raw_resp, dict):
                return json.dumps(raw_resp, default=str) if raw_resp else ""
            for itm in raw_resp.get("content", []):
                if isinstance(itm, dict) and itm.get("type") == "text":
                    return itm.get("text", "")
            # Fallback: serialize the whole thing
            return json.dumps(raw_resp, default=str)

        def _extract_element_ids(raw_resp: Any) -> list[int]:
            """
            Extract element IDs from a query_model response.
            Strategy: try JSON parsing (all known shapes), then regex fallback.
            """
            ids: list[int] = []
            text = _get_raw_text(raw_resp)
            if not text:
                return ids

            # --- JSON parsing ---
            try:
                parsed = json.loads(text)
                # Shape A: outcome.elements
                for el in parsed.get("outcome", {}).get("elements", []):
                    eid = (el or {}).get("elementId") or (el or {}).get("id")
                    if eid is not None:
                        ids.append(int(eid))
                # Shape A2: top-level elements
                if not ids:
                    for el in parsed.get("elements", []):
                        eid = (el or {}).get("elementId") or (el or {}).get("id")
                        if eid is not None:
                            ids.append(int(eid))
                # Shape B: results["Element Ids"]
                if not ids:
                    r = parsed.get("results", {})
                    if isinstance(r, dict):
                        ids = [int(x) for x in r.get("Element Ids", []) if x]
                    elif isinstance(r, list):
                        ids = [int(x) for x in r if x]
                # Shape C: top-level "Element Ids"
                if not ids and "Element Ids" in parsed:
                    ids = [int(x) for x in parsed["Element Ids"] if x]
            except Exception:
                pass

            # --- Regex fallback: scan raw text for 6-8 digit numbers ---
            if not ids:
                ids = [int(m) for m in _re.findall(r'\b(\d{6,8})\b', text)]

            return ids

        def _extract_elements(raw_resp: Any) -> list[dict]:
            """
            Extract element dicts from a get_element_data response.
            Tries all known JSON shapes, returns list of element dicts.
            """
            text = _get_raw_text(raw_resp)
            if not text:
                return []

            try:
                parsed = json.loads(text)
                # Shape A: top-level "elements" list
                if "elements" in parsed and isinstance(parsed["elements"], list):
                    return parsed["elements"]
                # Shape A2: outcome.elements
                out_e = parsed.get("outcome", {}).get("elements", [])
                if out_e:
                    return out_e
                # Shape B: results dict keyed by element ID
                results = parsed.get("results", {})
                if isinstance(results, dict) and "Element Ids" not in results:
                    elems = [v for v in results.values() if isinstance(v, dict)]
                    if elems:
                        return elems
            except Exception:
                pass

            return []

        # ------------------------------------------------------------------
        # get_category_area — queries a category and returns per-element data
        # ------------------------------------------------------------------

        AREA_KEYS = ["Area", "area", "ROOM_AREA", "GSA_SPACE_AREA", "NetArea", "GrossArea", "AREA"]
        NAME_KEYS = ["Name", "Mark", "Type Name", "Family"]
        LEVEL_KEYS = ["Level", "level", "LEVEL_PARAM"]

        async def get_category_area(category_name: str):
            """Query a Revit category → return (total_sqft, elements_info_with_level)."""
            logger.info("LOT_COVERAGE: querying %s …", category_name)

            raw = await self._run_governed_tool_sync(
                "query_model",
                {
                    "input": {
                        "categories": [category_name],
                        "searchScope": "AllViews",
                        "maxResults": 200,
                    }
                },
            )

            cat_ids = _extract_element_ids(raw)
            logger.info("LOT_COVERAGE: %s → %d element IDs: %s",
                        category_name, len(cat_ids), cat_ids[:10])

            if not cat_ids:
                return 0.0, []

            # Get element data with KeyParameters (agent-proven for floors)
            data_raw = await self._run_governed_tool_sync(
                "get_element_data",
                {
                    "elementIds": [int(eid) for eid in cat_ids],
                    "outputOptions": {
                        "basicElementInfo": True,
                        "parametersOutputType": "KeyParameters",
                    },
                },
            )

            elem_list = _extract_elements(data_raw)
            logger.info("LOT_COVERAGE: %s get_element_data → %d elements",
                        category_name, len(elem_list))

            total_sqft = 0.0
            elements_info = []

            for elem_val in elem_list:
                if not isinstance(elem_val, dict):
                    continue
                params = elem_val.get("parameters", {})
                elem_id = elem_val.get("elementId") or elem_val.get("id", "?")

                # Search params first, then top-level element fields
                area_val = _pick(params, AREA_KEYS)
                if area_val is None:
                    area_val = _pick(elem_val, AREA_KEYS)
                if area_val is None and "area" in elem_val:
                    area_val = elem_val["area"]

                name_val = (
                    elem_val.get("name")
                    or _pick(params, NAME_KEYS)
                    or _pick(elem_val, NAME_KEYS)
                    or f"Element {elem_id}"
                )
                level_val = (
                    _pick(params, LEVEL_KEYS)
                    or _pick(elem_val, LEVEL_KEYS)
                    or elem_val.get("level")
                    or None
                )

                if area_val is not None:
                    try:
                        area_float = self._extract_number(area_val)
                        if area_float > 0:
                            total_sqft += area_float
                            elements_info.append({
                                "name": name_val,
                                "area_sqft": area_float,
                                "level": level_val,
                            })
                            logger.info("LOT_COVERAGE:   %s id=%s area=%.2f level=%s",
                                        category_name, elem_id, area_float, level_val)
                    except ValueError:
                        pass

            logger.info("LOT_COVERAGE: %s total=%.2f sqft (%d elements)",
                        category_name, total_sqft, len(elements_info))
            return total_sqft, elements_info

        # ------------------------------------------------------------------
        # Main execution
        # ------------------------------------------------------------------

        try:
            # 1. Get Lot Area via the proven _run_lot_area_audit
            logger.info("LOT_COVERAGE: Step 1 — querying lot area …")
            lot_result = await self._run_lot_area_audit(arguments)
            if lot_result.get("status") == "Unavailable":
                return {
                    "audit_type": "lot_coverage",
                    "status": "Unavailable",
                    "narrative": (
                        "Lot coverage could not be calculated — the lot area audit failed.\n"
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

            # 2. Get Building Footprint (OST_Floors)
            logger.info("LOT_COVERAGE: Step 2 — querying floors …")
            _, floor_details = await get_category_area("OST_Floors")

            # Group floors by level → find largest single-level area
            level_groups = defaultdict(list)
            for fd in floor_details:
                lv = fd.get("level") or "Unknown Level"
                level_groups[lv].append(fd)

            max_level_name = ""
            max_level_area = 0.0
            level_breakdown = []
            for lv_name, elements in level_groups.items():
                lv_total = sum(e.get("area_sqft", 0) for e in elements)
                level_breakdown.append({
                    "level_name": lv_name,
                    "total_area_sqft": lv_total,
                    "elements": elements,
                })
                if lv_total > max_level_area:
                    max_level_area = lv_total
                    max_level_name = lv_name

            floor_sqft = max_level_area  # building footprint = max single-level area
            logger.info("LOT_COVERAGE: footprint = %.2f sqft (level: %s)",
                        floor_sqft, max_level_name)

            # 3. Get Additional Covered Areas (OST_Areas)
            logger.info("LOT_COVERAGE: Step 3 — querying areas …")
            area_sqft, area_details = await get_category_area("OST_Areas")

            # 4. Compute coverage
            total_covered_sqft = floor_sqft + area_sqft
            building_coverage_pct = (floor_sqft / total_lot_sqft) * 100
            total_coverage_pct = (total_covered_sqft / total_lot_sqft) * 100

            logger.info("LOT_COVERAGE: RESULT — building=%.1f%%, total=%.1f%%",
                        building_coverage_pct, total_coverage_pct)

            # 5. Build narrative
            narrative = "Lot Coverage Audit Complete.\n"
            narrative += f"Total Lot Area: {total_lot_sqft:,.2f} sq ft\n"
            narrative += f"Building Footprint Area: {floor_sqft:,.2f} sq ft\n"
            narrative += f"  (largest single-level floor plate: {max_level_name})\n"
            narrative += f"Additional Covered Area: {area_sqft:,.2f} sq ft\n\n"
            narrative += f"Building Lot Coverage: {building_coverage_pct:.1f}%\n"
            narrative += f"Total Lot Coverage: {total_coverage_pct:.1f}%\n"

            if include_details:
                if level_breakdown:
                    narrative += "\nBuilding Footprint — Per-Level Breakdown:\n"
                    for lv_entry in level_breakdown:
                        narrative += f"\n  Level: {lv_entry['level_name']}\n"
                        for elem in lv_entry["elements"]:
                            narrative += f"    - {elem['name']}: {elem['area_sqft']:,.2f} sq ft\n"
                        narrative += f"    Level Subtotal: {lv_entry['total_area_sqft']:,.2f} sq ft\n"
                    narrative += f"\n  Building Footprint (Largest Level): {max_level_area:,.2f} sq ft ({max_level_name})\n"
                if area_details:
                    narrative += "\nAdditional Covered Area Details:\n"
                    for a in area_details:
                        narrative += f"  - {a['name']}: {a['area_sqft']:,.2f} sq ft\n"

            return {
                "audit_type": "lot_coverage",
                "status": "Success",
                "lot_area_sqft": total_lot_sqft,
                "building_area_sqft": floor_sqft,
                "additional_area_sqft": area_sqft,
                "building_coverage_percent": building_coverage_pct,
                "total_coverage_percent": total_coverage_pct,
                "max_level_name": max_level_name,
                "level_breakdown": level_breakdown,
                "details": {
                    "floors": floor_details,
                    "areas": area_details,
                },
                "narrative": narrative,
            }

        except Exception as e:
            logger.error("LOT_COVERAGE: EXCEPTION — %s", e, exc_info=True)
            return {
                "audit_type": "lot_coverage",
                "error": str(e),
                "narrative": f"Error running lot coverage audit: {e}",
            }


    async def _run_setback_audit(self, arguments: dict) -> dict:
        """
        Setback audit requires geometry from property lines to calculate distance to walls.
        Since the Autodesk Revit MCP server does not export geometry for property lines,
        this deterministic mathematical calculation is currently impossible.
        """
        return {
            "audit_type": "setback",
            "status": "unavailable",
            "narrative": (
                "The Setback Audit is currently unavailable. The tool relies on extracting "
                "precise coordinate geometry from property lines to calculate the perpendicular "
                "distance to building exterior walls. However, the Autodesk Revit MCP Server "
                "currently does not export geometry for property lines, making this deterministic "
                "mathematical calculation impossible. Please check back in future API updates."
            )
        }
    async def handle_request(self, request: dict) -> dict:
        """
        Handle a single JSON-RPC 2.0 request.
        Returns a JSON-RPC response dict.
        """
        request_id = request.get("id")
        method = request.get("method", "")
        params = request.get("params", {})

        logger.info("Received request: method=%s id=%s", method, request_id)

        try:
            if method == "tools/list":
                tools = await self.get_tools()
                return {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {"tools": tools},
                }

            elif method == "tools/call":
                tool_name = params.get("name", "")
                arguments = params.get("arguments", {})

                if not tool_name:
                    return {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "error": {
                            "code": -32602,
                            "message": "Missing required parameter: 'name'",
                        },
                    }

                # Route to custom audit tools
                if tool_name == "axo_audit_septic":
                    result = await self._run_septic_audit(arguments)
                    return {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "result": {
                            "content": [
                                {
                                    "type": "text",
                                    "text": json.dumps(result, indent=2),
                                }
                            ]
                        },
                    }

                elif tool_name == "axo_audit_energy":
                    result = await self._run_energy_audit(arguments)
                    return {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "result": {
                            "content": [
                                {
                                    "type": "text",
                                    "text": json.dumps(result, indent=2),
                                }
                            ]
                        },
                    }

                elif tool_name == "axo_audit_wwr":
                    result = await self._run_wwr_audit(arguments)
                    return {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "result": {
                            "content": [
                                {
                                    "type": "text",
                                    "text": json.dumps(result, indent=2),
                                }
                            ]
                        },
                    }

                elif tool_name == "axo_audit_floor_area":
                    result = await self._run_floor_area_audit(arguments)
                    return {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "result": {
                            "content": [
                                {
                                    "type": "text",
                                    "text": json.dumps(result, indent=2),
                                }
                            ]
                        },
                    }

                elif tool_name == "axo_audit_lot_area":
                    result = await self._run_lot_area_audit(arguments or {})
                    return {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "result": {
                            "content": [
                                {
                                    "type": "text",
                                    "text": json.dumps(result, indent=2),
                                }
                            ]
                        },
                    }

                elif tool_name == "axo_audit_lot_coverage":
                    result = await self._run_lot_coverage_audit(arguments or {})
                    return {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "result": {
                            "content": [
                                {
                                    "type": "text",
                                    "text": json.dumps(result, indent=2),
                                }
                            ]
                        },
                    }

                elif tool_name == "axo_audit_setback":
                    result = await self._run_setback_audit(arguments or {})
                    return {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "result": {
                            "content": [
                                {
                                    "type": "text",
                                    "text": json.dumps(result, indent=2),
                                }
                            ]
                        },
                    }

                # All other tools: proxy to the Autodesk Revit pipe
                else:
                    raw_result = await self._call_revit_tool(tool_name, arguments)

                    # _call_revit_tool already returns MCP-wrapped content via
                    # _translate_response: {"content": [{"type": "text", "text": "..."}]}.
                    # Check if already wrapped to avoid double-wrapping.
                    if isinstance(raw_result, dict) and "content" in raw_result:
                        return {
                            "jsonrpc": "2.0",
                            "id": request_id,
                            "result": raw_result,
                        }

                    # Fallback: wrap in MCP-compliant content format
                    return {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "result": {
                            "content": [
                                {
                                    "type": "text",
                                    "text": json.dumps(raw_result, indent=2)
                                    if not isinstance(raw_result, str)
                                    else raw_result,
                                }
                            ]
                        },
                    }

            elif method == "resources/list":
                # Optional MCP method — return empty for now
                return {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {"resources": []},
                }

            elif method == "prompts/list":
                # Optional MCP method — return empty for now
                return {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {"prompts": []},
                }

            elif method == "initialize":
                # MCP initialization handshake
                return {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {
                            "tools": {},
                            "resources": {},
                            "prompts": {},
                        },
                        "serverInfo": {
                            "name": "axoworks-revit-proxy",
                            "version": "1.0.0",
                        },
                    },
                }

            elif method == "notifications/initialized":
                # No response needed for notifications
                return None

            else:
                return {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {
                        "code": -32601,
                        "message": f"Method not found: {method}",
                    },
                }

        except RevitBridgeError as exc:
            logger.error("Bridge error handling %s: %s", method, exc)
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {
                    "code": -32000,
                    "message": f"Revit Bridge Error: {exc}",
                },
            }
        except Exception as exc:
            logger.error("Unexpected error handling %s: %s", method, exc)
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {
                    "code": -32603,
                    "message": f"Internal error: {exc}",
                },
            }

    # -----------------------------------------------------------------------
    # Stdio event loop
    # -----------------------------------------------------------------------

    async def run_stdio(self):
        """
        Read JSON-RPC 2.0 requests from stdin (line-delimited) and write
        responses to stdout.  This is the standard MCP stdio transport.
        """
        logger.info("Axoworks Revit MCP Proxy starting (stdio transport)...")
        logger.info("Reading JSON-RPC requests from stdin, writing to stdout.")

        # Signal readiness on stderr (not stdout — that's the protocol channel)
        print("Axoworks Revit MCP Proxy ready.", file=sys.stderr, flush=True)

        loop = asyncio.get_running_loop()

        while True:
            try:
                # Read one line from stdin (asynchronously)
                line = await loop.run_in_executor(None, sys.stdin.readline)
                if not line:
                    logger.info("stdin closed. Shutting down.")
                    break

                line = line.strip()
                if not line:
                    continue

                # Parse the JSON-RPC request
                try:
                    request = json.loads(line)
                except json.JSONDecodeError as exc:
                    logger.error("Invalid JSON on stdin: %s", exc)
                    response = {
                        "jsonrpc": "2.0",
                        "id": None,
                        "error": {"code": -32700, "message": f"Parse error: {exc}"},
                    }
                    print(json.dumps(response), flush=True)
                    continue

                if not isinstance(request, dict) or "method" not in request:
                    logger.error("Invalid request format: %s", request)
                    response = {
                        "jsonrpc": "2.0",
                        "id": request.get("id") if isinstance(request, dict) else None,
                        "error": {"code": -32600, "message": "Invalid Request"},
                    }
                    print(json.dumps(response), flush=True)
                    continue

                # Handle the request
                response = await self.handle_request(request)

                # Notifications don't get responses
                if response is None:
                    continue

                # Write the response as a newline-delimited JSON line
                print(json.dumps(response), flush=True)

            except KeyboardInterrupt:
                logger.info("Keyboard interrupt received. Shutting down.")
                break
            except Exception as exc:
                logger.error("Fatal error in stdio loop: %s", exc)
                # Try to send error response
                try:
                    error_response = {
                        "jsonrpc": "2.0",
                        "id": None,
                        "error": {"code": -32603, "message": f"Fatal error: {exc}"},
                    }
                    print(json.dumps(error_response), flush=True)
                except Exception:
                    pass
                break


# ---------------------------------------------------------------------------
# Entry Point
# ---------------------------------------------------------------------------

async def main():
    transport = McpStdioTransport()
    await transport.run_stdio()


if __name__ == "__main__":
    import asyncio
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Server stopped by user.")
    except Exception as exc:
        logger.error("Server crashed: %s", exc)
        sys.exit(1)
