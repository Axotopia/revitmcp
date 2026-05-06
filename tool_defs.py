"""
Custom Audit Tool Schema Definitions
======================================
Defines the MCP tool schemas for Axoworks custom audit tools.
These are merged with native Revit tools during tools/list discovery.
"""


def build_custom_tools() -> list[dict]:
    """Return the list of custom audit tool schemas."""
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
            "name": "axo_query_floor_area",
            "description": (
                "Query floor element areas from the active Revit model. "
                "Queries OST_Floors elements, extracts the Area parameter using AllParameters, "
                "groups by Level, and returns the largest single-level floor area "
                "(building footprint) plus a per-level breakdown. "
                "Use this tool to determine the building footprint for lot coverage calculations."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "level_names": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional list of level names to filter by (e.g., ['FP1.GARAGE', 'FP2.ADU']). If omitted, returns data for all levels.",
                    },
                    "include_details": {
                        "type": "boolean",
                        "description": "Include per-element area breakdown. Default: true.",
                        "default": True,
                    },
                },
                "required": [],
            },
        },
        {
            "name": "axo_audit_lot_coverage",
            "description": (
                "Calculate lot coverage percentage for the active Revit model. "
                "Internally performs two steps: "
                "(1) queries property lines (OST_SiteProperty) for lot area, "
                "(2) queries floor elements (OST_Floors) grouped by level to find "
                "the largest single-level floor plate as the building footprint. "
                "Returns lot area, building footprint, and lot coverage percentage "
                "(footprint / lot area × 100). "
                "If the tool returns 0% coverage but you know floors exist, "
                "try asking 'Largest floor area at a single floor level' followed by "
                "'Deduce lot coverage' as a manual multi-step workaround."
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
