import os
import sys

# Add current directory to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Define mock vocab
MOCK_VOCAB = {
    "ranks": [
        {"rank_code": "PI", "rank_name_en": "Police Inspector", "rank_band": "officer"},
        {"rank_code": "PSI", "rank_name_en": "Police Sub-Inspector", "rank_band": "officer"},
        {"rank_code": "AASI", "rank_name_en": "Armed Assistant Sub-Inspector", "rank_band": "employee"},
        {"rank_code": "UASI", "rank_name_en": "Unarmed Assistant Sub-Inspector", "rank_band": "employee"},
        {"rank_code": "AHC", "rank_name_en": "Armed Head Constable", "rank_band": "employee"},
        {"rank_code": "UHC", "rank_name_en": "Unarmed Head Constable", "rank_band": "employee"},
        {"rank_code": "APC", "rank_name_en": "Armed Police Constable", "rank_band": "employee"},
        {"rank_code": "UPC", "rank_name_en": "Unarmed Police Constable", "rank_band": "employee"},
        {"rank_code": "ALR", "rank_name_en": "Armed Lokrakshak", "rank_band": "employee"},
        {"rank_code": "ULR", "rank_name_en": "Unarmed Lokrakshak", "rank_band": "employee"}
    ],
    "specs": [
        {"spec_code": "CYBER", "spec_name_en": "Cyber Crime", "category": "Investigation"},
        {"spec_code": "CRIME_INVEST", "spec_name_en": "Crime Investigation", "category": "Investigation"},
        {"spec_code": "SOG", "spec_name_en": "Special Operations Group", "category": "Investigation"},
        {"spec_code": "TRAFFIC", "spec_name_en": "Traffic", "category": "Field"},
        {"spec_code": "PATROL_MOBILE", "spec_name_en": "Patrol / Mobile", "category": "Field"},
        {"spec_code": "PCR_112", "spec_name_en": "PCR / 112 Response", "category": "Field"},
        {"spec_code": "FIELD_GENERAL", "spec_name_en": "General Field Duty", "category": "Field"},
        {"spec_code": "WOMEN_SAFETY", "spec_name_en": "Women Safety (SHE Team/Help Desk)", "category": "Community"},
        {"spec_code": "CHILD_WELFARE", "spec_name_en": "Child Welfare / SPC", "category": "Community"},
        {"spec_code": "COMMUNITY", "spec_name_en": "Community Policing (Suraksha Setu)", "category": "Community"},
        {"spec_code": "COURT_LEGAL", "spec_name_en": "Court / Summons / Warrant", "category": "Legal"},
        {"spec_code": "GUARD_SECURITY", "spec_name_en": "Guard / Escort / Security", "category": "Security"},
        {"spec_code": "VIP_PROTECTION", "spec_name_en": "VIP / Bungalow Protection", "category": "Security"},
        {"spec_code": "ARMOURY", "spec_name_en": "Armoury / Weapons", "category": "Security"},
        {"spec_code": "COMMANDO", "spec_name_en": "Commando / Gunman", "category": "Security"},
        {"spec_code": "CONTROL_ROOM", "spec_name_en": "Control Room / Wireless", "category": "Operations"},
        {"spec_code": "IT_COMPUTER", "spec_name_en": "Computer / IT Operations", "category": "Operations"},
        {"spec_code": "DRIVER", "spec_name_en": "Driver", "category": "Operations"},
        {"spec_code": "DOG_SQUAD", "spec_name_en": "Dog Handler", "category": "Operations"},
        {"spec_code": "WRITER_CLERK", "spec_name_en": "Writer / Clerical", "category": "Administration"},
        {"spec_code": "ACCOUNTS", "spec_name_en": "Accounts", "category": "Administration"},
        {"spec_code": "REGISTRY", "spec_name_en": "Registry / Application Branch", "category": "Administration"},
        {"spec_code": "STORE", "spec_name_en": "Store / Quartermaster", "category": "Administration"},
        {"spec_code": "ADMIN_GENERAL", "spec_name_en": "General Administration", "category": "Administration"},
        {"spec_code": "STATION_INCHARGE", "spec_name_en": "Station / Post In-charge", "category": "Command"},
        {"spec_code": "PSO", "spec_name_en": "Personal Security Officer", "category": "Security"},
        {"spec_code": "UNCLASSIFIED", "spec_name_en": "Unclassified (needs review)", "category": "Review"}
    ],
    "stations": [
        {"station_id": 1, "name_en": "Aslali", "name_raw": "અસલાલી"},
        {"station_id": 2, "name_en": "Sanand", "name_raw": "સાણંદ"},
        {"station_id": 3, "name_en": "Viramgam", "name_raw": "વિરમગામ"},
        {"station_id": 4, "name_en": "Dholka", "name_raw": "ધોળકા"},
        {"station_id": 5, "name_en": "Dhandhuka", "name_raw": "ધંધુકા"},
        {"station_id": 6, "name_en": "Bopal", "name_raw": "બોપલ"},
        {"station_id": 7, "name_en": "Mandal", "name_raw": "માંડલ"},
        {"station_id": 8, "name_en": "Detroj", "name_raw": "ડેટરોજ"},
        {"station_id": 9, "name_en": "Bavla", "name_raw": "બાવળા"},
        {"station_id": 10, "name_en": "Kanbha", "name_raw": "કણભા"},
        {"station_id": 11, "name_en": "Changodar", "name_raw": "ચાંગોદર"}
    ],
    "divisions": [
        {"division_id": 1, "name_en": "Sanand Division", "name_raw": "સાણંદ ડિવિઝન"},
        {"division_id": 2, "name_en": "Dholka Division", "name_raw": "ધોળકા ડિવિઝન"},
        {"division_id": 3, "name_en": "Viramgam Division", "name_raw": "વિરમગામ ડિવિઝન"}
    ]
}

