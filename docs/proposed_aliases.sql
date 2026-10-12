-- PROPOSED manufacturer aliases. NOT APPLIED: review, then run in psql (python -m b4p init does not load this file).
-- Each line maps a spelling seen in the sheet to the manufacturer name it should be filed under.
-- Lines are skipped (no error) if the canonical manufacturer does not exist. Safe to re-run.
-- Left out on purpose (needs a human call): co-brand/other-company spellings such as 'Bard-Parker / Aspen Surgical',
-- 'Hudson RCI / Medline', 'NOVAPLUS / Cardinal Health', 'CURAD (Medline)', 'namic / Medline Industries, LP',
-- 'Ethicon / Ferrosan Medical Devices', 'Kleenex / Kimberly-Clark Worldwide, Inc.', 'Medline Hudson RCI'.
-- After applying, run `python -m b4p sync --reprocess` once so rows already stored are re-filed too
-- (a normal sync only re-files new or changed rows). Then `b4p check --aliases` to re-check.
-- Local preview on 2026-10-11 (b4p_v2): 93 active rows re-filed, 228 -> 180 manufacturer spellings,
-- product names under more than one manufacturer 27 -> 6.

BEGIN;

-- BD
INSERT INTO manufacturer_alias (alias, manufacturer_id) SELECT 'becton, dickinson', manufacturer_id FROM manufacturer WHERE name = 'BD' ON CONFLICT DO NOTHING;
INSERT INTO manufacturer_alias (alias, manufacturer_id) SELECT 'becton, dickinson and company, usa', manufacturer_id FROM manufacturer WHERE name = 'BD' ON CONFLICT DO NOTHING;
INSERT INTO manufacturer_alias (alias, manufacturer_id) SELECT 'becton, dickinson and company,', manufacturer_id FROM manufacturer WHERE name = 'BD' ON CONFLICT DO NOTHING;
INSERT INTO manufacturer_alias (alias, manufacturer_id) SELECT 'becton, dickinson and company (bd)', manufacturer_id FROM manufacturer WHERE name = 'BD' ON CONFLICT DO NOTHING;
INSERT INTO manufacturer_alias (alias, manufacturer_id) SELECT 'bd (becton, dickinson and company)', manufacturer_id FROM manufacturer WHERE name = 'BD' ON CONFLICT DO NOTHING;
INSERT INTO manufacturer_alias (alias, manufacturer_id) SELECT 'bd becton dickinson and company', manufacturer_id FROM manufacturer WHERE name = 'BD' ON CONFLICT DO NOTHING;

-- ICU Medical
INSERT INTO manufacturer_alias (alias, manufacturer_id) SELECT 'icu medical, inc.', manufacturer_id FROM manufacturer WHERE name = 'ICU Medical' ON CONFLICT DO NOTHING;
INSERT INTO manufacturer_alias (alias, manufacturer_id) SELECT 'icumedical', manufacturer_id FROM manufacturer WHERE name = 'ICU Medical' ON CONFLICT DO NOTHING;

-- Hollister
INSERT INTO manufacturer_alias (alias, manufacturer_id) SELECT 'hollister incorporated', manufacturer_id FROM manufacturer WHERE name = 'Hollister' ON CONFLICT DO NOTHING;
INSERT INTO manufacturer_alias (alias, manufacturer_id) SELECT 'hollister / new image', manufacturer_id FROM manufacturer WHERE name = 'Hollister' ON CONFLICT DO NOTHING;

-- Medline
INSERT INTO manufacturer_alias (alias, manufacturer_id) SELECT 'medline industries, inc.', manufacturer_id FROM manufacturer WHERE name = 'Medline' ON CONFLICT DO NOTHING;

-- Mölnlycke
INSERT INTO manufacturer_alias (alias, manufacturer_id) SELECT 'mölnlycke health care ab', manufacturer_id FROM manufacturer WHERE name = 'Mölnlycke' ON CONFLICT DO NOTHING;

