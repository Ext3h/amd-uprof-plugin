-- Public symbol names grouped by start RVA, from the (rva, name) pairs llvm-pdbutil listed.
-- An empty name reads as NULL; NULL names sort after named ones and stay out of the label.
-- names lists the first four names in sorted order, then '... (+N more)' when the start has
-- more pairs and at least one of the four is a name; name_count counts every pair.
WITH pairs AS (
  SELECT CAST(rva AS BIGINT) AS rva, NULLIF(name, '') AS name
  FROM read_csv(getvariable('uprof_public_pairs'), header = true, all_varchar = true,
                columns = {'rva': 'VARCHAR', 'name': 'VARCHAR'})
), grouped AS (
  SELECT rva, CAST(count(*) AS BIGINT) AS name_count,
         list(name ORDER BY name NULLS LAST) AS ordered
  FROM pairs
  GROUP BY rva
), labelled AS (
  SELECT rva, name_count,
         array_to_string(list_filter(ordered[1:4], lambda name: name IS NOT NULL),
                         ' | ') AS label
  FROM grouped
)
SELECT rva, name_count,
       label || CASE WHEN name_count > 4 AND label <> ''
                     THEN ' | ... (+' || CAST(name_count - 4 AS VARCHAR) || ' more)'
                     ELSE '' END AS names
FROM labelled
ORDER BY rva;
