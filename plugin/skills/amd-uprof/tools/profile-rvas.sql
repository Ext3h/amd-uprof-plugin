-- The module path is the capture's own spelling of the module file; empty when Module has no row
-- for it. Addresses stay 0x-prefixed hex: llvm-symbolizer reads them as module offsets.
SELECT DISTINCT COALESCE(NULLIF(w.module_name, ''), '') AS module_name,
       COALESCE(mp.module_path, '') AS module_path,
       printf('0x%X', w.rva) AS rva
FROM v_scoped w
LEFT JOIN (
  SELECT module_key(path) AS module_name, max(path) AS module_path
  FROM Module
  GROUP BY module_name
) mp ON w.module_name = mp.module_name
WHERE w.rva IS NOT NULL AND COALESCE(w.module_name, '') <> ''
ORDER BY module_name, rva;
