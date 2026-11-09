SELECT COLUMNS(lambda c: projected(c))
FROM (
  SELECT module, samples,
         share(samples, total_samples) AS sample_share,
         share(weight, total_weight) AS time_share,
         share(tag_cycles, total_tag_cycles) AS tag_to_retire_share,
         "isSystemModule",
         samples * TRY_CAST(getvariable('uprof_sample_interval_s') AS DOUBLE) AS cpu_seconds,
         CASE WHEN hot_rva IS NULL THEN NULL ELSE printf('0x%X', hot_rva) END AS hot_rva,
         hot_samples,
         share(hot_samples, total_samples) AS hot_sample_share,
         weight, tag_cycles, total_samples, total_weight, total_tag_cycles, sort_key, rn
  FROM v_module_rows
) q
ORDER BY sort_key, rn NULLS LAST;
