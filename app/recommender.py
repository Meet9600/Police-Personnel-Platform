#!/usr/bin/env python3
"""
Explainable team-recommendation engine.

Given a senior officer's requirement (station, case type / needed specializations,
team size, optional rank mix), it:
  1. FILTERS to active personnel at the requesting station (per product decision).
  2. SCORES every candidate on a transparent weighted function. Specialization
     match dominates (per product decision), then rank fit, experience, and a
     clean disciplinary record.
  3. SELECTS a team that satisfies the requested size and rank mix, greedily
     taking the highest-scoring candidate for each required slot.
  4. EXPLAINS each pick — a per-officer reason string and a team-level rationale.

No ML black box: the score is a documented linear combination, so every
recommendation is fully auditable (essential for government accountability).
A clustering layer can be added later once labelled outcomes exist.

Pure local Postgres. No external calls.
"""
import psycopg2.extras

from app.config import get_conn
from flask_babel import gettext as _

# ---- Scoring weights (sum the component scores; specialization dominates) ----
W_SPEC      = 60.0   # fraction of requested specializations this person covers
W_RANK      = 20.0   # matches a still-needed rank slot
W_EXPERIENCE= 12.0   # normalized years of service (capped)
W_AWARDS    = 5.0    # awards, capped
W_CLEAN     = 3.0    # no punishments
PUNISH_PENALTY = 8.0 # per punishment, subtracted

EXPERIENCE_CAP_YEARS = 25.0
AWARDS_CAP = 3


