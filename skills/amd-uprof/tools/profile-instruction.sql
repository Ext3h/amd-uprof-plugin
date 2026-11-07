SELECT COLUMNS(lambda c: projected(c))
FROM (
  SELECT module, rva, "function", "file", line, attribution, samples,
         pct(samples, total_samples) AS pct_samples,
         pct(weight, total_weight) AS pct_time,
         pct(cycles, total_cycles) AS pct_cycles,
         weight, cycles, total_samples, total_weight, total_cycles, sort_key, rn
  FROM v_instruction_rows
) q
ORDER BY sort_key, rn NULLS LAST;
