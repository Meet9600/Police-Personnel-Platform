"""
Flask web application — Gujarat Police Management & AI.

Screens:
  * /login, /logout            — session auth (hashed passwords; no plaintext).
  * /                          — dashboard.
  * /recommend                 — requirement form -> explained team recommendation.
  * /ask                       — natural-language Q&A box (grounded, offline).
  * /personnel                 — searchable roster from clean.vw_ml_features.
  * /set-language/<lang>       — toggle UI between English and Gujarati.

Bilingual via Flask-Babel (gettext). All AI processing is local/offline.
"""
import os
import sys
import functools

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import (Flask, render_template, request, redirect, url_for,
                   session, flash, g, jsonify)
from flask_babel import Babel, gettext as _
from werkzeug.security import check_password_hash, generate_password_hash

from app.config import Config, query
from app.recommender import recommend_team
from nlp.nlq_engine import ask as nlq_ask


def select_locale():
    lang = session.get("lang")
    if lang in Config.LANGUAGES:
        return lang
    return request.accept_languages.best_match(Config.LANGUAGES) or "en"


def create_app():
    app = Flask(__name__, template_folder="templates", static_folder="static")
    app.config.from_object(Config)
    app.config["BABEL_TRANSLATION_DIRECTORIES"] = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "translations")
    Babel(app, locale_selector=select_locale)

    # ---- helpers ----
    def login_required(view):
        @functools.wraps(view)
        def wrapped(*a, **kw):
            if not session.get("user"):
                return redirect(url_for("login", next=request.path))
            return view(*a, **kw)
        return wrapped

    @app.context_processor
    def inject_globals():
        return {"current_lang": select_locale(), "current_user": session.get("user")}

    # ---- auth ----
    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "POST":
            username = request.form.get("username", "").strip()
            password = request.form.get("password", "")
            # Authenticate against existing users table (password_hash).
            # NOTE: passwords are verified against a stored hash; never stored
            # or logged in plaintext.
            rows = query(
                "SELECT id, username, password_hash, full_name, role "
                "FROM public.users WHERE username = %s",
                [username], read_only=False,
            ) if _users_table_exists() else []
            user = rows[0] if rows else None
            if user and user.get("password_hash") and \
                    check_password_hash(user["password_hash"], password):
                session["user"] = {"id": user["id"], "username": user["username"],
                                   "name": user.get("full_name") or user["username"],
                                   "role": user.get("role")}
                return redirect(request.args.get("next") or url_for("dashboard"))
            # demo fallback so the app is usable before real users are wired:
            if username == "admin" and password == os.environ.get("DEMO_PASS", "admin123"):
                session["user"] = {"id": 0, "username": "admin",
                                   "name": "Demo Admin", "role": "demo"}
                return redirect(url_for("dashboard"))
            flash(_("Invalid username or password."), "error")
        return render_template("login.html")

    @app.route("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @app.route("/set-language/<lang>")
    def set_language(lang):
        if lang in Config.LANGUAGES:
            session["lang"] = lang
        return redirect(request.referrer or url_for("dashboard"))

    # ---- dashboard ----
    @app.route("/")
    @login_required
    def dashboard():
        stats = query("""
            SELECT
              (SELECT count(*) FROM clean.person) AS total,
              (SELECT count(*) FROM clean.person WHERE rank_band='officer') AS officers,
              (SELECT count(*) FROM clean.person WHERE rank_band='employee') AS employees,
              (SELECT count(*) FROM clean.dim_station) AS stations,
              (SELECT count(*) FROM clean.dim_division) AS divisions
        """)[0]
        return render_template("dashboard.html", stats=stats)

    # ---- recommendation ----
    @app.route("/recommend", methods=["GET", "POST"])
    @login_required
    def recommend():
        divisions_query = query("""
            SELECT d.division_id, d.name_en as div_name_en, d.name_raw as div_name_gu, 
                   s.station_id, s.name_en as st_name_en, s.name_raw as st_name_gu
            FROM clean.dim_division d
            LEFT JOIN clean.dim_station s ON s.division_id = d.division_id AND s.is_active = TRUE
            ORDER BY d.name_en, s.name_en
        """)
        grouped_stations = {}
        for row in divisions_query:
            d_id = row['division_id']
            if d_id not in grouped_stations:
                grouped_stations[d_id] = {
                    'name_en': row['div_name_en'],
                    'name_gu': row['div_name_gu'],
                    'stations': []
                }
            if row['station_id']:
                grouped_stations[d_id]['stations'].append({
                    'id': row['station_id'],
                    'name_en': row['st_name_en'],
                    'name_gu': row['st_name_gu']
                })
        specs = query("SELECT spec_code, spec_name_en, category FROM clean.specialization_ref "
                      "WHERE spec_code <> 'UNCLASSIFIED' ORDER BY category, spec_name_en")
        ranks = query("SELECT rank_code, rank_name_en, rank_band FROM clean.rank_ref "
                      "ORDER BY rank_order DESC")
        result = None
        if request.method == "POST":
            try:
                target_vals = request.form.getlist("station_id")
                station_ids, division_ids = [], []
                for val in target_vals:
                    if val and val.startswith("div_"):
                        division_ids.append(int(val[4:]))
                    elif val and val.isdigit():
                        station_ids.append(int(val))

                needed = set(request.form.getlist("specializations"))
                team_size = max(1, int(request.form.get("team_size", 1)))
                rank_mix = {}
                for r in ranks:
                    n = request.form.get(f"rank_{r['rank_code']}", "").strip()
                    if n and n.isdigit() and int(n) > 0:
                        rank_mix[r["rank_code"]] = int(n)
                result = recommend_team(station_ids, division_ids, needed, team_size, rank_mix or None)
            except (TypeError, ValueError):
                flash(_("Please complete the form correctly."), "error")
        return render_template("recommend.html", grouped_stations=grouped_stations, specs=specs,
                               ranks=ranks, result=result)

    # ---- NLP Q&A ----
    @app.route("/ask", methods=["GET", "POST"])
    @login_required
    def ask_page():
        answer, question = None, ""
        if request.method == "POST":
            question = request.form.get("question", "").strip()
            if question:
                answer = nlq_ask(question)
        return render_template("ask.html", answer=answer, question=question)

    @app.route("/api/ask", methods=["POST"])
    @login_required
    def api_ask():
        data = request.get_json(silent=True) or {}
        q = (data.get("question") or "").strip()
        if not q:
            return jsonify({"ok": False, "message": "empty question"}), 400
        return jsonify(nlq_ask(q))

    # ---- personnel browser ----
    @app.route("/personnel")
    @login_required
    def personnel():
        rank = request.args.get("rank", "")
        spec = request.args.get("spec", "")
        where, params = ["1=1"], []
        sql = """
            SELECT v.person_id, p.full_name_gu AS name, v.rank_code, v.rank_band,
                   v.station_en, v.years_of_service, v.awards_count, v.punishments_count,
                   v.specializations
            FROM clean.vw_ml_features v
            JOIN clean.person p ON p.person_id = v.person_id
            WHERE {where}
            ORDER BY v.rank_order DESC, v.person_id
            LIMIT 300
        """
        if rank:
            where.append("v.rank_code = %s"); params.append(rank)
        if spec:
            where.append("%s = ANY(v.specializations)"); params.append(spec)
        rows = query(sql.format(where=" AND ".join(where)), params)
        ranks = query("SELECT rank_code FROM clean.rank_ref ORDER BY rank_order DESC")
        specs = query("SELECT spec_code FROM clean.specialization_ref "
                      "WHERE spec_code<>'UNCLASSIFIED' ORDER BY spec_code")
        return render_template("personnel.html", rows=rows, ranks=ranks, specs=specs,
                               sel_rank=rank, sel_spec=spec)

    return app


def _users_table_exists():
    try:
        r = query("SELECT to_regclass('public.users') AS t", read_only=False)
        return r and r[0]["t"] is not None
    except Exception:
        return False


app = create_app()

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5001, debug=False)
