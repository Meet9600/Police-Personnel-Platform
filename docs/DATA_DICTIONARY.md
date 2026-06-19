# DATA DICTIONARY & KEY FINDINGS
### Gujarat Police Management AI — Source Database (`police_management`, PostgreSQL 17)

This document describes the **source** database the system reads from, and the
real-world data quirks discovered by inspecting the actual data. Any LLM building
this system MUST respect these findings — they are not guesses, they were verified
against the real dump.

---

## 1. Scale (real numbers, from the live data)

| Entity | Count |
|---|---|
| Officers (`officer_details`)   | 133 |
| Employees (`employee_details`) | 1,392 |
| Police stations / branches     | 72 |
| Divisions                       | 6 |
| Duty types (`duty_details`)     | 77 |
| **Total personnel**             | **1,525** |

The full DB has 40 tables; only the **domain** tables below matter for the AI
system. Ignore all auth/plumbing tables (`users`, `user_sessions`,
`login_attempts`, `security_logs`, `rate_limit_attempts`, `alembic_version`),
family tables (`*_child_details`, `*_parent_details`, `*_spouse_details`),
`*_medical_insurance`, `loan_*`, `*_archive`, `*_transfer_drafts`, and `vw_*`.

---

## 2. The two-family structure (CRITICAL)

Everything exists twice:
- **Officers** = gazetted ranks. Designations: `PI`, `PSI`.
- **Employees** = non-gazetted. Designations: `UASI, UHC, UPC, ULR, AASI, AHC, APC, ALR`.

`officer_details.id` and `employee_details.id` are **separate sequences whose
integer ranges overlap**. Officer #1 and Employee #1 are different people. The
clean store MUST namespace every person: `O-<id>` for officers, `E-<id>` for
employees, so they never collide.

Org hierarchy: `divisions (6)` → `police_station_branch (72)` → people.

---

## 3. Where the Gujarati/English mixing lives

`designation` is ALREADY CLEAN (coded values above). The language mess is in:

| Field | What it looks like | Cleaning needed |
|---|---|---|
| `name` | Gujarati script, e.g. `શ્રી આર.વી ઠક્કર`. Honorific (`શ્રી`=Mr, `સુશ્રી`=Ms) prefixed. Sometimes a rank suffix like `વુ.પો.સ.ઇ` (Dy.PSI) is appended. | Strip honorific into its own column; strip rank-suffix noise; keep original in `*_raw`. |
| `study` | Mixed script: `B.COM` vs `બી.કોમ`. | Map Gujarati-script qualifications to canonical English tokens. |
| `batch` | Joining year, but wildly inconsistent: `2022`, `01/04/1991`, `05-12-1998`, Gujarati numerals `૨૦૦૯-૧૦`, or blank. | Translate Gujarati numerals (`૦-૯`→`0-9`); regex-extract a 4-digit year. |
| `duty_details.duty_detail` | 77 Gujarati free-text duties, e.g. `સાયબર`(Cyber), `ટ્રાફિક`(Traffic), `શી ટીમ`(SHE Team), `VHF ઓપરેટર`. | Map each to a controlled English **specialization code** (see `02_reference_data.sql`). |

---

## 4. CRITICAL DATA TRAPS (discovered against real data — do not repeat these mistakes)

1. **`appointment_date` / `posting_date` are DATA-ENTRY TIMESTAMPS, not real
   joining dates.** Many show 2025/2026 even for 30-year veterans. **Do NOT
   compute seniority from them.** The real seniority signal is `batch` (year
   joined). Derive `years_of_service` from `batch_year`.

2. **`batch` is empty for ~94% of people** (1,435 / 1,525) — well-populated for
   officers, mostly blank for employees. Therefore `years_of_service` is usually
   missing. Experience must be an **optional, weighted** signal in the
   recommender — NEVER a hard filter or a required field.

3. **`age` column is `VARCHAR` and unreliable.** Compute age from
   `date_of_birth` instead. ~20% of officers and ~29% of employees have no DOB,
   so age is often NULL — flag it, don't impute.

4. **`services_periods.duties_performed` and `services_periods_emp.duties_performed`
   contain PLACE NAMES, not duties** (e.g. `હેડ કર્વાટર`=Headquarters,
   `ધોળકા ટાઉન`=Dholka Town). So:
   - Specialization comes from `officer_duties`/`employee_duties` → `duty_details`.
   - Posting/tenure history comes from the `services_periods*` tables.
   These are two different signals — keep them separate.

5. **Some `duty_details` rows have ids with gaps** (no id 69, 74, 75) and a few
   have trailing whitespace/newlines. Trim before mapping.

---

## 5. Domain tables — columns that matter

### `officer_details`
`id, police_station_branch_id, name, designation, study, mode, batch, gender,
date_of_birth, appointment_date, posting_date` (+ PII columns to IGNORE:
`aadhar_number, pan_number, mobile_number, home_address, current_address, photo`).

### `employee_details`
`id, police_station_branch_id, name, designation, buckle_no, study, gender,
date_of_birth, police_station_present_date` (+ same PII to ignore).

### `police_station_branch`
`id, police_station_branch_name, division_id, is_active` (+ auth columns to ignore).

### `divisions`
`id, name, is_active`. The 6 divisions (Gujarati names):
`1=અસલાલી (Aslali), 2=સાણંદ (Sanand), 3=વિરમગામ (Viramgam), 4=ધોળકા (Dholka),
5=ધંધુકા (Dhandhuka), 6=અન્ય શાખા (Other Branches)`.

### `duty_details`
`id, duty_detail`. 77 rows. See full Gujarati→English→spec_code mapping in
`02_reference_data.sql`.

### `officer_duties` / `employee_duties`
Link tables: `(officer_id|employee_id, duty_id)`. Many-to-many person↔duty.

### `services_periods` / `services_periods_emp`
`id, officer_details_id|employee_details_id, duties_performed (=PLACE), start_date,
end_date, remarks`.

### `officer_awards` / `employee_awards`
`(id, officer_id|employee_id, award_name, award_date, ...)`.

### `officer_punishments` / `employee_punishments`
`(id, officer_id|employee_id, punishment_type, punishment_date, ...)`.

### Strength tables (availability signal)
- `officer_approved_strength` / `officer_present_strength`: `pi, psi, total` per station.
- `employee_approved_strength` / `employee_present_strength`: per-rank counts + `total`.
  (Note: `employee_approved_strength` has grouped columns `UASI/UHC, UPC/ULR, AASI/AHC, APC/ALR`.)
- Vacancy = approved.total − present.total.

### `workforce`
Per-station workload metrics: `cases_registered, summons, notices, bw, nbw,
control_room_dial_100, sum_total_bandobast_days`, etc.

---

## 6. Privacy constraint (non-negotiable)

Government owns this data. **No external API, cloud service, or network call is
permitted** for any data processing, ML, or NLP. Everything runs on-prem:
local PostgreSQL, local Python, and a local LLM (e.g. via Ollama / llama.cpp)
for the NLP layer. The build must never transmit officer data off-machine.

PII columns (`aadhar_number, pan_number, mobile_number, home_address,
current_address, photo`) must NOT enter the clean analytical store or the ML
feature set — they are irrelevant to recommendation/Q&A and increase risk.
