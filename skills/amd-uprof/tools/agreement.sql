-- uProf-versus-symbolizer agreement over the scoped addresses; views.sql defines v_scoped and
-- v_symbols, so load the schema first. The symbolizer name is the containing (outermost) frame,
-- because uProf's Function.id names the function that contains an inlined instance.
WITH addressed AS (
  SELECT w.module_name, w.rva,
         max(f.name) AS uprof_name,
         CAST(sum(w.samples) AS BIGINT) AS samples
  FROM v_scoped w
  LEFT JOIN Function f ON f.id = w.function_id
  GROUP BY w.module_name, w.rva
), joined AS (
  SELECT a.uprof_name, a.samples, s.containing_function AS sym_name
  FROM addressed a
  LEFT JOIN v_symbols s
    ON a.module_name = s.module_name AND a.rva = s.rva
), flagged AS (
  SELECT *,
         uprof_name IS NOT NULL AND uprof_name <> ''
           AND sym_name IS NOT NULL AND sym_name <> '' AS compared,
         sym_name IS NULL OR sym_name = '' AS unresolved
  FROM joined
)
SELECT 100.0 * COALESCE(sum(samples) FILTER (WHERE compared AND sym_name <> uprof_name), 0)
             / NULLIF(sum(samples) FILTER (WHERE compared), 0) AS mismatch_rate,
       CAST(COALESCE(sum(samples) FILTER (WHERE compared AND sym_name <> uprof_name), 0)
            AS BIGINT) AS mismatched_samples,
       CAST(COALESCE(sum(samples) FILTER (WHERE compared), 0) AS BIGINT) AS compared_samples,
       count(*) FILTER (WHERE compared AND sym_name <> uprof_name) AS name_mismatches,
       count(*) FILTER (WHERE compared) AS compared_addresses,
       count(*) FILTER (WHERE unresolved) AS unresolved,
       CAST(COALESCE(sum(samples) FILTER (WHERE unresolved), 0) AS BIGINT) AS unresolved_samples,
       100.0 * COALESCE(sum(samples) FILTER (WHERE unresolved), 0)
             / NULLIF(sum(samples), 0) AS unresolved_pct,
       CAST(COALESCE(sum(samples), 0) AS BIGINT) AS scoped_samples
FROM flagged;
