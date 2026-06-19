#!/usr/bin/env python3
"""
Gujarat Police Management AI — ETL pipeline (source 'public' -> schema 'clean').

Idempotent + incremental:
  * New source ids  -> APPEND a new person.
  * Existing source ids (e.g. after a transfer) -> UPDATE the person's posting
    and fields in place; never duplicates the person.
  * Posting history & specializations keyed by source rows -> safe to re-run.

NON-DESTRUCTIVE: original values preserved in *_raw columns; missing values
become explicit NULL plus an entry in person.missing_flags. Nothing is deleted.

No external services. Pure local Postgres + Python. Honours the data-privacy
constraint (no network egress).

Usage:
    python run_pipeline.py            # full incremental load
Connection via env: PGHOST PGPORT PGDATABASE PGUSER (PGPASSWORD if needed)
"""
import os
import re
import datetime as dt
import psycopg2
import psycopg2.extras

# --- Gujarati -> Western digit map -----------------------------------------
GUJ_DIGITS = str.maketrans("૦૧૨૩૪૫૬૭૮૯", "0123456789")

# Honorifics that prefix names (Mr / Ms etc.)
HONORIFICS = ["શ્રીમતી", "સુશ્રી", "કુ.", "શ્રી", "ડૉ.", "ડૉ"]

# Rank-suffix noise sometimes appended to the name field (e.g. Dy.PSI).
NAME_RANK_NOISE = ["વુ.પો.સ.ઇ", "વુ.પો.સ.ઈ", "પો.સ.ઇ", "પો.સ.ઈ", "પો.ઇન્સ", "પો.ઇન્સ."]

# Study normalization (Gujarati script -> canonical English token)
STUDY_MAP = {
    "બી.કોમ": "B.COM", "બી.એ": "B.A.", "બી.એસસી": "B.SC", "એમ.કોમ": "M.COM",
    "એમ.એ": "M.A.", "બી.કોમ.": "B.COM",
}


def norm_digits(s):
    return s.translate(GUJ_DIGITS) if s else s


def parse_year(batch_raw):
    """batch may be '2022', '01/04/1991', '૨૦૦૯-૧૦', or blank -> 4-digit year or None."""
    if not batch_raw:
        return None
    s = norm_digits(batch_raw.strip())
    m = re.search(r"(19|20)\d{2}", s)
    return int(m.group(0)) if m else None


def split_name(name_raw):
    """Return (honorific, clean_name_gu) with honorific + trailing rank-noise stripped."""
    if not name_raw:
        return None, None
    s = name_raw.strip()
    honorific = None
    for h in HONORIFICS:
        if s.startswith(h):
            honorific = h
            s = s[len(h):].strip()
            break
    for noise in NAME_RANK_NOISE:
        if s.endswith(noise):
            s = s[: -len(noise)].strip()
    return honorific, s


def norm_study(study_raw):
    if not study_raw:
        return None
    s = study_raw.strip().rstrip(".").upper()
    key = study_raw.strip().rstrip(".")
    if key in STUDY_MAP:
        return STUDY_MAP[key]
    # already-English values: collapse 'B.A' / 'B.A.' -> 'B.A.'
    return s.replace(" ", "")


def years_between(d, ref=None):
    if not d:
        return None
    ref = ref or dt.date.today()
    return round((ref - d).days / 365.25, 2)


def age_from_dob(dob):
    if not dob:
        return None
    today = dt.date.today()
    return today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))


def connect():
    return psycopg2.connect(
        host=os.environ.get("PGHOST", "/tmp/pgrun"),
        port=os.environ.get("PGPORT", "5433"),
        dbname=os.environ.get("PGDATABASE", "police_management"),
        user=os.environ.get("PGUSER", "postgres"),
    )


# ===========================================================================
def load_org(cur):
    cur.execute("""
        INSERT INTO clean.dim_division (division_id, name_raw, name_en, is_active)
        SELECT id, name, name, COALESCE(is_active, TRUE) FROM public.divisions
        ON CONFLICT (division_id) DO UPDATE
          SET name_raw=EXCLUDED.name_raw, is_active=EXCLUDED.is_active;
    """)
    cur.execute("""
        INSERT INTO clean.dim_station (station_id, division_id, name_raw, name_en, is_active)
        SELECT id, division_id, police_station_branch_name, police_station_branch_name,
               COALESCE(is_active, TRUE)
        FROM public.police_station_branch
        ON CONFLICT (station_id) DO UPDATE
          SET division_id=EXCLUDED.division_id, name_raw=EXCLUDED.name_raw,
              is_active=EXCLUDED.is_active;
    """)


