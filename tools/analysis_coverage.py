"""Describe incomplete debates separately from valid hold decisions."""


def summarize_analysis(archived_symbols, eligible_symbols, decided_symbols,
                       held_symbols, evidence_skips):
    archived = set(archived_symbols)
    eligible = set(eligible_symbols)
    decided = set(decided_symbols)
    missing = sorted(eligible - decided)
    return {
        "status": "degraded" if missing or evidence_skips else "complete",
        "archived_candidates": len(archived),
        "held_removed": sorted(archived & set(held_symbols)),
        "evidence_skips": [skip["symbol"] for skip in evidence_skips],
        "debate_candidates": len(eligible),
        "completed_decisions": len(decided),
        "missing_decision_symbols": missing,
    }
