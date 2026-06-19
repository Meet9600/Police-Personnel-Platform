"""
Local NLP Q&A engine — grounded, offline, auditable.

DESIGN: deterministic intent parser, NOT a free-text LLM emitting SQL.
For a government system this is the safer choice:
  * It can ONLY run a fixed set of parameterized query TEMPLATES.
  * Templates touch ONLY the `clean` schema and NEVER PII columns.
  * The query is fully read-only (executed via the read-only DB role).
  * Every answer shows the interpreted intent, so it is auditable.
  * Zero external dependencies — works completely offline.

An optional local-LLM hook (Ollama) can be added to map fuzzier phrasing onto
these same intents, but the EXECUTION path is always one of these safe templates;
the model is never allowed to emit raw SQL. See `interpret()` docstring.

Supports bilingual input (English or Gujarati keywords) and returns structured
results the Flask layer renders as a table.
"""
import re
import unicodedata
from app.config import query

# --- Controlled vocab loaded once from the clean store ----------------------
# (ranks, specializations, stations, divisions) — used to resolve entities in
# the user's question to ids/codes safely.


def _load_vocab():
    ranks = query("SELECT rank_code, rank_name_en, rank_band FROM clean.rank_ref")
    specs = query("SELECT spec_code, spec_name_en, category FROM clean.specialization_ref")
    stations = query("SELECT station_id, name_en, name_raw FROM clean.dim_station")
    divisions = query("SELECT division_id, name_en, name_raw FROM clean.dim_division")
    return {
        "ranks": ranks,
        "specs": specs,
        "stations": stations,
        "divisions": divisions,
    }


# Gujarati keyword synonyms for ranks / common terms (ext, not exhaustive).
GU_TERMS = {
    "પીઆઈ": "PI", "પીએસઆઈ": "PSI", "પોલીસ ઇન્સ્પેક્ટર": "PI", "કોન્સ્ટેબલ": "PC",
    "સાયબર": "CYBER", "સાઇબર": "CYBER", "ટ્રાફિક": "TRAFFIC", "મહિલા સુરક્ષા": "WOMEN_SAFETY",
    "કેટલા": "count", "કોણ": "who", "યાદી": "list", "ઇનામ": "awards", "સજા": "punishments",
    "ખાલી": "vacancy",
}

# Specialization keyword aliases (english) -> spec_code
SPEC_ALIASES = {
    "cyber": "CYBER", "traffic": "TRAFFIC", "crime": "CRIME_INVEST",
    "investigation": "CRIME_INVEST", "she team": "WOMEN_SAFETY",
    "women safety": "WOMEN_SAFETY", "child": "CHILD_WELFARE", "court": "COURT_LEGAL",
    "driver": "DRIVER", "computer": "IT_COMPUTER", "it": "IT_COMPUTER",
    "control": "CONTROL_ROOM", "wireless": "CONTROL_ROOM", "sog": "SOG",
    "commando": "COMMANDO", "gunman": "COMMANDO", "dog": "DOG_SQUAD",
    "armoury": "ARMOURY", "armory": "ARMOURY", "guard": "GUARD_SECURITY",
    "vip": "VIP_PROTECTION", "accounts": "ACCOUNTS", "patrol": "PATROL_MOBILE",
    "pso": "PSO", "registry": "REGISTRY", "store": "STORE",
}


def _norm(text):
    return unicodedata.normalize("NFC", text or "").strip()


def _lower(text):
    return _norm(text).lower()


class NLQResult:
    def __init__(self, intent, interpretation, columns, rows, sql_label, ok=True, message=None):
        self.intent = intent
        self.interpretation = interpretation     # human-readable, shown to user
        self.columns = columns
        self.rows = rows
        self.sql_label = sql_label               # which template ran (audit)
        self.ok = ok
        self.message = message

    def to_dict(self):
        return {
            "ok": self.ok, "intent": self.intent,
            "interpretation": self.interpretation, "columns": self.columns,
            "rows": self.rows, "sql_label": self.sql_label, "message": self.message,
        }


