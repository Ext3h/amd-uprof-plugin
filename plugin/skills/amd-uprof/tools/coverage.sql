-- Per-module function-name coverage over the scoped samples. Load views.sql first: v_scoped
-- applies the --pid/--image/--time-filter scope. A sample is named when its Function.name is
-- non-empty; a NULL function_id (no UnifiedSampleSeries address mapping) and a blank name both
-- count as unresolved. The window totals repeat on each row so the caller renders one line.
WITH per_module AS (
  SELECT w.module_name,
         CAST(sum(w.samples) AS BIGINT) AS samples,
         CAST(COALESCE(sum(w.samples) FILTER (WHERE COALESCE(f.name, '') <> ''), 0)
              AS BIGINT) AS named_samples
  FROM v_scoped w
  LEFT JOIN Function f ON f.id = w.function_id
  GROUP BY w.module_name
)
SELECT module_name, samples, named_samples,
       CAST(sum(samples) OVER () AS BIGINT) AS scoped_samples,
       CAST(sum(named_samples) OVER () AS BIGINT) AS named_total
FROM per_module
WHERE samples > 0
ORDER BY samples DESC, module_name;
