-- Modules with samples whose uProf Function.name is empty, plus their on-disk path. Only these
-- modules need function ranges derived from the binary; a fully named module keeps its
-- Function.id rows and pays no llvm-readobj run. The caller skips a row with no path or an
-- absent file and reports that module as unresolved.
SELECT COALESCE(NULLIF(w.module_name, ''), '<unknown module>') AS module_name,
       COALESCE(mp.module_path, '') AS module_path,
       CAST(sum(w.samples) FILTER (WHERE COALESCE(f.name, '') = '') AS BIGINT) AS unresolved_samples,
       CAST(sum(w.samples) AS BIGINT) AS samples
FROM v_scoped w
LEFT JOIN Function f ON f.id = w.function_id
LEFT JOIN (
  SELECT module_key(path) AS module_name, max(path) AS module_path
  FROM Module
  GROUP BY module_key(path)
) mp ON w.module_name = mp.module_name
GROUP BY 1, 2
HAVING sum(w.samples) FILTER (WHERE COALESCE(f.name, '') = '') > 0
ORDER BY module_name;
