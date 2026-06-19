-- ============================================================================
-- Reference / controlled-vocabulary seed data for the clean store.
-- Hand-built from the actual source values (designations + 77 duty_details).
-- ============================================================================
SET search_path TO clean, public;

-- ---------------------------------------------------------------------------
-- RANKS  (rank_order: higher = more senior)
-- Officers (gazetted) sit above employees (non-gazetted).
-- ---------------------------------------------------------------------------
INSERT INTO clean.rank_ref (rank_code, rank_band, rank_name_en, rank_name_gu, rank_order) VALUES
    ('PI',   'officer',  'Police Inspector',                'પોલીસ ઇન્સ્પેક્ટર',        90),
    ('PSI',  'officer',  'Police Sub-Inspector',            'પોલીસ સબ ઇન્સ્પેક્ટર',     80),
    ('AASI', 'employee', 'Armed Assistant Sub-Inspector',   'સશસ્ત્ર મદદનીશ સ.ઇ.',      60),
    ('UASI', 'employee', 'Unarmed Assistant Sub-Inspector', 'નિઃશસ્ત્ર મદદનીશ સ.ઇ.',    58),
    ('AHC',  'employee', 'Armed Head Constable',            'સશસ્ત્ર હેડ કોન્સ્ટેબલ',    50),
    ('UHC',  'employee', 'Unarmed Head Constable',          'નિઃશસ્ત્ર હેડ કોન્સ્ટેબલ',  48),
    ('APC',  'employee', 'Armed Police Constable',          'સશસ્ત્ર પોલીસ કોન્સ્ટેબલ',  40),
    ('UPC',  'employee', 'Unarmed Police Constable',        'નિઃશસ્ત્ર પોલીસ કોન્સ્ટેબલ',38),
    ('ALR',  'employee', 'Armed Lokrakshak',                'સશસ્ત્ર લોકરક્ષક',          30),
    ('ULR',  'employee', 'Unarmed Lokrakshak',              'નિઃશસ્ત્ર લોકરક્ષક',        28)
ON CONFLICT (rank_code) DO NOTHING;

-- ---------------------------------------------------------------------------
-- SPECIALIZATIONS  (the controlled vocabulary duties map onto)
-- ---------------------------------------------------------------------------
INSERT INTO clean.specialization_ref (spec_code, spec_name_en, category) VALUES
    ('CYBER',          'Cyber Crime',                'Investigation'),
    ('CRIME_INVEST',   'Crime Investigation',        'Investigation'),
    ('SOG',            'Special Operations Group',   'Investigation'),
    ('TRAFFIC',        'Traffic',                    'Field'),
    ('PATROL_MOBILE',  'Patrol / Mobile',            'Field'),
    ('PCR_112',        'PCR / 112 Response',         'Field'),
    ('FIELD_GENERAL',  'General Field Duty',         'Field'),
    ('WOMEN_SAFETY',   'Women Safety (SHE Team/Help Desk)', 'Community'),
    ('CHILD_WELFARE',  'Child Welfare / SPC',        'Community'),
    ('COMMUNITY',      'Community Policing (Suraksha Setu)', 'Community'),
    ('COURT_LEGAL',    'Court / Summons / Warrant',  'Legal'),
    ('GUARD_SECURITY', 'Guard / Escort / Security',  'Security'),
    ('VIP_PROTECTION', 'VIP / Bungalow Protection',  'Security'),
    ('ARMOURY',        'Armoury / Weapons',          'Security'),
    ('COMMANDO',       'Commando / Gunman',          'Security'),
    ('CONTROL_ROOM',   'Control Room / Wireless',    'Operations'),
    ('IT_COMPUTER',    'Computer / IT Operations',   'Operations'),
    ('DRIVER',         'Driver',                     'Operations'),
    ('DOG_SQUAD',      'Dog Handler',                'Operations'),
    ('WRITER_CLERK',   'Writer / Clerical',          'Administration'),
    ('ACCOUNTS',       'Accounts',                   'Administration'),
    ('REGISTRY',       'Registry / Application Branch', 'Administration'),
    ('STORE',          'Store / Quartermaster',      'Administration'),
    ('ADMIN_GENERAL',  'General Administration',     'Administration'),
    ('STATION_INCHARGE','Station / Post In-charge',  'Command'),
    ('PSO',            'Personal Security Officer',  'Security'),
    ('UNCLASSIFIED',   'Unclassified (needs review)', 'Review')
ON CONFLICT (spec_code) DO NOTHING;