-- MediChoice
INSERT INTO manufacturer_alias (alias, manufacturer_id) SELECT 'medi choice', manufacturer_id FROM manufacturer WHERE name = 'MediChoice' ON CONFLICT DO NOTHING;
INSERT INTO manufacturer_alias (alias, manufacturer_id) SELECT 'medichoice / owens & minor, inc.', manufacturer_id FROM manufacturer WHERE name = 'MediChoice' ON CONFLICT DO NOTHING;

-- Teleflex
INSERT INTO manufacturer_alias (alias, manufacturer_id) SELECT 'teleflex medical', manufacturer_id FROM manufacturer WHERE name = 'Teleflex' ON CONFLICT DO NOTHING;
INSERT INTO manufacturer_alias (alias, manufacturer_id) SELECT 'teleflex - anesthesia', manufacturer_id FROM manufacturer WHERE name = 'Teleflex' ON CONFLICT DO NOTHING;
INSERT INTO manufacturer_alias (alias, manufacturer_id) SELECT 'teleflex / arrow', manufacturer_id FROM manufacturer WHERE name = 'Teleflex' ON CONFLICT DO NOTHING;
INSERT INTO manufacturer_alias (alias, manufacturer_id) SELECT 'arrow / teleflex', manufacturer_id FROM manufacturer WHERE name = 'Teleflex' ON CONFLICT DO NOTHING;
INSERT INTO manufacturer_alias (alias, manufacturer_id) SELECT 'arrow (teleflex)', manufacturer_id FROM manufacturer WHERE name = 'Teleflex' ON CONFLICT DO NOTHING;
INSERT INTO manufacturer_alias (alias, manufacturer_id) SELECT 'teleflex / z-medica, llc', manufacturer_id FROM manufacturer WHERE name = 'Teleflex' ON CONFLICT DO NOTHING;

-- Covidien
INSERT INTO manufacturer_alias (alias, manufacturer_id) SELECT 'covidien argyle', manufacturer_id FROM manufacturer WHERE name = 'Covidien' ON CONFLICT DO NOTHING;
INSERT INTO manufacturer_alias (alias, manufacturer_id) SELECT 'covidien / shiley', manufacturer_id FROM manufacturer WHERE name = 'Covidien' ON CONFLICT DO NOTHING;
INSERT INTO manufacturer_alias (alias, manufacturer_id) SELECT 'covidien / kendall', manufacturer_id FROM manufacturer WHERE name = 'Covidien' ON CONFLICT DO NOTHING;
INSERT INTO manufacturer_alias (alias, manufacturer_id) SELECT 'covidien / valleylab', manufacturer_id FROM manufacturer WHERE name = 'Covidien' ON CONFLICT DO NOTHING;

-- Cardinal Health
INSERT INTO manufacturer_alias (alias, manufacturer_id) SELECT 'cardinalhealth kendall', manufacturer_id FROM manufacturer WHERE name = 'Cardinal Health' ON CONFLICT DO NOTHING;
INSERT INTO manufacturer_alias (alias, manufacturer_id) SELECT 'cardinal health / kendall', manufacturer_id FROM manufacturer WHERE name = 'Cardinal Health' ON CONFLICT DO NOTHING;
INSERT INTO manufacturer_alias (alias, manufacturer_id) SELECT 'cardinal health / dover', manufacturer_id FROM manufacturer WHERE name = 'Cardinal Health' ON CONFLICT DO NOTHING;

-- Ambu
INSERT INTO manufacturer_alias (alias, manufacturer_id) SELECT 'ambu a/s', manufacturer_id FROM manufacturer WHERE name = 'Ambu' ON CONFLICT DO NOTHING;

-- HR Pharmaceuticals
INSERT INTO manufacturer_alias (alias, manufacturer_id) SELECT 'hr pharmaceuticals, inc.', manufacturer_id FROM manufacturer WHERE name = 'HR Pharmaceuticals' ON CONFLICT DO NOTHING;

-- Masimo
INSERT INTO manufacturer_alias (alias, manufacturer_id) SELECT 'masimo set', manufacturer_id FROM manufacturer WHERE name = 'Masimo' ON CONFLICT DO NOTHING;