def load_duty_map_raw(cur):
    """Backfill duty_detail_raw from live table; flag any duty id not pre-mapped."""
    cur.execute("SELECT id, duty_detail FROM public.duty_details")
    for did, detail in cur.fetchall():
        cur.execute("""
            INSERT INTO clean.duty_map (source_duty_id, duty_detail_raw, spec_code, needs_review)
            VALUES (%s, %s, 'UNCLASSIFIED', TRUE)
            ON CONFLICT (source_duty_id)
              DO UPDATE SET duty_detail_raw = EXCLUDED.duty_detail_raw;
        """, (did, detail))


def load_people(cur, source_table, prefix, rank_band):
    cur.execute(f"SELECT * FROM public.{source_table}")
    cols = [d[0] for d in cur.description]
    rows = cur.fetchall()
    for row in rows:
        r = dict(zip(cols, row))
        pid = f"{prefix}-{r['id']}"
        honorific, clean_name = split_name(r.get("name"))
        dob = r.get("date_of_birth")
        appt = r.get("appointment_date")  # officers only
        present_date = r.get("police_station_present_date")  # employees
        batch_raw = r.get("batch")
        flags = []
        if not dob:
            flags.append("dob")
        if rank_band == "officer" and not appt:
            flags.append("appointment_date")
        if not r.get("name"):
            flags.append("name")
        if not r.get("police_station_branch_id"):
            flags.append("station")

        # NOTE: source appointment_date/present_date is a data-entry timestamp,
        # NOT the real joining date. The reliable seniority signal is `batch`
        # (year joined). Derive service years from batch_year; flag if absent.
        batch_year = parse_year(batch_raw)
        if batch_year:
            yos = round(dt.date.today().year - batch_year +
                        (dt.date.today().timetuple().tm_yday / 365.25), 2)
        else:
            yos = None
            flags.append("service_years")
        appt_eff = appt or present_date
        cur.execute("""
            INSERT INTO clean.person
              (person_id, source_table, source_id, rank_band, rank_code, rank_raw,
               full_name_raw, full_name_gu, honorific, current_station_id, gender,
               dob, age_years, appointment_date, years_of_service, study_raw, study_en,
               batch_raw, batch_year, missing_flags, is_active, last_updated_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,TRUE,now())
            ON CONFLICT (source_table, source_id) DO UPDATE SET
               rank_code=EXCLUDED.rank_code, rank_raw=EXCLUDED.rank_raw,
               full_name_raw=EXCLUDED.full_name_raw, full_name_gu=EXCLUDED.full_name_gu,
               honorific=EXCLUDED.honorific,
               current_station_id=EXCLUDED.current_station_id,   -- transfer updates posting
               gender=EXCLUDED.gender, dob=EXCLUDED.dob, age_years=EXCLUDED.age_years,
               appointment_date=EXCLUDED.appointment_date,
               years_of_service=EXCLUDED.years_of_service,
               study_raw=EXCLUDED.study_raw, study_en=EXCLUDED.study_en,
               batch_raw=EXCLUDED.batch_raw, batch_year=EXCLUDED.batch_year,
               missing_flags=EXCLUDED.missing_flags, last_updated_at=now();
        """, (
            pid, source_table, r["id"], rank_band,
            r.get("designation"), r.get("designation"),
            r.get("name"), clean_name, honorific,
            r.get("police_station_branch_id"), r.get("gender"),
            dob, age_from_dob(dob), appt, yos,
            r.get("study"), norm_study(r.get("study")),
            batch_raw, batch_year, flags,
        ))


def load_specializations(cur, link_table, id_col, prefix):
    cur.execute(f"""
        SELECT d.{id_col}, dm.spec_code, dm.duty_detail_raw
        FROM public.{link_table} d
        JOIN clean.duty_map dm ON dm.source_duty_id = d.duty_id
    """)
    for sid, spec, raw in cur.fetchall():
        cur.execute("""
            INSERT INTO clean.person_specialization (person_id, spec_code, duty_detail_raw)
            VALUES (%s,%s,%s) ON CONFLICT DO NOTHING;
        """, (f"{prefix}-{sid}", spec, raw))