def fetch_station_candidates(conn, station_ids=None, division_ids=None):
    """All active personnel at the selected stations/divisions, with features + specialization set."""
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT p.person_id, p.full_name_gu, p.rank_code, r.rank_band, r.rank_order,
               p.years_of_service, p.age_years, p.photo,
               COALESCE(perf.awards_count,0)      AS awards,
               COALESCE(perf.punishments_count,0) AS punishments,
               COALESCE(
                 (SELECT array_agg(DISTINCT ps.spec_code)
                  FROM clean.person_specialization ps WHERE ps.person_id=p.person_id),
                 '{}') AS specs
        FROM clean.person p
        JOIN clean.rank_ref r ON r.rank_code = p.rank_code
        LEFT JOIN clean.person_performance perf ON perf.person_id = p.person_id
        LEFT JOIN clean.dim_station s ON s.station_id = p.current_station_id
        WHERE (
            (%(st)s IS NULL AND %(div)s IS NULL)
            OR p.current_station_id = ANY(%(st)s)
            OR s.division_id = ANY(%(div)s)
        ) AND p.is_active
    """, {"st": station_ids or None, "div": division_ids or None})
    out = []
    for row in cur.fetchall():
        d = dict(row)
        d["specs"] = set(d["specs"] or [])
        out.append(d)
    cur.close()
    return out


def score_candidate(c, needed_specs, needed_rank=None):
    """Return (score, reasons[]) for one candidate against the requirement."""
    reasons = []
    score = 0.0

    # Specialization match — dominant signal.
    matched = c["specs"] & needed_specs
    if needed_specs:
        frac = len(matched) / len(needed_specs)
        score += W_SPEC * frac
        if matched:
            reasons.append(_("covers %(num)d/%(total)d required skills: %(skills)s", num=len(matched), total=len(needed_specs), skills=", ".join(sorted(matched))))
        else:
            reasons.append(_("no direct skill match"))

    # Rank fit — does this person fill a still-needed rank slot?
    if needed_rank and c["rank_code"] == needed_rank:
        score += W_RANK
        reasons.append(_("fills required rank %(rank)s", rank=needed_rank))

    # Experience (batch-derived; may be missing).
    yos = c["years_of_service"]
    if yos is not None:
        yos = float(yos)
        exp = min(yos, EXPERIENCE_CAP_YEARS) / EXPERIENCE_CAP_YEARS
        score += W_EXPERIENCE * exp
        reasons.append(_("%(yos)d yrs service", yos=int(yos)))

    # Awards (capped).
    if c["awards"]:
        score += W_AWARDS * min(c["awards"], AWARDS_CAP) / AWARDS_CAP
        reasons.append(_("%(count)d award(s)", count=c['awards']))

    # Clean-record bonus / punishment penalty.
    if c["punishments"] == 0:
        score += W_CLEAN
    else:
        score -= PUNISH_PENALTY * c["punishments"]
        reasons.append(_("%(count)d punishment(s) on record", count=c['punishments']))

    return round(score, 2), reasons


def recommend_team(station_ids, division_ids, needed_specs, team_size, rank_mix=None):
    """
    needed_specs : set of spec_code strings the case requires
    team_size    : total people wanted
    rank_mix     : optional dict {rank_code: count}; must sum <= team_size.
                   Remaining slots are filled by best overall score.
    Returns a dict with the team, per-member explanations, and team rationale.
    """
    station_ids = station_ids or []
    division_ids = division_ids or []
    with get_conn(read_only=True) as conn:
        candidates = fetch_station_candidates(conn, station_ids=station_ids, division_ids=division_ids)
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        target_names = []
        if station_ids:
            cur.execute("SELECT name_en FROM clean.dim_station WHERE station_id = ANY(%s)", (station_ids,))
            target_names.extend([r["name_en"] for r in cur.fetchall()])
        if division_ids:
            cur.execute("SELECT name_en FROM clean.dim_division WHERE division_id = ANY(%s)", (division_ids,))
            target_names.extend([f"{r['name_en']} Division" for r in cur.fetchall()])
        target_name = ", ".join(target_names) if target_names else "All Stations"
        cur.close()

    if not candidates:
        return {"error": _("No active personnel found at %(target)s.", target=target_name)}

    rank_mix = dict(rank_mix or {})
    chosen, chosen_ids = [], set()

    def take(candidates, needed_rank=None):
        scored = []
        for c in candidates:
            if c["person_id"] in chosen_ids:
                continue
            if needed_rank and c["rank_code"] != needed_rank:
                continue
            s, reasons = score_candidate(c, needed_specs)
            scored.append({"person": c, "score": s, "reasons": reasons})
            
        if not scored:
            return False
            
        scored.sort(key=lambda x: -x["score"])
        best = scored[0]
        alts = []
        for alt in scored[1:6]:
            alts.append({
                "person_id": alt["person"]["person_id"],
                "name": alt["person"]["full_name_gu"],
                "rank": alt["person"]["rank_code"],
                "photo": alt["person"].get("photo"),
                "score": alt["score"],
                "why": "; ".join(alt["reasons"])
            })
            
        chosen_ids.add(best["person"]["person_id"])
        chosen.append({
            "person": best["person"], 
            "score": best["score"], 
            "reasons": best["reasons"],
            "alternatives": alts
        })
        return True

    # 1) Fill explicit rank slots first.
    unfilled_ranks = []
    for rank, count in rank_mix.items():
        for _ in range(count):
            if not take(candidates, needed_rank=rank):
                unfilled_ranks.append(rank)

    # 2) Fill remaining slots, prioritizing still-uncovered skills first
    #    (specialization is the dominant priority per product decision).
    def covered_now():
        cov = set()
        for cid in chosen_ids:
            pc = next((c for c in candidates if c["person_id"] == cid), None)
            if pc:
                cov |= (pc["specs"] & needed_specs)
        return cov

    while len(chosen) < team_size:
        uncovered = needed_specs - covered_now()
        pool_with_gap = [c for c in candidates
                         if c["person_id"] not in chosen_ids and (c["specs"] & uncovered)]
        target_pool = pool_with_gap if (uncovered and pool_with_gap) else candidates
        scoring_specs = uncovered if (uncovered and pool_with_gap) else needed_specs
        
        scored = []
        for c in target_pool:
            if c["person_id"] in chosen_ids:
                continue
            s, _ = score_candidate(c, scoring_specs)
            disp_score, disp_reasons = score_candidate(c, needed_specs)
            scored.append({"person": c, "sort_score": s, "score": disp_score, "reasons": disp_reasons})
            
        if not scored:
            break
            
        scored.sort(key=lambda x: -x["sort_score"])
        best = scored[0]
        alts = []
        for alt in scored[1:6]:
            alts.append({
                "person_id": alt["person"]["person_id"],
                "name": alt["person"]["full_name_gu"],
                "rank": alt["person"]["rank_code"],
                "photo": alt["person"].get("photo"),
                "score": alt["score"],
                "why": "; ".join(alt["reasons"])
            })
            
        chosen_ids.add(best["person"]["person_id"])
        chosen.append({
            "person": best["person"], 
            "score": best["score"], 
            "reasons": best["reasons"],
            "alternatives": alts
        })

    # 3) Team-level rationale: coverage of requested specializations.
    covered = set()
    for m in chosen:
        covered |= (m["person"]["specs"] & needed_specs)
    coverage_pct = (len(covered) / len(needed_specs) * 100) if needed_specs else 100
    missing = needed_specs - covered

    # 3b) For any missing skill, find who ELSE in the same division has it
    #     (informational only — not auto-pulled into the team).
    division_suggestions = []
    if missing and (station_ids or division_ids):
        with get_conn(read_only=True) as conn2:
            c2 = conn2.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            if station_ids:
                c2.execute("""
                    SELECT p.person_id, p.full_name_gu AS name, p.rank_code AS rank,
                           s.name_en AS station, ps.spec_code
                    FROM clean.person p
                    JOIN clean.dim_station s ON s.station_id = p.current_station_id
                    JOIN clean.person_specialization ps ON ps.person_id = p.person_id
                    LEFT JOIN clean.rank_ref r ON p.rank_code = r.rank_code
                    WHERE s.division_id IN (SELECT division_id FROM clean.dim_station WHERE station_id = ANY(%(st)s))
                      AND NOT (p.current_station_id = ANY(%(st)s))
                      AND ps.spec_code = ANY(%(specs)s)
                      AND p.is_active
                    ORDER BY ps.spec_code, r.rank_order DESC
                    LIMIT 30
                """, {"st": station_ids, "specs": list(missing)})
            else:
                # If selecting entire divisions, suggest from other divisions
                c2.execute("""
                    SELECT p.person_id, p.full_name_gu AS name, p.rank_code AS rank,
                           s.name_en AS station, ps.spec_code
                    FROM clean.person p
                    JOIN clean.dim_station s ON s.station_id = p.current_station_id
                    JOIN clean.person_specialization ps ON ps.person_id = p.person_id
                    LEFT JOIN clean.rank_ref r ON p.rank_code = r.rank_code
                    WHERE NOT (s.division_id = ANY(%(div)s))
                      AND ps.spec_code = ANY(%(specs)s)
                      AND p.is_active
                    ORDER BY ps.spec_code, r.rank_order DESC
                    LIMIT 30
                """, {"div": division_ids, "specs": list(missing)})
            for row in c2.fetchall():
                division_suggestions.append(dict(row))
            c2.close()

    return {
        "station": target_name,
        "station_id": station_ids,
        "requested": {"team_size": team_size, "specializations": sorted(needed_specs),
                      "rank_mix": rank_mix},
        "team": [
            {
                "person_id": m["person"]["person_id"],
                "name": m["person"]["full_name_gu"],
                "rank": m["person"]["rank_code"],
                "photo": m["person"]["photo"],
                "score": m["score"],
                "why": "; ".join(m["reasons"]),
                "alternatives": m.get("alternatives", []),
            } for m in sorted(chosen, key=lambda x: (-x["person"]["rank_order"], -x["score"]))
        ],
        "team_rationale": {
            "skill_coverage_pct": round(coverage_pct, 1),
            "skills_covered": sorted(covered),
            "skills_missing": sorted(missing),
            "unfilled_rank_slots": unfilled_ranks,
            "division_suggestions": division_suggestions,
            "summary": (
                f"This team covers {round(coverage_pct)}% of the required skills "
                f"({', '.join(sorted(covered)) or 'none'}) using personnel posted at "
                f"{target_name}."
                + (f" Could not source: {', '.join(sorted(missing))}." if missing else "")
                + (f" Unfilled rank slots: {', '.join(unfilled_ranks)}." if unfilled_ranks else "")
            ),
        },
    }


# ---- CLI demo -------------------------------------------------------------
if __name__ == "__main__":
    import json
    # Example: cyber-crime case at station 37, want 4 people incl. 1 PI lead.
    result = recommend_team(
        station_ids=[37],
        division_ids=None,
        needed_specs={"CYBER", "CRIME_INVEST", "IT_COMPUTER"},
        team_size=4,
        rank_mix={"PI": 1},
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
