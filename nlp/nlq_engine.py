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
        # Extreme transliteration misspellings mapping to correct English
        "sannad": "sanand", "sanad": "sanan", "saanand": "sanand",
        "bopl": "bopal", "boopal": "bopal",
        "virmgam": "viramgam", "viramgaam": "viramgam",
        "ahmedabad": "ahmedabad", "ahmdabad": "ahmedabad", "amdavad": "ahmedabad",
        "dolka": "dholka", "dholkaa": "dholka",
    }

    def _apply_aliases(self, text):
        t = _lower(text)
        for eng, guj in self.PLACE_ALIASES.items():
            if eng in t:
                t = t.replace(eng, guj)
        return t

    def _match_station(self, text):
        tokens = _tokenize(text)
        stemmed_tokens = _stem_tokens(tokens)
        stemmed_tokens = [self._apply_aliases(t) for t in stemmed_tokens]

        best_station = None
        best_score = 0.0

        for s in self.vocab["stations"]:
            name_en = _lower(s.get("name_en") or "")
            name_gu = _normalize_guj(_lower(s.get("name_raw") or ""))
            
            vocab_tokens_en = _stem_tokens(_tokenize(name_en))
            vocab_tokens_gu = _stem_tokens(_tokenize(name_gu))
            
            # 1. Exact match of the entire vocab name in stemmed tokens
            if _match_phrase_in_tokens(vocab_tokens_en, stemmed_tokens) or \
               _match_phrase_in_tokens(vocab_tokens_gu, stemmed_tokens):
                score = 1.0 + len(vocab_tokens_en) / 100.0
                if score > best_score:
                    best_score = score
                    best_station = s
                    continue

            # 2. Fuzzy match word by word (extreme typo tolerance)
            for t_word in stemmed_tokens:
                for v_word in vocab_tokens_en + vocab_tokens_gu:
                    sim = _similarity(t_word, v_word)
                    if sim > 0.70:
                        score = sim
                        if score > best_score:
                            best_score = score
                            best_station = s

        return best_station

    def _match_division(self, text):
        tokens = _tokenize(text)
        stemmed_tokens = [self._apply_aliases(t) for t in _stem_tokens(tokens)]

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

            for t_word in stemmed_tokens:
                for v_word in vocab_tokens_en + vocab_tokens_gu:
                    sim = _similarity(t_word, v_word)
                    if sim > 0.70:
                        score = sim
                        if score > best_score:
                            best_score = score
                            best_division = d

        return best_division

    def _match_rank(self, text):
        tokens = _tokenize(text)
        stemmed_tokens = _stem_tokens(tokens)
        
        # Rank mappings defined as tokenized lists
        rank_mapping = {
            # PI
            "PI": [["pi"], ["police", "inspector"], ["inspector"], ["પીઆઈ"], ["પોલીસ", "ઇન્સ્પેક્ટર"], ["ઇન્સ્પેક્ટર"], ["ઈન્સ્પેક્ટર"]],
            # PSI
            "PSI": [["psi"], ["police", "sub", "inspector"], ["police", "sub-inspector"], ["sub", "inspector"], ["sub-inspector"], ["પીએસઆઈ"], ["પોલીસ", "સબ", "ઇન્સ્પેક્ટર"], ["પોલીસ", "સબ", "ઈન્સ્પેક્ટર"], ["સબ", "ઇન્સ્પેક્ટર"], ["સબ", "ઈન્સ્પેક્ટર"]],
            # AASI
            "AASI": [["aasi"], ["armed", "assistant", "sub", "inspector"], ["armed", "assistant", "sub-inspector"], ["એએએસઆઈ"], ["સશસ્ત્ર", "મદદનીશ", "સબ", "ઇન્સ્પેક્ટર"]],
            # UASI
            "UASI": [["uasi"], ["unarmed", "assistant", "sub", "inspector"], ["unarmed", "assistant", "sub-inspector"], ["યુએએસઆઈ"], ["નિઃશસ્ત્ર", "મદદનીશ", "સબ", "ઇન્સ્પેક્ટર"]],
            # ASI
            "UASI": [["asi"], ["assistant", "sub", "inspector"], ["assistant", "sub-inspector"], ["એએસઆઈ"], ["મદદનીશ", "સબ", "ઇન્સ્પેક્ટર"]],
            # AHC
            "AHC": [["ahc"], ["armed", "head", "constable"], ["સશસ્ત્ર", "હેડ", "કોન્સ્ટેબલ"]],
            # UHC
            "UHC": [["uhc"], ["unarmed", "head", "constable"], ["નિઃશસ્ત્ર", "હેડ", "કોન્સ્ટેબલ"]],
            # HC
            "UHC": [["hc"], ["head", "constable"], ["jamadar"], ["હેડ", "કોન્સ્ટેબલ"], ["જમાદાર"]],
            # APC
            "APC": [["apc"], ["armed", "police", "constable"], ["armed", "constable"], ["સશસ્ત્ર", "પોલીસ", "કોન્સ્ટેબલ"], ["સશસ્ત્ર", "કોન્સ્ટેબલ"]],
            # UPC
            "UPC": [["upc"], ["unarmed", "police", "constable"], ["unarmed", "constable"], ["નિઃશસ્ત્ર", "પોલીસ", "કોન્સ્ટેબલ"], ["નિઃશસ્ત્ર", "કોન્સ્ટેબલ"]],
            # PC
            "UPC": [["pc"], ["constable"], ["police", "constable"], ["કોન્સ્ટેબલ"], ["પોલીસ", "કોન્સ્ટેબલ"]],
            # ALR
            "ALR": [["alr"], ["armed", "lokrakshak"], ["Armed", "lr"], ["સશસ્ત્ર", "લોકરક્ષક"]],
            # ULR
            "ULR": [["ulr"], ["unarmed", "lokrakshak"], ["unarmed", "lr"], ["નિઃશસ્ત્ર", "લોકરક્ષક"]],
            # LR
            "ULR": [["lr"], ["lokrakshak"], ["lok", "rakshak"], ["લોકરક્ષક"]],
        }

        # Check in order of specificity (longest phrase first)
        all_matches = []
        for rank_code, phrases in rank_mapping.items():
            for p in phrases:
                p_stemmed = _stem_tokens(p)
                if _match_phrase_in_tokens(p_stemmed, stemmed_tokens):
                    all_matches.append((rank_code, len(p_stemmed)))
                    
        if all_matches:
            all_matches.sort(key=lambda x: -x[1])
            return all_matches[0][0]

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

        all_matches = []
        for spec_code, phrases in spec_mapping.items():
            for p in phrases:
                p_stemmed = _stem_tokens(p)
                if _match_phrase_in_tokens(p_stemmed, stemmed_tokens):
                    all_matches.append((spec_code, len(p_stemmed)))
                    
        if all_matches:
            all_matches.sort(key=lambda x: -x[1])
            best_spec, best_len = all_matches[0]
            # Special check to avoid false matches for single short English tokens
            if best_spec == "IT_COMPUTER" and ["it"] in spec_mapping["IT_COMPUTER"]:
                if "it" not in tokens:
                    return None
            return best_spec

        for s in self.vocab["specs"]:
            code = s["spec_code"].lower()
            if code in tokens:
                return s["spec_code"]

        return None

    # ----- main entry -----
    def interpret(self, question):
        """
        Map a natural-language question to ONE safe query template.
        """
        q = _norm(question)
        ql = _lower(q)
        q_norm = _normalize_guj(ql)
        
        station = self._match_station(q)
        division = self._match_division(q)
        rank = self._match_rank(q)
        spec = self._match_spec(q)

        # 1. Distinguish between Age and Years of Service / Experience
        has_age_context = any(x in ql or x in q_norm for x in ["age", "old", "ઉંમર", "મોટી", "મોટો", "નાની", "નાનો"])

        min_years = None
        max_years = None
        min_age = None
        max_age = None

        # Advanced Range & Extreme Parsing
        if not has_age_context:
            # Check for range: "between X and Y years"
            range_match = re.search(r"(?:between|from)\s*(\d+)\s*(?:and|to|-)\s*(\d+)\s*(?:years?|વર્ષ)", ql)
            if range_match:
                min_years = int(range_match.group(1))
                max_years = int(range_match.group(2))
            else:
                years_match_more = re.search(r"(?:more than|>|over|at least|minimum)\s*(\d+)\s*(?:years?|વર્ષ)", ql)
                if not years_match_more:
                    years_match_more = re.search(r"(\d+)\s*(?:years?|વર્ષ)(?:\s*(?:કે તેથી વધારે|કે તેથી વધુ|થી વધારે|થી વધુ|વધુ|અનુભવ|કામ|થી કામ|થી કામ કરતા|કામ કરતા))", q)
                if not years_match_more:
                    years_match_more = re.search(r"(\d+)\+\s*(?:years?|વર્ષ)", ql)
                if years_match_more:
                    min_years = int(years_match_more.group(1))

                years_match_less = re.search(r"(?:less than|<|under|at most|maximum)\s*(\d+)\s*(?:years?|વર્ષ)", ql)
                if not years_match_less:
                    years_match_less = re.search(r"(\d+)\s*(?:years?|વર્ષ)(?:\s*(?:કે તેથી ઓછો|કે તેથી ઓછી|થી ઓછો|થી ઓછી|ઓછો|ઓછી))", q)
                if years_match_less:
                    max_years = int(years_match_less.group(1))
                
                # Extreme modifiers (most experienced / newest)
                if any(k in ql for k in ["most experienced", "longest serving", "senior most", "સૌથી વધુ અનુભવી", "સૌથી જુના"]):
                    min_years = 15 # Heuristic for highly experienced
                elif any(k in ql for k in ["newest", "least experienced", "fresh", "સૌથી નવા", "સૌથી ઓછો અનુભવી"]):
                    max_years = 2 # Heuristic for very new
        
        else:
            range_match = re.search(r"(?:between|from)\s*(\d+)\s*(?:and|to|-)\s*(\d+)\s*(?:years?|વર્ષ)", ql)
            if range_match:
                min_age = int(range_match.group(1))
                max_age = int(range_match.group(2))
            else:
                age_under = re.search(r"(?:under|<|younger than|below|at most)\s*(\d+)\s*(?:years?|વર્ષ)", ql)
                if not age_under:
                    age_under = re.search(r"(\d+)\s*(?:years?|વર્ષ)થી\s*(?:નાની|નાનો|ઓછી|ઓછો)", q)
                if not age_under:
                    age_under = re.search(r"(\d+)\s*(?:વર્ષ)\s*(?:થી ઓછી ઉંમર|થી નાની ઉંમર)", q)
                if age_under:
                    max_age = int(age_under.group(1))

                age_over = re.search(r"(?:over|>|older than|above|at least)\s*(\d+)\s*(?:years?|વર્ષ)", ql)
                if not age_over:
                    age_over = re.search(r"(\d+)\s*(?:years?|વર્ષ)થી\s*(?:મોટી|મોટો|વધુ|વધારે)", q)
                if not age_over:
                    age_over = re.search(r"(\d+)\s*(?:વર્ષ)\s*(?:થી વધુ ઉંમર|થી મોટી ઉંમર)", q)
                if age_over:
                    min_age = int(age_over.group(1))
                
                # Extreme modifiers (youngest / oldest)
                if any(k in ql for k in ["youngest", "સૌથી નાની", "સૌથી નાનો"]):
                    max_age = 28 # Heuristic for youngest
                elif any(k in ql for k in ["oldest", "સૌથી મોટી", "સૌથી મોટો"]):
                    min_age = 50 # Heuristic for oldest

        # 2. Gender extraction (plurals/stemmed compatible whole tokens)
        gender = None
        gender_f_keywords = ["female", "lady", "women", "woman", "she", "મહિલા", "સ્ત્રી", "બહેન"]
        gender_m_keywords = ["male", "men", "man", "he", "પુરુષ", "પુરૂષ", "ભાઈ"]
        
        stemmed_tokens = _stem_tokens(_tokenize(q_norm))
        
        norm_f_kws = [_guj_stem(_normalize_guj(_lower(k))) for k in gender_f_keywords]
        norm_m_kws = [_guj_stem(_normalize_guj(_lower(k))) for k in gender_m_keywords]
        
        if any(w in stemmed_tokens for w in norm_f_kws) or any(any(fk in t for fk in ["મહીલા", "સ્ત્રી", "બહેન"]) for t in stemmed_tokens if not t.isascii()):
            gender = 'F'
        elif any(w in stemmed_tokens for w in norm_m_kws) or any(any(mk in t for mk in ["પુરુષ", "પુરૂષ", "ભાઈ"]) for t in stemmed_tokens if not t.isascii()):
            gender = 'M'

        # 3. Disciplinary record (clean_record)
        clean_record_terms = [
            "clean record", "no punishment", "unpunished", "good conduct", "clean sheet",
            "ક્લીન રેકોર્ડ", "કોઈ સજા", "સજા વગર", "સજા ન", "નિષ્કલંક", "કોઈ શિક્ષા", "શિક્ષા વગર"
        ]
        norm_clean_terms = [_normalize_guj(_lower(t)) for t in clean_record_terms]
        
        clean_record = False
        for term in norm_clean_terms:
            if term in q_norm or term in ql:
                clean_record = True
                break

        # 4. Semantic trigger keywords
        count_kws = ["how many", "count", "number of", "total", "quantity", "sum", "કેટલા", "કેટલી", "કેટલો", "કુલ સંખ્યા", "સંખ્યા", "ગણતરી", "ત્યાં કેટલા છે"]
        list_kws = ["list", "show", "which", "who", "name", "get", "find", "display", "officer", "personnel", "all", "detail", "યાદી", "કોણ", "શોધો", "બતાવો", "બધા", "લિસ્ટ", "કર્મચારી", "અધિકારી", "નામ", "માહિતી આપો"]
        award_kws = ["award", "top", "best", "highest", "medal", "reward", "honored", "honoured", "ઇનામ", "ઈનામ", "એવોર્ડ", "મેડલ", "પુરસ્કાર", "ઉત્કૃષ્ટ", "સૌથી વધુ સન્માનિત"]
        vacancy_kws = ["vacancy", "vacant", "shortage", "empty", "need", "lack", "ખાલી", "જગ્યા", "ઘટ", "અછત", "જરૂરિયાત", "ખાલી જગ્યાઓ"]

        norm_count_kws = [_normalize_guj(_lower(k)) for k in count_kws]
        norm_list_kws = [_normalize_guj(_lower(k)) for k in list_kws]
        norm_award_kws = [_normalize_guj(_lower(k)) for k in award_kws]
        norm_vacancy_kws = [_normalize_guj(_lower(k)) for k in vacancy_kws]

        wants_count = any(k in q_norm or k in ql or k in stemmed_tokens for k in norm_count_kws)
        wants_list = any(k in q_norm or k in ql or k in stemmed_tokens for k in norm_list_kws)
        wants_awards = any(k in q_norm or k in ql or k in stemmed_tokens for k in norm_award_kws)
        wants_vacancy = any(k in q_norm or k in ql or k in stemmed_tokens for k in norm_vacancy_kws)

        filters = {
            "min_years": min_years, "max_years": max_years,
            "min_age": min_age, "max_age": max_age,
            "gender": gender, "clean_record": clean_record
        }
        has_filter = any(v is not None and v is not False for v in filters.values())

        # 5. Intent Classifier Scoring
        scores = {
            "station_vacancy": 0.0,
            "top_awards": 0.0,
            "count_personnel": 0.0,
            "list_personnel": 0.0
        }

        # Intent 1: station_vacancy
        if wants_vacancy:
            scores["station_vacancy"] += 3.0
        if station:
            scores["station_vacancy"] += 2.0
        elif division:
            scores["station_vacancy"] += 1.0

        # Intent 2: top_awards
        if wants_awards:
            scores["top_awards"] += 3.0
        if wants_list:
            scores["top_awards"] += 1.0
        if wants_count:
            scores["top_awards"] += 0.5
        if station or division:
            scores["top_awards"] += 0.5
        if has_filter:
            scores["top_awards"] += 0.5

        # Intent 3: count_personnel
        if wants_count:
            scores["count_personnel"] += 3.0
        if rank:
            scores["count_personnel"] += 1.0
        if spec:
            scores["count_personnel"] += 1.0
        if station or division:
            scores["count_personnel"] += 1.0
        if has_filter:
            scores["count_personnel"] += 1.0

        # Intent 4: list_personnel
        if wants_list:
            scores["list_personnel"] += 3.0
        if spec:
            scores["list_personnel"] += 2.0
        if rank:
            scores["list_personnel"] += 1.0
        if station or division:
            scores["list_personnel"] += 1.0
        if has_filter:
            scores["list_personnel"] += 1.0

        # Select highest intent
        best_intent, best_score = max(scores.items(), key=lambda x: x[1])

        # Verification threshold
        confidence_threshold = 2.0
        has_entities = bool(rank or spec or station or division or has_filter)
        
        is_nonsense = True
        if best_score >= confidence_threshold:
            if best_intent == "station_vacancy" and (station or division):
                is_nonsense = False
            elif best_intent == "top_awards":
                is_nonsense = False
            elif best_intent == "count_personnel" and has_entities:
                is_nonsense = False
            elif best_intent == "list_personnel" and (has_entities or wants_list):
                is_nonsense = False

        if is_nonsense:
            return NLQResult(
                intent="unknown", interpretation="", columns=[], rows=[],
                sql_label=None, ok=False,
                message=("Could not confidently interpret the question. Try e.g. "
                         "“How many female PSIs at <station>?”, “List officers in "
                         "<division> with clean record”, or “<station> માં કેટલા અધિકારીઓ છે?”"),
            )

        # 6. Execute mapped template
        if best_intent == "station_vacancy":
            return self._q_vacancy(station, division)
        elif best_intent == "top_awards":
            return self._q_top_awards(station, division, filters)
        elif best_intent == "count_personnel":
            return self._q_count(rank, station, division, filters)
        else:
            return self._q_list(rank, spec, station, division, filters)

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

    def _q_list(self, rank, spec, station, division, filters=None, is_past_posting=False):
        where, params, desc = ["p.is_active"], [], []
        joins = ["LEFT JOIN clean.dim_station s ON s.station_id = p.current_station_id"]
        if spec:
            joins.append("JOIN clean.person_specialization ps ON ps.person_id = p.person_id")
            where.append("ps.spec_code = %s"); params.append(spec); desc.append(f"skill {spec}")
        if rank:
            where.append("p.rank_code = %s"); params.append(rank); desc.append(f"rank {rank}")
        
        if station:
            if is_past_posting:
                joins.append("JOIN clean.person_posting_history pph ON pph.person_id = p.person_id")
                where.append("(pph.place_en ILIKE %s OR pph.place_raw ILIKE %s)"); 
                params.extend([f"%{station['name_en']}%", f"%{station['name_en']}%"])
                where.append("p.current_station_id != %s"); params.append(station["station_id"])
                desc.append(f"transferred from {station['name_en']}")
            else:
                where.append("p.current_station_id = %s"); params.append(station["station_id"])
                desc.append(f"at {station['name_en']}")
                
        if division:
            if is_past_posting:
                if not station: # Only join if not already joined
                    joins.append("JOIN clean.person_posting_history pph ON pph.person_id = p.person_id")
                where.append("(pph.place_en ILIKE %s OR pph.place_raw ILIKE %s)"); 
                params.extend([f"%{division['name_en']}%", f"%{division['name_en']}%"])
                where.append("s.division_id != %s"); params.append(division["division_id"])
                desc.append(f"transferred from {division['name_en']} division")
            else:
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

    def _q_top_awards(self, station, division=None, filters=None):
        where, params, desc = ["p.is_active", "perf.awards_count > 0"], [], []
        joins = [
            "JOIN clean.person_performance perf ON perf.person_id = p.person_id",
            "LEFT JOIN clean.dim_station s ON s.station_id = p.current_station_id"
        ]
        if station:
            where.append("p.current_station_id = %s"); params.append(station["station_id"])
            desc.append(f"at {station['name_en']}")
        elif division:
            where.append("s.division_id = %s"); params.append(division["division_id"])
            desc.append(f"in {division['name_en']} division")
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

    def _q_vacancy(self, station, division=None):
        if station:
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
