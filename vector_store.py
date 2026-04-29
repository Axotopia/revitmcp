from typing import List, Any

def query_code_db(query: str, jurisdiction: str = "default", top_k: int = 4) -> List[dict[str, Any]]:
    snippets = [
        {
            "source": "IBC 2024 / Local Amendments",
            "section": "R301.2",
            "jurisdiction": jurisdiction,
            "text": "Septic tanks shall be setback not less than 50 feet from property lines.",
            "score": 0.99,
        }
    ]
    if "energy" in query.lower() or "u-factor" in query.lower():
        snippets.append({
            "source": "IECC 2024 Table C402.1.4",
            "section": "C402.1.4",
            "jurisdiction": jurisdiction,
            "text": "Climate Zone 5B: Wall U-factor max 0.060, Roof U-factor max 0.032, Window U-factor max 0.30, SHGC max 0.25.",
            "score": 0.98,
        })
    return snippets
