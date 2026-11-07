SELECT COLUMNS(lambda c: projected(c))
FROM (
  SELECT process, pid, samples,
         pct(samples, total_samples) AS pct_samples,
         pct(weight, total_weight) AS pct_time,
         pct(cycles, total_cycles) AS pct_cycles,
         weight, cycles, total_samples, total_weight, total_cycles, sort_key, rn
  FROM v_process_rows
) q
ORDER BY sort_key, rn NULLS LAST;