def load_postings(cur, sp_table, id_col, prefix):
    cur.execute(f"SELECT id, {id_col}, duties_performed, start_date, end_date, remarks FROM public.{sp_table}")
    for sid, pid_src, place, start, end, remarks in cur.fetchall():
        if pid_src is None:
            continue
        cur.execute("""
            INSERT INTO clean.person_posting_history
              (person_id, place_raw, place_en, start_date, end_date, remarks,
               source_table, source_id)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (source_table, source_id) DO UPDATE
              SET place_raw=EXCLUDED.place_raw, start_date=EXCLUDED.start_date,
                  end_date=EXCLUDED.end_date, remarks=EXCLUDED.remarks;
        """, (f"{prefix}-{pid_src}", place, place, start, end, remarks, sp_table, sid))


def load_performance(cur):
    cur.execute("""
        INSERT INTO clean.person_performance
          (person_id, awards_count, punishments_count, last_award_date, last_punishment_date)
        SELECT p.person_id,
               COALESCE(a.cnt,0), COALESCE(pu.cnt,0), a.last_dt, pu.last_dt
        FROM clean.person p
        LEFT JOIN (
            SELECT 'O-'||officer_id pid, count(*) cnt, max(award_date) last_dt
            FROM public.officer_awards GROUP BY officer_id
            UNION ALL
            SELECT 'E-'||employee_id pid, count(*) cnt, max(award_date) last_dt
            FROM public.employee_awards GROUP BY employee_id
        ) a ON a.pid = p.person_id
        LEFT JOIN (
            SELECT 'O-'||officer_id pid, count(*) cnt, max(punishment_date) last_dt
            FROM public.officer_punishments GROUP BY officer_id
            UNION ALL
            SELECT 'E-'||employee_id pid, count(*) cnt, max(punishment_date) last_dt
            FROM public.employee_punishments GROUP BY employee_id
        ) pu ON pu.pid = p.person_id
        ON CONFLICT (person_id) DO UPDATE SET
            awards_count=EXCLUDED.awards_count,
            punishments_count=EXCLUDED.punishments_count,
            last_award_date=EXCLUDED.last_award_date,
            last_punishment_date=EXCLUDED.last_punishment_date;
    """)


def load_capacity(cur):
    cur.execute("""
        INSERT INTO clean.station_capacity (station_id, rank_band, approved_total, present_total, vacancy)
        SELECT s.station_id, 'officer',
               oa.total, op.total, COALESCE(oa.total,0)-COALESCE(op.total,0)
        FROM clean.dim_station s
        LEFT JOIN public.officer_approved_strength oa ON oa.police_station_branch_id=s.station_id
        LEFT JOIN public.officer_present_strength  op ON op.police_station_branch_id=s.station_id
        ON CONFLICT (station_id, rank_band) DO UPDATE
          SET approved_total=EXCLUDED.approved_total, present_total=EXCLUDED.present_total,
              vacancy=EXCLUDED.vacancy;
    """)
    cur.execute("""
        INSERT INTO clean.station_capacity (station_id, rank_band, approved_total, present_total, vacancy)
        SELECT s.station_id, 'employee',
               ea.total, ep.total, COALESCE(ea.total,0)-COALESCE(ep.total,0)
        FROM clean.dim_station s
        LEFT JOIN public.employee_approved_strength ea ON ea.police_station_branch_id=s.station_id
        LEFT JOIN public.employee_present_strength  ep ON ep.police_station_branch_id=s.station_id
        ON CONFLICT (station_id, rank_band) DO UPDATE
          SET approved_total=EXCLUDED.approved_total, present_total=EXCLUDED.present_total,
              vacancy=EXCLUDED.vacancy;
    """)


def main():
    conn = connect()
    conn.autocommit = False
    cur = conn.cursor()
    print("[1/7] org hierarchy ...");        load_org(cur)
    print("[2/7] duty map raw ...");          load_duty_map_raw(cur)
    print("[3/7] people (officers) ...");     load_people(cur, "officer_details", "O", "officer")
    print("      people (employees) ...");    load_people(cur, "employee_details", "E", "employee")
    print("[4/7] specializations ...");       load_specializations(cur, "officer_duties", "officer_id", "O")
    load_specializations(cur, "employee_duties", "employee_id", "E")
    print("[5/7] postings ...");              load_postings(cur, "services_periods", "officer_details_id", "O")
    load_postings(cur, "services_periods_emp", "employee_details_id", "E")
    print("[6/7] performance ...");           load_performance(cur)
    print("[7/7] station capacity ...");      load_capacity(cur)
    conn.commit()
    cur.close(); conn.close()
    print("Pipeline complete.")


if __name__ == "__main__":
    main()