# Mock clean store query
def mock_query(sql, params=None):
    if "COUNT(*)" in sql or "count" in sql.lower():
        return [{"count": 42}]
    return []

# Apply patch to nlp.nlq_engine
import nlp.nlq_engine
nlp.nlq_engine.query = mock_query
nlp.nlq_engine._load_vocab = lambda: MOCK_VOCAB

# Re-initialize engine
engine = nlp.nlq_engine.NLQEngine()

# Test cases with expected outputs to verify logic correctness
tests = [
    # 1. Basic counts
    {
        "q": "How many PSIs are there?",
        "expected_intent": "count_personnel",
        "expected_interpretation_contains": ["rank PSI"]
    },
    {
        "q": "સાણંદમાં કેટલા PSI છે?",
        "expected_intent": "count_personnel",
        "expected_interpretation_contains": ["rank PSI", "at Sanand"]
    },
    # 2. Gender variations (stemmed/plurals)
    {
        "q": "સાણંદમાં કેટલી મહિલા કર્મચારીઓ છે?",
        "expected_intent": "count_personnel",
        "expected_interpretation_contains": ["at Sanand", "gender Female"]
    },
    # 3. Vacancy at Bopal (synonyms)
    {
        "q": "બોપલ પોલીસ સ્ટેશનમાં ખાલી જગ્યાઓ કેટલી છે?",
        "expected_intent": "station_vacancy",
        "expected_interpretation_contains": ["at Bopal"]
    },
    # 4. English rank full names
    {
        "q": "list all unarmed police constables with more than 5 years of service",
        "expected_intent": "list_personnel",
        "expected_interpretation_contains": ["rank UPC", "> 5 yrs exp"]
    },
    # 5. Age vs experience distinction
    {
        "q": "List of officers with over 10 years experience and clean record",
        "expected_intent": "list_personnel",
        "expected_interpretation_contains": ["> 10 yrs exp", "clean disciplinary record"]
    },
    {
        "q": "How many officers under 50 years of age?",
        "expected_intent": "count_personnel",
        "expected_interpretation_contains": ["< 50 yrs old"]
    },
    # 6. Clean record in Gujarati (inflections)
    {
        "q": "કોઈ પણ સજા ન મેળવેલા પીએસઆઈ ની યાદી",
        "expected_intent": "list_personnel",
        "expected_interpretation_contains": ["rank PSI", "clean disciplinary record"]
    },
    # 7. Awards in Gujarati (interchangeable vowel spelling)
    {
        "q": "સૌથી વધુ ઈનામ મેળવનાર કોણ છે?",
        "expected_intent": "top_awards",
        "expected_interpretation_contains": ["Most-awarded personnel"]
    },
    # 8. Specializations
    {
        "q": "list cyber personnel",
        "expected_intent": "list_personnel",
        "expected_interpretation_contains": ["skill CYBER"]
    },
    {
        "q": "સાયબર ગુનાના નિષ્ણાતોની યાદી આપો",
        "expected_intent": "list_personnel",
        "expected_interpretation_contains": ["skill CYBER"]
    },
    # 9. Nonsense query handling
    {
        "q": "what is the weather today",
        "expected_ok": False
    }
]

def run_tests():
    print("=== RUNNING DETAILED NLQ PARSING VERIFICATION ===")
    failures = 0
    for i, test in enumerate(tests):
        q = test["q"]
        res = engine.interpret(q)
        print(f"\nTest {i+1}: '{q}'")
        
        # Check OK status
        expected_ok = test.get("expected_ok", True)
        if res.ok != expected_ok:
            print(f"  FAIL: Expected ok={expected_ok}, got ok={res.ok}")
            failures += 1
            continue
            
        if not res.ok:
            print("  PASS: Correctly rejected.")
            continue
            
        # Check Intent
        if res.intent != test["expected_intent"]:
            print(f"  FAIL: Expected intent '{test['expected_intent']}', got '{res.intent}'")
            failures += 1
            continue
            
        # Check Interpretation Substrings
        subs = test["expected_interpretation_contains"]
        has_failed_sub = False
        for sub in subs:
            if sub not in res.interpretation:
                print(f"  FAIL: Expected interpretation to contain '{sub}', got '{res.interpretation}'")
                has_failed_sub = True
                break
        if has_failed_sub:
            failures += 1
            continue
            
        print(f"  PASS: Intent={res.intent}, Interpretation={res.interpretation}")

    print(f"\n{'='*40}")
    if failures == 0:
        print("SUCCESS: All 12 test cases passed!")
        sys.exit(0)
    else:
        print(f"FAILURE: {failures} test case(s) failed.")
        sys.exit(1)

if __name__ == "__main__":
    run_tests()
