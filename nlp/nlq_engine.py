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
import difflib
from app.config import query

import json
import logging
try:
    from llama_cpp import Llama
    _llm = None
except ImportError:
    Llama = None

def get_llm():
    global _llm
    if _llm is not None:
        return _llm
    if Llama is None:
        return None
    try:
        # Load the local model downloaded previously
        _llm = Llama(
            model_path="models/qwen2.5-3b-instruct-q4_k_m.gguf",
            n_ctx=1024,
            verbose=False
        )
    except Exception as e:
        logging.error(f"Failed to load LLM: {e}")
        return None
    return _llm


# --- Controlled vocab loaded once from the clean store ----------------------
# (ranks, specializations, stations, divisions) — used to resolve entities in
# the user's question to ids/codes safely.


def _load_vocab():
    ranks = query("SELECT rank_code, rank_name_en, rank_band FROM clean.rank_ref")
    specs = query("SELECT spec_code, spec_name_en, category FROM clean.specialization_ref")
    stations = query("SELECT station_id, name_en, name_raw FROM clean.dim_station")
    divisions = query("SELECT division_id, name_en, name_raw FROM clean.dim_division")
    import json
    try:
        with open("scratch/stations_dump.json", "w") as f:
            json.dump([dict(s) for s in stations], f, indent=2, default=str)
    except Exception as e:
        print(f"Failed to dump stations: {e}")
    
    return {
        "ranks": ranks,
        "specs": specs,
        "stations": stations,
        "divisions": divisions,
    }


def _norm(text):
    return unicodedata.normalize("NFC", text or "").strip()


def _lower(text):
    return _norm(text).lower()


def _normalize_guj(text):
    if not text:
        return ""
    # Standardize interchangeable vowels and character combinations
    text = unicodedata.normalize("NFC", text)
    text = text.replace("ઇ", "ઈ").replace("િ", "ી")
    text = text.replace("ઉ", "ઊ").replace("ુ", "ૂ")
    text = text.replace("સાઇ", "સાય")
    text = text.replace("આે", "ઓ")
    return text


def _guj_stem(word):
    if not word:
        return ""
    # Common postpositions, suffixes, and plural markers in Gujarati (longest first)
    suffixes = [
        "વાળાં", "વાળું", "વાળી", "વાળા",
        "માં", "થી", "ને", "ની", "ના", "નું", "નાં", "નો", "ઓ", "આે", "વાર"
    ]
    changed = True
    while changed:
        changed = False
        for suff in suffixes:
            if word.endswith(suff) and len(word) > len(suff):
                word = word[:-len(suff)]
                changed = True
                break
        # Also remove vowel sign plural markers ો, ોં, ાં
        for suff in ["ોં", "ો", "ાં"]:
            if word.endswith(suff) and len(word) > len(suff):
                word = word[:-len(suff)]
                changed = True
                break
    return word


def _english_singular(word):
    if word.isalpha() and word.endswith('s') and len(word) > 2:
        if word not in ["is", "as", "us", "yes", "this", "class", "status", "news"]:
            return word[:-1]
    return word


def _similarity(s1, s2):
    return difflib.SequenceMatcher(None, s1, s2).ratio()


def _clean_word(w):
    return w.strip('!"#$%&\'()*+,-./:;<=>?@[\\]^_`{|}~?')


def _tokenize(text):
    if not text:
        return []
    text = str(text).replace('.', '')
    normalized = _normalize_guj(_lower(text))
    tokens = []
    for w in normalized.split():
        cleaned = _clean_word(w)
        if cleaned:
            tokens.append(cleaned)
    return tokens


def _stem_tokens(tokens):
    stemmed = []
    for t in tokens:
        if t.isascii():
            stemmed.append(_english_singular(t))
        else:
            stemmed.append(_guj_stem(t))
    return [w for w in stemmed if w]