-- smiths medical
INSERT INTO manufacturer_alias (alias, manufacturer_id) SELECT 'smiths medical jelco', manufacturer_id FROM manufacturer WHERE name = 'smiths medical' ON CONFLICT DO NOTHING;
INSERT INTO manufacturer_alias (alias, manufacturer_id) SELECT 'smiths medical jelco®', manufacturer_id FROM manufacturer WHERE name = 'smiths medical' ON CONFLICT DO NOTHING;
INSERT INTO manufacturer_alias (alias, manufacturer_id) SELECT 'smiths medical asd, inc.', manufacturer_id FROM manufacturer WHERE name = 'smiths medical' ON CONFLICT DO NOTHING;
INSERT INTO manufacturer_alias (alias, manufacturer_id) SELECT 'smiths medical asd, inc', manufacturer_id FROM manufacturer WHERE name = 'smiths medical' ON CONFLICT DO NOTHING;

-- Kleenex
INSERT INTO manufacturer_alias (alias, manufacturer_id) SELECT 'kleenex professional', manufacturer_id FROM manufacturer WHERE name = 'Kleenex' ON CONFLICT DO NOTHING;

-- DNAGenotek
INSERT INTO manufacturer_alias (alias, manufacturer_id) SELECT 'dna genotek inc.', manufacturer_id FROM manufacturer WHERE name = 'DNAGenotek' ON CONFLICT DO NOTHING;

-- HALYARD
INSERT INTO manufacturer_alias (alias, manufacturer_id) SELECT 'halyard*', manufacturer_id FROM manufacturer WHERE name = 'HALYARD' ON CONFLICT DO NOTHING;

-- Medtronic
INSERT INTO manufacturer_alias (alias, manufacturer_id) SELECT 'medtronic inc.', manufacturer_id FROM manufacturer WHERE name = 'Medtronic' ON CONFLICT DO NOTHING;

-- Dale Medical Products
INSERT INTO manufacturer_alias (alias, manufacturer_id) SELECT 'dale', manufacturer_id FROM manufacturer WHERE name = 'Dale Medical Products' ON CONFLICT DO NOTHING;
INSERT INTO manufacturer_alias (alias, manufacturer_id) SELECT 'dale medical products, inc.', manufacturer_id FROM manufacturer WHERE name = 'Dale Medical Products' ON CONFLICT DO NOTHING;

-- Centurion
INSERT INTO manufacturer_alias (alias, manufacturer_id) SELECT 'centurion medical products', manufacturer_id FROM manufacturer WHERE name = 'Centurion' ON CONFLICT DO NOTHING;
INSERT INTO manufacturer_alias (alias, manufacturer_id) SELECT 'centurion medical products, lp', manufacturer_id FROM manufacturer WHERE name = 'Centurion' ON CONFLICT DO NOTHING;

-- B. Braun
INSERT INTO manufacturer_alias (alias, manufacturer_id) SELECT 'b braun', manufacturer_id FROM manufacturer WHERE name = 'B. Braun' ON CONFLICT DO NOTHING;

-- Apothecary Products, LLC (Ezy Dose)
INSERT INTO manufacturer_alias (alias, manufacturer_id) SELECT 'apothecary products, llc', manufacturer_id FROM manufacturer WHERE name = 'Apothecary Products, LLC (Ezy Dose)' ON CONFLICT DO NOTHING;

-- ICON
INSERT INTO manufacturer_alias (alias, manufacturer_id) SELECT 'icon central laboratories', manufacturer_id FROM manufacturer WHERE name = 'ICON' ON CONFLICT DO NOTHING;

-- BARD
INSERT INTO manufacturer_alias (alias, manufacturer_id) SELECT 'bard (c. r. bard, inc.)', manufacturer_id FROM manufacturer WHERE name = 'BARD' ON CONFLICT DO NOTHING;
INSERT INTO manufacturer_alias (alias, manufacturer_id) SELECT 'bard access systems (c. r. bard, inc.)', manufacturer_id FROM manufacturer WHERE name = 'BARD' ON CONFLICT DO NOTHING;
INSERT INTO manufacturer_alias (alias, manufacturer_id) SELECT 'bardex', manufacturer_id FROM manufacturer WHERE name = 'BARD' ON CONFLICT DO NOTHING;

COMMIT;
