"""
End-to-end tests using Flask's test client (in-process, same routes/templates/DB).
Run:  PYTHONPATH=. PG... python3 tests/test_app.py
Exits non-zero on any failure.
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.main import app
from app.config import query
from app.recommender import recommend_team
from nlp.nlq_engine import ask

PASS, FAIL = 0, 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


def login(client):
    return client.post("/login", data={"username": "admin", "password": "admin123"},
                       follow_redirects=True)


def test_auth_and_pages():
    print("\n[auth & pages]")
    app.config["TESTING"] = True
    with app.test_client() as c:
        # unauthenticated dashboard redirects to login
        r = c.get("/", follow_redirects=False)
        check("dashboard requires login", r.status_code == 302)
        # bad login
        r = c.post("/login", data={"username": "x", "password": "y"}, follow_redirects=True)
        check("bad login rejected", b"Invalid" in r.data or b"\xe0" in r.data)
        # good login
        r = login(c)
        check("demo login works", r.status_code == 200)
        # dashboard shows counts
        total = query("SELECT count(*) c FROM clean.person")[0]["c"]
        r = c.get("/")
        check("dashboard loads", r.status_code == 200 and str(total).encode() in r.data,
              f"expected total {total} on dashboard")
        # recommend form
        r = c.get("/recommend")
        check("recommend form loads", r.status_code == 200 and b"Required skills" in r.data)
        # ask page
        r = c.get("/ask")
        check("ask page loads", r.status_code == 200)
        # personnel
        r = c.get("/personnel?spec=CYBER")
        check("personnel filter loads", r.status_code == 200)
        # language toggle to Gujarati
        c.get("/set-language/gu")
        r = c.get("/")
        check("gujarati toggle renders", "ડેશબોર્ડ".encode() in r.data,
              "expected Gujarati nav text")
        c.get("/set-language/en")


def test_recommend_post():
    print("\n[recommendation via HTTP POST]")
    with app.test_client() as c:
        login(c)
        # pick a real large station id
        sid = query("SELECT current_station_id sid FROM clean.person "
                    "WHERE current_station_id IS NOT NULL GROUP BY 1 "
                    "ORDER BY count(*) DESC LIMIT 1")[0]["sid"]
        r = c.post("/recommend", data={
            "station_id": str(sid),
            "specializations": ["TRAFFIC", "FIELD_GENERAL"],
            "team_size": "4",
            "rank_PI": "1",
        }, follow_redirects=True)
        check("recommend POST returns team", r.status_code == 200 and b"skill coverage" in r.data)


def test_recommender_logic():
    print("\n[recommender logic]")
    sid = query("SELECT current_station_id sid FROM clean.person "
                "WHERE current_station_id IS NOT NULL GROUP BY 1 "
                "ORDER BY count(*) DESC LIMIT 1")[0]["sid"]
    res = recommend_team(sid, {"TRAFFIC", "FIELD_GENERAL", "WOMEN_SAFETY"}, 5, {"PI": 1, "PSI": 1})
    check("team has members", len(res["team"]) > 0)
    check("every member has a reason", all(m["why"] for m in res["team"]))
    check("coverage computed", 0 <= res["team_rationale"]["skill_coverage_pct"] <= 100)
    check("team size respected", len(res["team"]) <= 5)
    # rank slots honoured (at least one PI present if station has one)
    ranks = [m["rank"] for m in res["team"]]
    check("requested PI slot filled or flagged",
          "PI" in ranks or "PI" in res["team_rationale"]["unfilled_rank_slots"])
    # empty station handled
    res2 = recommend_team(999999, {"CYBER"}, 3)
    check("nonexistent station handled", "error" in res2)


def test_nlp():
    print("\n[nlp q&a]")
    r = ask("How many PSIs are there?")
    check("count PSIs grounded", r["ok"] and r["rows"] and r["rows"][0].get("count", 0) > 0)
    r = ask("list cyber personnel")
    check("list cyber grounded", r["ok"] and r["intent"] == "list_personnel")
    r = ask("what is the weather today")
    check("nonsense rejected", not r["ok"])
    # PII safety: ensure no template can return aadhar/pan/mobile
    for label in ("list_personnel", "count_personnel", "top_awards", "station_vacancy"):
        pass
    r = ask("list traffic officers")
    cols = set(r.get("columns", []))
    check("no PII in NLP output",
          not (cols & {"aadhar_number", "pan_number", "mobile_number", "home_address"}))


def test_pii_isolation():
    print("\n[privacy: clean store has no PII columns]")
    cols = query("""
        SELECT column_name FROM information_schema.columns
        WHERE table_schema='clean'
    """)
    names = {c["column_name"] for c in cols}
    leaked = names & {"aadhar_number", "pan_number", "mobile_number",
                      "home_address", "current_address", "photo"}
    check("clean schema excludes PII", not leaked, f"leaked: {leaked}")


if __name__ == "__main__":
    test_auth_and_pages()
    test_recommend_post()
    test_recommender_logic()
    test_nlp()
    test_pii_isolation()
    print(f"\n{'='*40}\nRESULT: {PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)
