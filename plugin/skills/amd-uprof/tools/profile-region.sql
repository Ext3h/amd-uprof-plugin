SELECT COLUMNS(lambda c: projected(c))
FROM (
  SELECT page_start, page_end,
         module, samples,
         share(samples, total_samples) AS sample_share,
         share(weight, total_weight) AS time_share,
         share(tag_cycles, total_tag_cycles) AS tag_to_retire_share,
         "isSystemModule",
         samples * TRY_CAST(getvariable('uprof_sample_interval_s') AS DOUBLE) AS cpu_seconds,
         sampled_addresses,
         printf('0x%X', hot_rva) AS hot_rva,
         hot_samples AS hot_rva_samples,
         share(hot_samples, total_samples) AS hot_rva_share,
         hot_function AS "function", hot_file AS file, hot_line AS line,
         attribution, symbol_reason, function_source, function_derived AS synthetic_name,
         weight, tag_cycles, total_samples, total_weight, total_tag_cycles, sort_key, rn
  FROM v_region_rows
) q
ORDER BY sort_key, rn NULLS LAST;