def _match_phrase_in_tokens(phrase_tokens, query_tokens):
    if not phrase_tokens or not query_tokens:
        return False
    n_p = len(phrase_tokens)
    n_q = len(query_tokens)
    for i in range(n_q - n_p + 1):
        if query_tokens[i:i+n_p] == phrase_tokens:
            return True
    return False


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
        # Core aliases
        "aslali": "અસલાલી", "sanand": "સાણંદ", "viramgam": "વિરમગામ",
        "dholka": "ધોળકા", "dhandhuka": "ધંધુકા", "bopal": "બોપલ",
        "mandal": "માંડલ", "detroj": "ડેટરોજ", "bavla": "બાવળા",
        "kanbha": "કણભા", "changodar": "ચાંગોદર",
        # Common station words
        "gidc": "જીઆઇડીસી", "g.i.d.c.": "જીઆઇડીસી",
        "rural": "રૂરલ", "town": "ટાઉન",
        "head": "હેડ", "quarter": "ક્વાર્ટર", "hq": "હેડ ક્વાર્ટર", "headquarters": "હેડ ક્વાર્ટર",
        "police": "પોલીસ", "station": "સ્ટેશન", "po": "પો", "ste": "સ્ટે",
        "reader": "રીડર", "cpi": "સીપીઆઇ", "c.p.i.": "સીપીઆઇ",
        # Extreme transliteration misspellings mapping to correct English
        "sannad": "sanand", "sanad": "sanand", "saanand": "sanand",
        "bopl": "bopal", "boopal": "bopal",
        "virmgam": "viramgam", "viramgaam": "viramgam",
        "ahmedabad": "ahmedabad", "ahmdabad": "ahmedabad", "amdavad": "ahmedabad",
        "dolka": "dholka", "dholkaa": "dholka",
        # Ranks
        "inspector": "ઇન્સ્પેક્ટર", "sub": "સબ", "assistant": "મદદનીશ",
        "constable": "કોન્સ્ટેબલ", "jamadar": "જમાદાર", "armed": "સશસ્ત્ર", "unarmed": "નિઃશસ્ત્ર",
        "lokrakshak": "લોકરક્ષક", "lr": "લોકરક્ષક", "pi": "પીઆઈ", "psi": "પીએસઆઈ", "asi": "એએસઆઈ",
        # Specializations
        "cyber": "સાયબર", "traffic": "ટ્રાફિક", "crime": "ક્રાઈમ", "investigation": "તપાસ",
        "murder": "ખૂન", "theft": "ચોરી", "robbery": "લૂંટ", "women": "મહિલા", "safety": "સુરક્ષા",
        "child": "બાળ", "welfare": "કલ્યાણ", "court": "કોર્ટ", "summons": "સમન્સ", "warrant": "વોરંટ",
        "driver": "ડ્રાઈવર", "driving": "ડ્રાઈવર", "car": "ગાડી", "vehicle": "વાહન",
        "computer": "કમ્પ્યુટર", "it": "આઇટી", "software": "સોફ્ટવેર", "hardware": "હાર્ડવેર",
        "control": "કંટ્રોલ", "wireless": "વાયરલેસ", "radio": "રેડિયો",
        "sog": "એસઓજી", "commando": "કમાન્ડો", "gunman": "ગનમેન", "dog": "ડોગ", "squad": "સ્ક્વોડ",
        "armoury": "આર્મોરી", "guard": "ગાર્ડ", "vip": "વીઆઈપી"
    }

    def _apply_aliases(self, text):
        t = _lower(text)
        for eng in sorted(self.PLACE_ALIASES.keys(), key=len, reverse=True):
            guj = self.PLACE_ALIASES[eng]
            if eng in t:
                t = t.replace(eng, guj)
        return t

    def _match_station(self, text):
        raw_stemmed = _stem_tokens(_tokenize(text))
        stemmed_tokens = []
        for t in raw_stemmed:
            aliased = self._apply_aliases(t)
            stemmed_tokens.extend(_tokenize(aliased))

        best_score = 0.0
        matches = []

        for s in self.vocab["stations"]:
            name_en = _lower(s.get("name_en") or "")
            name_gu = _normalize_guj(_lower(s.get("name_raw") or ""))
            
            vocab_tokens_en = _stem_tokens(_tokenize(name_en))
            vocab_tokens_gu = _stem_tokens(_tokenize(name_gu))
            
            # 1. Exact match of the entire vocab name in stemmed tokens
            if _match_phrase_in_tokens(vocab_tokens_en, stemmed_tokens) or \
               _match_phrase_in_tokens(vocab_tokens_gu, stemmed_tokens):
                score = 1.0 + len(vocab_tokens_en) / 100.0
                matches.append((score, s))
                if score > best_score: best_score = score
                continue

            # 2. Fuzzy match word by word (aggregate score for all query tokens)
            if stemmed_tokens:
                total_sim_en = sum(max([_similarity(t, v) for v in vocab_tokens_en] + [0]) for t in stemmed_tokens)
                avg_sim_en = total_sim_en / len(stemmed_tokens) if stemmed_tokens else 0.0

                total_sim_gu = sum(max([_similarity(t, v) for v in vocab_tokens_gu] + [0]) for t in stemmed_tokens)
                avg_sim_gu = total_sim_gu / len(stemmed_tokens) if stemmed_tokens else 0.0

                avg_sim = max(avg_sim_en, avg_sim_gu)
                if avg_sim > 0.70:
                    # Penalize slightly for extra unmatched words in the vocab
                    penalty = 0.01 * max(0, min(len(vocab_tokens_en), len(vocab_tokens_gu)) - len(stemmed_tokens))
                    score = avg_sim - penalty
                    
                    if name_en.startswith(stemmed_tokens[0]) or name_gu.startswith(stemmed_tokens[0]):
                        score += 0.02

                    matches.append((score, s))
                    if score > best_score: best_score = score

        if not matches:
            return [], 0.0
            
        # Return all stations that are within 0.05 of the best score
        best_stations = [s for score, s in matches if best_score - score < 0.05]
        return best_stations, best_score

    def _match_division(self, text):
        raw_stemmed = _stem_tokens(_tokenize(text))
        stemmed_tokens = []
        for t in raw_stemmed:
            aliased = self._apply_aliases(t)
            stemmed_tokens.extend(_tokenize(aliased))

        best_division = None
        best_score = 0.0

        for d in self.vocab["divisions"]:
            name_en = _lower(d.get("name_en") or "")
            name_gu = _normalize_guj(_lower(d.get("name_raw") or ""))
            
            # Clean/strip division suffixes from vocab
            name_en_clean = name_en.replace("division", "").strip()
            name_gu_clean = name_gu.replace("ડિવિઝન", "").strip()
            
            vocab_tokens_en = _stem_tokens(_tokenize(name_en_clean))
            vocab_tokens_gu = _stem_tokens(_tokenize(name_gu_clean))
            
            if _match_phrase_in_tokens(vocab_tokens_en, stemmed_tokens) or \
               _match_phrase_in_tokens(vocab_tokens_gu, stemmed_tokens):
                score = 1.0 + len(vocab_tokens_en) / 100.0
                if score > best_score:
                    best_score = score
                    best_division = d
                    continue

            if stemmed_tokens:
                total_sim_en = sum(max([_similarity(t, v) for v in vocab_tokens_en] + [0]) for t in stemmed_tokens)
                avg_sim_en = total_sim_en / len(stemmed_tokens) if stemmed_tokens else 0.0

                total_sim_gu = sum(max([_similarity(t, v) for v in vocab_tokens_gu] + [0]) for t in stemmed_tokens)
                avg_sim_gu = total_sim_gu / len(stemmed_tokens) if stemmed_tokens else 0.0

                avg_sim = max(avg_sim_en, avg_sim_gu)
                if avg_sim > 0.70:
                    score = avg_sim
                    if score > best_score:
                        best_score = score
                        best_division = d

        return best_division, best_score

    def _match_rank(self, text):
        tokens = _tokenize(text)
        stemmed_tokens = _stem_tokens(tokens)
        
        # Rank mappings defined as tokenized lists (consolidated for duplicate codes)
        rank_mapping = {
            "PI": [["pi"], ["police", "inspector"], ["inspector"], ["પીઆઈ"], ["પોલીસ", "ઇન્સ્પેક્ટર"], ["ઇન્સ્પેક્ટર"], ["ઈન્સ્પેક્ટર"]],
            "PSI": [["psi"], ["police", "sub", "inspector"], ["police", "sub-inspector"], ["sub", "inspector"], ["sub-inspector"], ["પીએસઆઈ"], ["પોલીસ", "સબ", "ઇન્સ્પેક્ટર"], ["પોલીસ", "સબ", "ઈન્સ્પેક્ટર"], ["સબ", "ઇન્સ્પેક્ટર"], ["સબ", "ઈન્સ્પેક્ટર"]],
            "AASI": [["aasi"], ["armed", "assistant", "sub", "inspector"], ["armed", "assistant", "sub-inspector"], ["એએએસઆઈ"], ["સશસ્ત્ર", "મદદનીશ", "સબ", "ઇન્સ્પેક્ટર"]],
            "UASI": [
                ["uasi"], ["unarmed", "assistant", "sub", "inspector"], ["unarmed", "assistant", "sub-inspector"], ["યુએએસઆઈ"], ["નિઃશસ્ત્ર", "મદદનીશ", "સબ", "ઇન્સ્પેક્ટર"],
                ["asi"], ["assistant", "sub", "inspector"], ["assistant", "sub-inspector"], ["એએસઆઈ"], ["મદદનીશ", "સબ", "ઇન્સ્પેક્ટર"]
            ],
            "AHC": [["ahc"], ["armed", "head", "constable"], ["સશસ્ત્ર", "હેડ", "કોન્સ્ટેબલ"]],
            "UHC": [
                ["uhc"], ["unarmed", "head", "constable"], ["નિઃશસ્ત્ર", "હેડ", "કોન્સ્ટેબલ"],
                ["hc"], ["head", "constable"], ["jamadar"], ["હેડ", "કોન્સ્ટેબલ"], ["જમાદાર"]
            ],
            "APC": [["apc"], ["armed", "police", "constable"], ["armed", "constable"], ["સશસ્ત્ર", "પોલીસ", "કોન્સ્ટેબલ"], ["સશસ્ત્ર", "કોન્સ્ટેબલ"]],
            "UPC": [
                ["upc"], ["unarmed", "police", "constable"], ["unarmed", "constable"], ["નિઃશસ્ત્ર", "પોલીસ", "કોન્સ્ટેબલ"], ["નિઃશસ્ત્ર", "કોન્સ્ટેબલ"],
                ["pc"], ["constable"], ["police", "constable"], ["કોન્સ્ટેબલ"], ["પોલીસ", "કોન્સ્ટેબલ"]
            ],
            "ALR": [["alr"], ["armed", "lokrakshak"], ["Armed", "lr"], ["સશસ્ત્ર", "લોકરક્ષક"]],
            "ULR": [
                ["ulr"], ["unarmed", "lokrakshak"], ["unarmed", "lr"], ["નિઃશસ્ત્ર", "લોકરક્ષક"],
                ["lr"], ["lokrakshak"], ["lok", "rakshak"], ["લોકરક્ષક"]
            ],
        }

        best_rank = None
        best_score = 0.0

        if not stemmed_tokens:
            return None

        # Fuzzy match over rank phrases
        for rank_code, phrases in rank_mapping.items():
            for p in phrases:
                p_stemmed = _stem_tokens(p)
                
                # Exact subset match logic (bonus)
                if _match_phrase_in_tokens(p_stemmed, stemmed_tokens):
                    score = 1.0 + len(p_stemmed) / 100.0
                    if score > best_score:
                        best_score = score
                        best_rank = rank_code
                    continue
                
                total_sim = sum(max([_similarity(t, v) for v in p_stemmed] + [0]) for t in stemmed_tokens)
                avg_sim = total_sim / len(stemmed_tokens) if stemmed_tokens else 0.0
                
                if avg_sim > 0.70:
                    score = avg_sim
                    if score > best_score:
                        best_score = score
                        best_rank = rank_code

        if best_rank:
            return best_rank

        for r in self.vocab["ranks"]:
            code = r["rank_code"].lower()
            if code in tokens:
                return r["rank_code"]

        return None

    def _match_spec(self, text):
        tokens = _tokenize(text)
        stemmed_tokens = _stem_tokens(tokens)

        # Specialization mappings as tokenized lists (Massive Semantic Expansion)
        spec_mapping = {
            "CYBER": [
                ["cyber"], ["સાઇબર"], ["સાયબર"], ["cyber", "crime"],
                ["hacker"], ["hacking"], ["phishing"], ["online", "fraud"], ["internet"], ["સોશિયલ", "મીડિયા"], ["ઇન્ટરનેટ"]
            ],
            "TRAFFIC": [
                ["traffic"], ["ટ્રાફિક"], ["accident"], ["highway"], ["road"], ["અકસ્માત"], ["રસ્તો"]
            ],
            "CRIME_INVEST": [
                ["crime"], ["investigation"], ["ક્રાઈમ"], ["તપાસ"],
                ["murder"], ["theft"], ["robbery"], ["stolen"], ["killer"], ["homicide"], ["ખૂન"], ["ચોરી"], ["લૂંટ"], ["ગુનો"]
            ],
            "WOMEN_SAFETY": [
                ["she", "team"], ["women", "safety"], ["female", "safety"], ["મહિલા", "સુરક્ષા"], ["શી", "ટીમ"], ["બહેનો", "સુરક્ષા"],
                ["harassment"], ["domestic", "violence"], ["છેડતી"]
            ],
            "CHILD_WELFARE": [
                ["child"], ["child", "welfare"], ["spc"], ["બાળ", "કલ્યાણ"], ["ચાઇલ્ડ", "વેલફેર"], ["kid"], ["minor"], ["બાળક"]
            ],
            "COURT_LEGAL": [
                ["court"], ["summons"], ["warrant"], ["કોર્ટ"], ["સમન્સ"], ["વોરંટ"], ["legal"], ["lawyer"], ["judge"], ["કાયદો"]
            ],
            "DRIVER": [
                ["driver"], ["ડ્રાઈવર"], ["driving"], ["car"], ["vehicle"], ["ગાડી"], ["વાહન"]
            ],
            "IT_COMPUTER": [
                ["computer"], ["it"], ["કોમ્પ્યુટર"], ["કમ્પ્યુટર"], ["software"], ["hardware"], ["typist"], ["સોફ્ટવેર"]
            ],
            "CONTROL_ROOM": [
                ["control"], ["wireless"], ["કંટ્રોલ"], ["વાયરલેસ"], ["radio"], ["dispatch"], ["રેડિયો"]
            ],
            "SOG": [
                ["sog"], ["special", "operations"], ["એસઓજી"], ["anti", "terror"], ["special", "force"]
            ],
            "COMMANDO": [
                ["commando"], ["gunman"], ["કમાન્ડો"], ["ગનમેન"], ["shooter"], ["sniper"], ["હથિયારધારી"]
            ],
            "DOG_SQUAD": [
                ["dog"], ["dog", "squad"], ["ડોગ"], ["ડોગ", "સ્ક્વોડ"], ["k9"], ["sniffer"]
            ],
            "ARMOURY": [
                ["armoury"], ["armory"], ["આર્મોરી"], ["હથિયાર"], ["weapons"], ["guns"], ["દારૂગોળો"]
            ],
            "GUARD_SECURITY": [
                ["guard"], ["security"], ["ગાર્ડ"], ["સુરક્ષા"], ["watchman"], ["ચોકીદાર"]
            ],
            "VIP_PROTECTION": [
                ["vip"], ["bungalow"], ["બંગલો"], ["minister", "security"], ["protection"]
            ],
            "ACCOUNTS": [
                ["accounts"], ["એકાઉન્ટ"], ["હિસાબ"], ["finance"], ["salary"], ["pay"], ["પગાર"], ["નાણાં"]
            ],
            "PATROL_MOBILE": [
                ["patrol"], ["mobile", "patrol"], ["પેટ્રોલ"], ["મોબાઈલ", "પેટ્રોલ"], ["beat"], ["night", "round"]
            ],
            "PSO": [
                ["pso"], ["પીએસઓ"], ["station", "officer"]
            ],
            "REGISTRY": [
                ["registry"], ["રજીસ્ટ્રી"], ["records"], ["files"], ["દસ્તાવેજ"]
            ],
            "STORE": [
                ["store"], ["સ્ટોર"], ["inventory"], ["supplies"], ["સામાન"]
            ],
        }

        best_spec = None
        best_score = 0.0

        if not stemmed_tokens:
            return None

        # Fuzzy match over specialization phrases
        for spec_code, phrases in spec_mapping.items():
            for p in phrases:
                p_stemmed = _stem_tokens(p)
                
                # Exact subset match logic (bonus)
                if _match_phrase_in_tokens(p_stemmed, stemmed_tokens):
                    score = 1.0 + len(p_stemmed) / 100.0
                    if score > best_score:
                        best_score = score
                        best_spec = spec_code
                    continue
                
                total_sim = sum(max([_similarity(t, v) for v in p_stemmed] + [0]) for t in stemmed_tokens)
                avg_sim = total_sim / len(stemmed_tokens) if stemmed_tokens else 0.0
                
                if avg_sim > 0.70:
                    score = avg_sim
                    if score > best_score:
                        best_score = score
                        best_spec = spec_code

        if best_spec:
            if best_spec == "IT_COMPUTER" and ["it"] in spec_mapping["IT_COMPUTER"]:
                if "it" not in tokens:
                    # Ignore if "it" wasn't explicitly mentioned as a word
                    pass
            return best_spec

        for s in self.vocab["specs"]:
            code = s["spec_code"].lower()
            if code in tokens:
                return s["spec_code"]

        return None

    # ----- main entry -----
    def interpret(self, question):
        """
        Map a natural-language question to ONE safe query template using local LLM.
        """
        llm = get_llm()
        if not llm:
            # Fallback if LLM is not loaded
            return NLQResult(
                intent="unknown", interpretation="", columns=[], rows=[], sql_label=None, ok=False,
                message="Offline LLM model not loaded. Please check logs."
            )

        # Prepare context for the LLM
        prompt = f"""<|im_start|>system
You are an AI assistant that extracts information from police queries (English or Gujarati) into JSON.
You must return ONLY a JSON object with these exact keys:
- "intent": one of ["station_vacancy", "top_awards", "count_personnel", "list_personnel"]
- "rank": The exact rank mentioned (e.g. PSI, PI, ASI) or null
- "station": The police station mentioned or null
- "division": The division mentioned or null
- "spec": The specialization mentioned (e.g. Cyber, Traffic) or null
- "filters": A dict with "clean_record" (bool), "is_past_posting" (bool), and "min_years" (int) or "max_years" (int), or null.

Examples:
"Show all cyber officers in Ahmedabad Rural" -> {{"intent": "list_personnel", "spec": "Cyber", "station": "Ahmedabad Rural", "rank": null, "division": null, "filters": null}}
"How many PIs are vacant in Sanand Town?" -> {{"intent": "station_vacancy", "rank": "PI", "station": "Sanand Town", "spec": null, "division": null, "filters": null}}
"Who used to work in traffic at Mandal?" -> {{"intent": "list_personnel", "spec": "Traffic", "station": "Mandal", "rank": null, "division": null, "filters": {{"is_past_posting": true}}}}
"GIDC officers list" -> {{"intent": "list_personnel", "spec": null, "station": "GIDC", "rank": null, "division": null, "filters": null}}
"list officers is in sanand GIDC" -> {{"intent": "list_personnel", "spec": null, "station": "Sanand GIDC", "rank": null, "division": null, "filters": null}}
"Constables with more than 5 years experience in Dholka Rural" -> {{"intent": "list_personnel", "spec": null, "station": "Dholka Rural", "rank": "Constable", "division": null, "filters": {{"min_years": 5}}}}
"List all ASIs in Viramgam division" -> {{"intent": "list_personnel", "spec": null, "station": null, "rank": "ASI", "division": "Viramgam", "filters": null}}
"Show officers in Viramgam reader" -> {{"intent": "list_personnel", "spec": null, "station": "Viramgam reader", "rank": null, "division": null, "filters": null}}
<|im_end|>
<|im_start|>user
{question}
<|im_end|>
<|im_start|>assistant
"""
        try:
            response = llm(prompt, max_tokens=150, stop=["<|im_end|>"], echo=False)
            output = response['choices'][0]['text'].strip()
            # Clean up output to ensure it's just JSON
            if "```json" in output:
                output = output.split("```json")[1].split("```")[0].strip()
            elif "```" in output:
                output = output.split("```")[1].strip()
                
            data = json.loads(output)
            
            # Map LLM raw strings back to our DB dictionaries
            q_stemmed = set(_stem_tokens(_tokenize(question)))
            q_stemmed_aliased = set()
            for t in q_stemmed:
                q_stemmed_aliased.add(self._apply_aliases(t))
                q_stemmed_aliased.add(t)

            def _anti_hallucinate(extracted_str):
                if not extracted_str: return None
                ext_stemmed = set(_stem_tokens(_tokenize(str(extracted_str))))
                for t in ext_stemmed:
                    if t in q_stemmed_aliased or self._apply_aliases(t) in q_stemmed_aliased:
                        return extracted_str
                return None

            rank_str = _anti_hallucinate(data.get("rank"))
            station_str = _anti_hallucinate(data.get("station"))
            div_str = _anti_hallucinate(data.get("division"))
            spec_str = _anti_hallucinate(data.get("spec"))
            intent = data.get("intent", "list_personnel")
            filters = data.get("filters") or {}

            # Anti-hallucinate is_past_posting
            if filters and filters.get("is_past_posting"):
                past_words = {"past", "previous", "used", "former", "earlier", "was", "worked", "old"}
                if not past_words.intersection(q_stemmed):
                    filters["is_past_posting"] = False

            # Regex fallback for years of experience to prevent LLM misses
            import re
            if "min_years" not in filters or filters["min_years"] is None:
                m_min = re.search(r'(?:more than|>|over)\s*(\d+)\s*year', question, re.IGNORECASE)
                if m_min:
                    filters["min_years"] = int(m_min.group(1))
            
            if "max_years" not in filters or filters["max_years"] is None:
                m_max = re.search(r'(?:less than|<|under)\s*(\d+)\s*year', question, re.IGNORECASE)
                if m_max:
                    filters["max_years"] = int(m_max.group(1))

            rank = self._match_rank(rank_str) if rank_str else None
            stations, _ = self._match_station(station_str) if station_str else ([], 0)
            division, _ = self._match_division(div_str) if div_str else (None, 0)
            spec = self._match_spec(spec_str) if spec_str else None

            if intent == "station_vacancy":
                return self._q_vacancy(stations, division)
            elif intent == "top_awards":
                return self._q_top_awards(stations, division, filters)
            elif intent == "count_personnel":
                return self._q_count(rank, stations, division, filters)
            else:
                is_past = filters.get("is_past_posting", False) if filters else False
                return self._q_list(rank, spec, stations, division, filters, is_past_posting=is_past)
                
        except Exception as e:
            logging.error(f"LLM Error: {e}")
            return NLQResult(
                intent="unknown", interpretation="", columns=[], rows=[], sql_label=None, ok=False,
                message=f"LLM failed to process query: {e}"
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
    def _q_count(self, rank, stations, division, filters=None):
        where, params, desc = ["p.is_active"], [], []
        joins = ["LEFT JOIN clean.dim_station s ON s.station_id = p.current_station_id"]
        if rank:
            where.append("p.rank_code = %s"); params.append(rank); desc.append(f"rank {rank}")
        if stations:
            station_ids = tuple(s["station_id"] for s in stations)
            names_en = " or ".join(s["name_en"] for s in stations)
            where.append("p.current_station_id IN %s"); params.append(station_ids)
            desc.append(f"at {names_en}")
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

    def _q_list(self, rank, spec, stations, division, filters=None, is_past_posting=False):
        where, params, desc = ["p.is_active"], [], []
        joins = ["LEFT JOIN clean.dim_station s ON s.station_id = p.current_station_id"]
        if spec:
            joins.append("JOIN clean.person_specialization ps ON ps.person_id = p.person_id")
            where.append("ps.spec_code = %s"); params.append(spec); desc.append(f"skill {spec}")
        if rank:
            where.append("p.rank_code = %s"); params.append(rank); desc.append(f"rank {rank}")
        
        if stations:
            station_ids = tuple(s["station_id"] for s in stations)
            names_en = " or ".join(s["name_en"] for s in stations)
            if is_past_posting:
                joins.append("JOIN clean.person_posting_history pph ON pph.person_id = p.person_id")
                
                # To handle multiple stations in past postings: (place_en ILIKE %s OR place_en ILIKE %s ...)
                like_clauses = []
                for s in stations:
                    like_clauses.extend(["pph.place_en ILIKE %s", "pph.place_raw ILIKE %s"])
                    params.extend([f"%{s['name_en']}%", f"%{s['name_en']}%"])
                
                where.append(f"({' OR '.join(like_clauses)})")
                where.append("p.current_station_id NOT IN %s"); params.append(station_ids)
                desc.append(f"transferred from {names_en}")
            else:
                where.append("p.current_station_id IN %s"); params.append(station_ids)
                desc.append(f"at {names_en}")
                
        if division:
            if is_past_posting:
                if not stations: # Only join if not already joined
                    joins.append("JOIN clean.person_posting_history pph ON pph.person_id = p.person_id")
                where.append("(pph.place_en ILIKE %s OR pph.place_raw ILIKE %s)"); 
                params.extend([f"%{division['name_en']}%", f"%{division['name_en']}%"])
                where.append("s.division_id != %s"); params.append(division["division_id"])
                desc.append(f"transferred from {division['name_en']} division")
            else:
                where.append("s.division_id = %s"); params.append(division["division_id"])
                desc.append(f"in {division['name_en']} division")
                
        self._apply_filters(filters, joins, where, params, desc)
        
        joins.append("LEFT JOIN clean.rank_ref r ON p.rank_code = r.rank_code")
        sql = f"""
            SELECT DISTINCT p.display_id AS buckle_no, p.full_name_gu AS name, p.rank_code AS rank,
                   s.name_en AS station, p.years_of_service, r.rank_order
            FROM clean.person p
            {' '.join(joins)}
            WHERE {' AND '.join(where)}
            ORDER BY r.rank_order DESC, p.display_id
            LIMIT 200
        """
        rows = query(sql, params)
        return NLQResult(
            intent="list_personnel",
            interpretation="Personnel " + (", ".join(desc) if desc else "(all)"),
            columns=["buckle_no", "name", "rank", "station", "years_of_service"],
            rows=rows, sql_label="list_personnel",
        )

    def _q_top_awards(self, stations, division=None, filters=None):
        where, params, desc = ["p.is_active", "perf.awards_count > 0"], [], []
        joins = [
            "JOIN clean.person_performance perf ON perf.person_id = p.person_id",
            "LEFT JOIN clean.dim_station s ON s.station_id = p.current_station_id",
            "LEFT JOIN clean.rank_ref r ON p.rank_code = r.rank_code"
        ]
        if stations:
            station_ids = tuple(s["station_id"] for s in stations)
            names_en = " or ".join(s["name_en"] for s in stations)
            where.append("p.current_station_id IN %s"); params.append(station_ids)
            desc.append(f"at {names_en}")
        elif division:
            where.append("s.division_id = %s"); params.append(division["division_id"])
            desc.append(f"in {division['name_en']} division")
        self._apply_filters(filters, joins, where, params, desc)
        
        sql = f"""
            SELECT p.person_id, p.full_name_gu AS name, p.rank_code AS rank,
                   perf.awards_count, s.name_en AS station, r.rank_order
            FROM clean.person p
            {' '.join(joins)}
            WHERE {' AND '.join(where)}
            ORDER BY r.rank_order DESC, perf.awards_count DESC, p.person_id
            LIMIT 50
        """
        rows = query(sql, params)
        return NLQResult(
            intent="top_awards",
            interpretation="Most-awarded personnel " + (", ".join(desc) if desc else "(force-wide)"),
            columns=["person_id", "name", "rank", "awards_count", "station"],
            rows=rows, sql_label="top_awards",
        )

    def _q_vacancy(self, stations, division=None):
        if stations:
            station_ids = tuple(s["station_id"] for s in stations)
            names_en = " or ".join(s["name_en"] for s in stations)
            sql = """
                SELECT s.name_en AS station, c.rank_band,
                       c.approved_total, c.present_total, c.vacancy
                FROM clean.station_capacity c
                JOIN clean.dim_station s ON s.station_id = c.station_id
                WHERE c.station_id IN %s
                ORDER BY s.name_en, c.rank_band
            """
            rows = query(sql, [station_ids])
            return NLQResult(
                intent="station_vacancy",
                interpretation=f"Approved vs present strength at {names_en}",
                columns=["station", "rank_band", "approved_total", "present_total", "vacancy"],
                rows=rows, sql_label="station_vacancy",
            )
        else:
            sql = """
                SELECT s.name_en AS station, c.rank_band,
                       c.approved_total, c.present_total, c.vacancy
                FROM clean.station_capacity c
                JOIN clean.dim_station s ON s.station_id = c.station_id
                WHERE s.division_id = %s
                ORDER BY s.name_en, c.rank_band
            """
            rows = query(sql, [division["division_id"]])
            return NLQResult(
                intent="station_vacancy",
                interpretation=f"Approved vs present strength in {division['name_en']} division",
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

if __name__ == "__main__":
    import sys
    import json
    
    # Configure basic logging to see LLM loading errors if any
    logging.basicConfig(level=logging.INFO)
    
    if len(sys.argv) > 1:
        q = " ".join(sys.argv[1:])
    else:
        q = "Show all cyber officers in Ahmedabad"
        
    print(f"Question: {q}")
    res = ask(q)
    print(json.dumps(res, indent=2, default=str))