class NLQEngine:
    def __init__(self):
        self.vocab = _load_vocab()

    # ----- entity resolution (safe: maps text -> known ids/codes) -----
    # Common transliterations to help English queries match Gujarati database strings
    PLACE_ALIASES = {
        "aslali": "અસલાલી", "sanand": "સાણંદ", "viramgam": "વિરમગામ",
        "dholka": "ધોળકા", "dhandhuka": "ધંધુકા", "bopal": "બોપલ",
        "mandal": "માંડલ", "detroj": "ડેટરોજ", "bavla": "બાવળા",
        "kanbha": "કણભા", "changodar": "ચાંગોદર",
    }

    def _apply_aliases(self, text):
        t = _lower(text)
        for eng, guj in self.PLACE_ALIASES.items():
            if eng in t:
                t = t.replace(eng, guj)
        return t

    def _match_station(self, text):
        t = self._apply_aliases(text)
        # Sort stations by length descending to match longest possible string first
        best = None
        for s in self.vocab["stations"]:
            for cand in (s.get("name_en"), s.get("name_raw")):
                if not cand: continue
                c = _lower(cand)
                # Direct match: e.g. cand is "સાણંદ" and t is "how many in સાણંદ"
                if c in t:
                    if best is None or len(c) > best[1]:
                        best = (s, len(c))
                # Partial match: e.g. cand is "સાણંદ ટાઉન" and t has "સાણંદ"
                # If cand has multiple words, and the most significant word is in t
                words = c.split()
                if words and len(words[0]) > 3 and words[0] in t:
                    if best is None or len(words[0]) > best[1]:
                        best = (s, len(words[0]))
        return best[0] if best else None

    def _match_division(self, text):
        t = self._apply_aliases(text)
        for d in self.vocab["divisions"]:
            for cand in (d.get("name_en"), d.get("name_raw")):
                if not cand: continue
                c = _lower(cand)
                if c in t:
                    return d
                words = c.split()
                if words and len(words[0]) > 3 and words[0] in t:
                    return d
        return None

    def _match_rank(self, text):
        t = _lower(text)
        for r in self.vocab["ranks"]:
            code = r["rank_code"].lower()
            # match the code with an optional trailing plural 's' (PSIs, PIs)
            if re.search(r"\b" + re.escape(code) + r"s?\b", t):
                return r["rank_code"]
        for gu, gcode in GU_TERMS.items():
            if gu in text and gcode in {x["rank_code"] for x in self.vocab["ranks"]}:
                return gcode
        return None

    def _match_spec(self, text):
        t = _lower(text)
        for alias, code in SPEC_ALIASES.items():
            if re.search(r"\b" + re.escape(alias) + r"\b", t):
                return code
        for gu, code in GU_TERMS.items():
            if gu in text and any(s["spec_code"] == code for s in self.vocab["specs"]):
                return code
        return None

    # ----- main entry -----
    def interpret(self, question):
        """
        Map a natural-language question to ONE safe query template.
        """
        q = _norm(question)
        ql = _lower(q)
        station = self._match_station(q)
        division = self._match_division(q)
        rank = self._match_rank(q)
        spec = self._match_spec(q)

        # Advanced Experience extraction (English + Gujarati)
        years_match_more = re.search(r"(?:more than|>|over)\s*(\d+)\s*(?:year|વર્ષ)", ql)
        if not years_match_more: years_match_more = re.search(r"(\d+)\s*(?:year|વર્ષ)(?:થી વધુ)", q)
            
        years_match_less = re.search(r"(?:less than|<|under)\s*(\d+)\s*(?:year|વર્ષ)", ql)
        if not years_match_less: years_match_less = re.search(r"(\d+)\s*(?:year|વર્ષ)(?:થી ઓછો|થી ઓછી)", q)
            
        min_years = int(years_match_more.group(1)) if years_match_more else None
        max_years = int(years_match_less.group(1)) if years_match_less else None

        # Age extraction
        age_under = re.search(r"(?:under|<|younger than)\s*(\d+)\s*(?:year|વર્ષ)", ql)
        if not age_under: age_under = re.search(r"(\d+)\s*(?:year|વર્ષ)થી નાની", q)
        
        age_over = re.search(r"(?:over|>|older than)\s*(\d+)\s*(?:year|વર્ષ)", ql)
        if not age_over: age_over = re.search(r"(\d+)\s*(?:year|વર્ષ)થી મોટી", q)
            
        max_age = int(age_under.group(1)) if age_under else None
        min_age = int(age_over.group(1)) if age_over else None

        # Gender
        gender = None
        if re.search(r"\b(female|lady|women|woman)\b", ql) or any(x in q for x in ("સ્ત્રી", "મહિલા", "બહેન")):
            gender = 'F'
        elif re.search(r"\b(male|men|man)\b", ql) or any(x in q for x in ("પુરુષ", "ભાઈ")):
            gender = 'M'

        # Disciplinary record
        clean_record = bool(re.search(r"\b(clean record|no punishment|unpunished)\b", ql)) \
            or any(x in q for x in ("ક્લીન રેકોર્ડ", "કોઈ સજા નહિ", "સજા વગર"))

        wants_count = bool(re.search(r"\b(how many|count|number of|total|quantity)\b", ql)) \
            or any(x in q for x in ("કેટલા", "કુલ સંખ્યા"))
        wants_list = bool(re.search(r"\b(list|show|which|who|name|get|find|display|officer|personnel|all)\b", ql)) \
            or any(x in q for x in ("યાદી", "કોણ", "શોધો", "બતાવો", "બધા", "ઓફિસર", "અધિકારી", "કર્મચારી"))
        wants_awards = bool(re.search(r"\b(award|top|best|highest)\b", ql)) or "ઇનામ" in q
        wants_vacancy = bool(re.search(r"\b(vacanc|vacant|shortage|empty)\b", ql)) \
            or any(x in q for x in ("ખાલી", "જગ્યા", "ઘટ"))

        filters = {
            "min_years": min_years, "max_years": max_years,
            "min_age": min_age, "max_age": max_age,
            "gender": gender, "clean_record": clean_record
        }
        has_filter = any(v is not None and v is not False for v in filters.values())

        # --- Intent: vacancy at a station ---
        if wants_vacancy and station:
            return self._q_vacancy(station)

        # --- Intent: most awards ---
        if wants_awards and (wants_list or "most" in ql or station or has_filter):
            return self._q_top_awards(station, filters)

        # --- Intent: count of a rank (optionally at a station / division) ---
        if wants_count and (rank or station or division or has_filter):
            return self._q_count(rank, station, division, filters)

        # --- Intent: list people by specialization (optionally rank/place) ---
        if (wants_list or spec or has_filter) and (spec or rank or station or division or has_filter):
            return self._q_list(rank, spec, station, division, filters)

        # --- Fallback: count people at a place ---
        if station or division:
            return self._q_count(rank, station, division, filters)

        return NLQResult(
            intent="unknown", interpretation="", columns=[], rows=[],
            sql_label=None, ok=False,
            message=("Could not confidently interpret the question. Try e.g. "
                     "“How many female PSIs at <station>?”, “List officers in "
                     "<division> with clean record”, or “<station> માં કેટલા અધિકારીઓ છે?”"),
        )

    def _apply_filters(self, filters, joins, where, params, desc):
        if not filters: return
        if filters.get("min_years"):
            where.append("p.years_of_service > %s"); params.append(filters["min_years"])
            desc.append(f"> {filters['min_years']} yrs exp")
        if filters.get("max_years"):
            where.append("p.years_of_service < %s"); params.append(filters["max_years"])
            desc.append(f"< {filters['max_years']} yrs exp")
        if filters.get("min_age"):
            where.append("p.age_years > %s"); params.append(filters["min_age"])
            desc.append(f"> {filters['min_age']} yrs old")
        if filters.get("max_age"):
            where.append("p.age_years < %s"); params.append(filters["max_age"])
            desc.append(f"< {filters['max_age']} yrs old")
        if filters.get("gender"):
            where.append("p.gender ILIKE %s"); params.append(f"{filters['gender']}%")
            desc.append(f"gender {'Female' if filters['gender']=='F' else 'Male'}")
        if filters.get("clean_record"):
            if "person_performance perf" not in " ".join(joins):
                joins.append("LEFT JOIN clean.person_performance perf ON perf.person_id = p.person_id")
            where.append("COALESCE(perf.punishments_count, 0) = 0")
            desc.append("clean disciplinary record")

    # ----- safe parameterized templates (clean schema only, no PII) -----
    def _q_count(self, rank, station, division, filters=None):
        where, params, desc = ["p.is_active"], [], []
        joins = ["LEFT JOIN clean.dim_station s ON s.station_id = p.current_station_id"]
        if rank:
            where.append("p.rank_code = %s"); params.append(rank); desc.append(f"rank {rank}")
        if station:
            where.append("p.current_station_id = %s"); params.append(station["station_id"])
            desc.append(f"at {station['name_en']}")
        if division:
            where.append("s.division_id = %s"); params.append(division["division_id"])
            desc.append(f"in {division['name_en']} division")
        self._apply_filters(filters, joins, where, params, desc)
        
        sql = f"""
            SELECT COUNT(*) AS count
            FROM clean.person p
            {' '.join(joins)}
            WHERE {' AND '.join(where)}
        """
        rows = query(sql, params)
        return NLQResult(
            intent="count_personnel",
            interpretation="Count of personnel " + (", ".join(desc) if desc else "(all)"),
            columns=["count"], rows=rows, sql_label="count_personnel",
        )

    def _q_list(self, rank, spec, station, division, filters=None):
        where, params, desc = ["p.is_active"], [], []
        joins = ["LEFT JOIN clean.dim_station s ON s.station_id = p.current_station_id"]
        if spec:
            joins.append("JOIN clean.person_specialization ps ON ps.person_id = p.person_id")
            where.append("ps.spec_code = %s"); params.append(spec); desc.append(f"skill {spec}")
        if rank:
            where.append("p.rank_code = %s"); params.append(rank); desc.append(f"rank {rank}")
        if station:
            where.append("p.current_station_id = %s"); params.append(station["station_id"])
            desc.append(f"at {station['name_en']}")
        if division:
            where.append("s.division_id = %s"); params.append(division["division_id"])
            desc.append(f"in {division['name_en']} division")
        self._apply_filters(filters, joins, where, params, desc)
        
        sql = f"""
            SELECT DISTINCT p.person_id, p.full_name_gu AS name, p.rank_code AS rank,
                   s.name_en AS station, p.years_of_service
            FROM clean.person p
            {' '.join(joins)}
            WHERE {' AND '.join(where)}
            ORDER BY p.rank_code, p.person_id
            LIMIT 200
        """
        rows = query(sql, params)
        return NLQResult(
            intent="list_personnel",
            interpretation="Personnel " + (", ".join(desc) if desc else "(all)"),
            columns=["person_id", "name", "rank", "station", "years_of_service"],
            rows=rows, sql_label="list_personnel",
        )

    def _q_top_awards(self, station, filters=None):
        where, params, desc = ["p.is_active", "perf.awards_count > 0"], [], []
        joins = [
            "JOIN clean.person_performance perf ON perf.person_id = p.person_id",
            "LEFT JOIN clean.dim_station s ON s.station_id = p.current_station_id"
        ]
        if station:
            where.append("p.current_station_id = %s"); params.append(station["station_id"])
            desc.append(f"at {station['name_en']}")
        self._apply_filters(filters, joins, where, params, desc)
        
        sql = f"""
            SELECT p.person_id, p.full_name_gu AS name, p.rank_code AS rank,
                   perf.awards_count, s.name_en AS station
            FROM clean.person p
            {' '.join(joins)}
            WHERE {' AND '.join(where)}
            ORDER BY perf.awards_count DESC, p.person_id
            LIMIT 50
        """
        rows = query(sql, params)
        return NLQResult(
            intent="top_awards",
            interpretation="Most-awarded personnel " + (", ".join(desc) if desc else "(force-wide)"),
            columns=["person_id", "name", "rank", "awards_count", "station"],
            rows=rows, sql_label="top_awards",
        )

    def _q_vacancy(self, station):
        sql = """
            SELECT s.name_en AS station, c.rank_band,
                   c.approved_total, c.present_total, c.vacancy
            FROM clean.station_capacity c
            JOIN clean.dim_station s ON s.station_id = c.station_id
            WHERE c.station_id = %s
            ORDER BY c.rank_band
        """
        rows = query(sql, [station["station_id"]])
        return NLQResult(
            intent="station_vacancy",
            interpretation=f"Approved vs present strength at {station['name_en']}",
            columns=["station", "rank_band", "approved_total", "present_total", "vacancy"],
            rows=rows, sql_label="station_vacancy",
        )


# module-level singleton, lazily built
_engine = None


def get_engine():
    global _engine
    if _engine is None:
        _engine = NLQEngine()
    return _engine


def ask(question):
    return get_engine().interpret(question).to_dict()
