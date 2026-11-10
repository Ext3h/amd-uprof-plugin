SELECT COLUMNS(lambda c: projected(c))
FROM (
  SELECT "function" AS "function", module,
         function_start, function_end, function_source, function_derived,
         attribution, function_reason, samples,
         share(samples, total_samples) AS sample_share,
         share(weight, total_weight) AS time_share,
         share(tag_cycles, total_tag_cycles) AS tag_to_retire_share,
         "isSystemModule",
         samples * TRY_CAST(getvariable('uprof_sample_interval_s') AS DOUBLE) AS cpu_seconds,
         hot_rva, hot_samples,
         CAST(samples - hot_samples AS BIGINT) AS other_samples,
         share(hot_samples, total_samples) AS hot_sample_share,
         weight, tag_cycles, total_samples, total_weight, total_tag_cycles, sort_key, rn
  FROM v_function_rows
) q
ORDER BY sort_key, rn NULLS LAST;
