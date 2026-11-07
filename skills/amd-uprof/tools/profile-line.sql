SELECT COLUMNS(lambda c: projected(c))
FROM (
  SELECT "file", line, "function", module, attribution, samples,
         share(samples, total_samples) AS sample_share,
         share(weight, total_weight) AS time_share,
         share(tag_cycles, total_tag_cycles) AS tag_to_retire_share,
         weight, tag_cycles, total_samples, total_weight, total_tag_cycles, sort_key, rn
  FROM v_line_rows
) q
ORDER BY sort_key, rn NULLS LAST;