-- ---------------------------------------------------------------------------
-- DUTY MAP: every source duty_id -> spec_code.
-- duty_detail_raw is filled by the pipeline from the live table; here we set
-- the mapping + English label. Unmapped -> UNCLASSIFIED + needs_review.
-- ---------------------------------------------------------------------------
INSERT INTO clean.duty_map (source_duty_id, duty_detail_raw, duty_detail_en, spec_code, needs_review) VALUES
    (1,  'PI રાઈટર',                       'PI Writer',                    'WRITER_CLERK',    FALSE),
    (2,  'PSI રાઈટર',                      'PSI Writer',                   'WRITER_CLERK',    FALSE),
    (3,  'ક્રાઈમ રાઈટર',                    'Crime Writer',                 'CRIME_INVEST',    FALSE),
    (4,  'એકાઉન્ટ રાઈટર',                  'Account Writer',               'ACCOUNTS',        FALSE),
    (5,  'LIB',                            'Local Intelligence Branch',    'CRIME_INVEST',    FALSE),
    (6,  'ફિલ્ડ LIB',                      'Field LIB',                    'CRIME_INVEST',    FALSE),
    (7,  'બારનીશી',                        'Barnishi (Court Clerk)',       'COURT_LEGAL',     FALSE),
    (8,  'કોર્ટ ડ્યુટી',                    'Court Duty',                   'COURT_LEGAL',     FALSE),
    (9,  'પીએસઓ',                          'PSO',                          'PSO',             FALSE),
    (10, 'VHF ઓપરેટર',                     'VHF Operator',                 'CONTROL_ROOM',    FALSE),
    (11, 'કમ્પ્યુટર ઓપરેટર',                'Computer Operator',            'IT_COMPUTER',     FALSE),
    (12, 'ડી સ્ટાફ',                       'D Staff (Detection)',          'CRIME_INVEST',    FALSE),
    (13, 'ડ્રાઈવર',                        'Driver',                       'DRIVER',          FALSE),
    (14, 'ઓ.પી /બીટ/ ચોકી ઇન્ચાર્જ',        'OP/Beat/Chowki In-charge',     'STATION_INCHARGE',FALSE),
    (15, 'ઓ.પી /બીટ/ ચોકી મદદ',            'OP/Beat/Chowki Support',       'FIELD_GENERAL',   FALSE),
    (16, 'ઓ.પી /બીટ/ ચોકી રાઈટર',          'OP/Beat/Chowki Writer',        'WRITER_CLERK',    FALSE),
    (17, 'સાયબર',                          'Cyber',                        'CYBER',           FALSE),
    (18, 'ટ્રાફિક',                        'Traffic',                      'TRAFFIC',         FALSE),
    (19, 'MOB',                            'Mobile',                       'PATROL_MOBILE',   FALSE),
    (20, 'શી ટીમ',                         'SHE Team',                     'WOMEN_SAFETY',    FALSE),
    (21, 'PCR ઇન્ચાર્જ',                   'PCR In-charge',                'PCR_112',         FALSE),
    (22, '112 ઇન્ચાર્જ એએસઆઈ/એચસી અને પીસી','112 In-charge',               'PCR_112',         FALSE),
    (23, 'મહિલા હેલ્પ ડેસ્ક',              'Women Help Desk',              'WOMEN_SAFETY',    FALSE),
    (24, 'જેલ ગાર્ડ/તિજોરી ગાર્ડ',          'Jail/Treasury Guard',          'GUARD_SECURITY',  FALSE),
    (25, 'સમન્સ/વોરંટ',                    'Summons/Warrant',              'COURT_LEGAL',     FALSE),
    (26, 'જનરલ ડ્યુટી',                    'General Duty',                 'FIELD_GENERAL',   FALSE),
    (27, 'ડેપ્યુટેશન',                     'Deputation',                   'ADMIN_GENERAL',   FALSE),
    (28, 'ટુ –મોબાઇલ',                     'Two-Mobile',                   'PATROL_MOBILE',   FALSE),
    (29, 'મિસીંગ',                         'Missing Persons',              'CRIME_INVEST',    FALSE),
    (30, 'સુરક્ષા સેતુ ઇન્ચાર્જ',           'Suraksha Setu In-charge',      'COMMUNITY',       FALSE),
    (31, 'એસ.પી.સી/ચાઇલ્ડ વેલફેર',         'SPC/Child Welfare',            'CHILD_WELFARE',   FALSE),
    (32, 'સી.એમ.ગાર્ડ',                    'CM Guard',                     'VIP_PROTECTION',  FALSE),
    (33, 'એસ્કોટીંગ',                      'Escorting',                    'GUARD_SECURITY',  FALSE),
    (34, 'ટ્રેઝરી ગાર્ડ',                   'Treasury Guard',               'GUARD_SECURITY',  FALSE),
    (35, 'સબ જેલ ગાર્ડ',                   'Sub Jail Guard',               'GUARD_SECURITY',  FALSE),
    (36, 'વન–મોબાઇલ',                      'One-Mobile',                   'PATROL_MOBILE',   FALSE),
    (37, 'રાઇટર',                          'Writer',                       'WRITER_CLERK',    FALSE),
    (38, 'એસ.ઓ.જી.-સાણંદ ડિવિઝન ફીલ્ડ',    'SOG Sanand Division Field',    'SOG',             FALSE),
    (39, 'એસ.ઓ.જી.-ધોળકા ડિવિઝન ફીલ્ડ',    'SOG Dholka Division Field',    'SOG',             FALSE),
    (40, 'એસ.ઓ.જી.-વિરમગામ ડિવિઝન ફીલ્ડ',  'SOG Viramgam Division Field',  'SOG',             FALSE),
    (41, 'એસ.ઓ.જી.-ટેકનીકલ',               'SOG Technical',                'SOG',             FALSE),
    (42, 'એસ.ઓ.જી.-ઓપરેટર',                'SOG Operator',                 'SOG',             FALSE),
    (43, 'ઓફીસ વર્ક',                      'Office Work',                  'ADMIN_GENERAL',   FALSE),
    (44, 'ફિલ્ડવર્ક',                      'Field Work',                   'FIELD_GENERAL',   FALSE),
    (45, 'પેરોલ',                          'Patrol',                       'PATROL_MOBILE',   FALSE),
    (46, 'થાણા અધિ.શ્રી',                  'Station Officer',              'STATION_INCHARGE',FALSE),
    (47, 'સ્ટોર જમાદાર',                   'Store Jamadar',                'STORE',           FALSE),
    (48, 'આર્મોરર',                        'Armourer',                     'ARMOURY',         FALSE),
    (49, 'હાજરી માસ્તર',                   'Attendance Master',            'ADMIN_GENERAL',   FALSE),
    (50, 'સી.પી.સી. કેન્ટીન',              'CPC Canteen',                  'ADMIN_GENERAL',   FALSE),
    (51, 'લાઇન જમાદાર',                    'Line Jamadar',                 'ADMIN_GENERAL',   FALSE),
    (52, 'કમાન્ડો',                        'Commando',                     'COMMANDO',        FALSE),
    (53, 'અધિ.સા.શ્રીનાઓના બંગલે પિકેટ',    'Senior Officer Bungalow Picket','VIP_PROTECTION', FALSE),
    (54, 'ગનમેન',                          'Gunman',                       'COMMANDO',        FALSE),
    (55, 'કલેકટર સા.શ્રી.ના બંગલે પિકેટ',   'Collector Bungalow Picket',    'VIP_PROTECTION',  FALSE),
    (56, 'ડીસ્ટ્રીક્ટ જજ સા.શ્રીના બંગલે પિકેટ','District Judge Bungalow Picket','VIP_PROTECTION',FALSE),
    (57, 'કલેકટર સા.શ્રી.ની કચેરી ફ્લડ કંટ્રોલ','Collector Office Flood Control','CONTROL_ROOM',FALSE),
    (58, 'અરજી શાખા',                      'Application Branch',           'REGISTRY',        FALSE),
    (59, 'બેંન્ક ઓર્ડલી',                  'Bank Orderly',                 'GUARD_SECURITY',  FALSE),
    (60, 'રજીસ્ટ્રી શાખા',                 'Registry Branch',              'REGISTRY',        FALSE),
    (61, 'કંટ્રોલ ઇન્ચાર્જ',               'Control In-charge',            'CONTROL_ROOM',    FALSE),
    (62, 'કંટ્રોલ',                        'Control Room',                 'CONTROL_ROOM',    FALSE),
    (63, 'રજીસ્ટ્રી શાખા',                 'Registry Branch',              'REGISTRY',        FALSE),
    (64, 'કોમ્પ્યુટર શાખા',                'Computer Branch',              'IT_COMPUTER',     FALSE),
    (65, 'ડોગ હેન્ડલર',                    'Dog Handler',                  'DOG_SQUAD',       FALSE),
    (66, 'એ.ડી.આઇ.',                       'ADI',                          'ADMIN_GENERAL',   FALSE),
    (67, 'ડે.ઓફીસર',                       'Day Officer',                  'STATION_INCHARGE',FALSE),
    (68, 'એકાઉન્ટ શાખા',                   'Accounts Branch',              'ACCOUNTS',        FALSE),
    (70, 'આર.પી.આઇ.',                      'RPI',                          'ADMIN_GENERAL',   FALSE),
    (71, 'આર.એસ.આઇ',                       'RSI',                          'ADMIN_GENERAL',   FALSE),
    (72, 'આર.એસ.આઇ',                       'RSI',                          'ADMIN_GENERAL',   FALSE),
    (73, 'સેકન્ડ પો.ઇન્સ.શ્રી',            'Second Police Inspector',      'STATION_INCHARGE',FALSE),
    (76, 'રનર',                            'Runner',                       'ADMIN_GENERAL',   FALSE),
    (77, 'ડાયલ-૧૦૦',                       'Dial-100',                     'PCR_112',         FALSE),
    (78, 'ક્વાર્ટર ગાર્ડ',                 'Quarter Guard',                'GUARD_SECURITY',  FALSE),
    (79, 'ફ.મો.',                          'F.Mo.',                        'PATROL_MOBILE',   FALSE),
    (80, 'એન્ટી સબોટેજ',                   'Anti-Sabotage',                'SOG',             FALSE)
ON CONFLICT (source_duty_id) DO NOTHING;
