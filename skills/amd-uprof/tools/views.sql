-- Single schema for the profile command. uprof.py binds the uprof_* inputs as prepared
-- parameters, then loads this file once per run; every profile file is a projection over these
-- views. A hand run sets the same inputs with SET VARIABLE.
--
-- Bound inputs:
--   uprof_image         process base-image ILIKE pattern; '%' selects every process
--   uprof_pid           one PID as text; empty selects every process
--   uprof_top           row cap; 20 by default, 0 means no cap
--   uprof_time_start    window start in ns from capture start (inclusive)
--   uprof_time_end      window end in ns (exclusive)
--   uprof_ibs_dump      absolute IBS ASCII dump path; empty for a PMC capture
--   uprof_event_column  PMC sample column in UnifiedSampleSeries; empty for a dump capture
--   uprof_session_meta  session.uprof path when the file exists, else empty
--   uprof_tag_available 'true' only for an IBS OP dump whose IbsTagToRetCtr exists and has a
--                       non-null value; Python derives it from the sampler kind and v_dump_sanity
--   uprof_symbols       combined symbols.parquet path for line and instruction
--   uprof_aliases       combined symbols.publics.csv path for fold labelling
--   uprof_pdbs          combined symbols.pdbs.csv path: the PDB symbolization chose per module
--
-- The SET VARIABLE statements below are the mode flags, guards and typed top/time values; no view
-- file carries its own setup. The guards run before v_dump, so a bad capture fails once, with a
-- message naming what is missing.

-- Macros are TEMP because cpu.db is opened read-only.
CREATE OR REPLACE TEMP MACRO module_key(p) AS replace(lower(p), chr(92), '/');
-- A share is a fraction of the scoped total (0.0-1.0); the unit is stated once in the output.
CREATE OR REPLACE TEMP MACRO share(part, whole) AS part / NULLIF(whole, 0);
-- A kernel image is loaded in the kernel half of the x64 address space, or, when the catalog has
-- no instance to ask, has a basename only the kernel uses. 0xFFFF800000000000 starts that half;
-- the Windows kernel and HAL carry no .sys suffix, and Linux core images carry no suffix at all.
-- The basename match is required: a suffix test for hal.dll also matches user-mode marshal.dll,
-- and Module.isSystemModule is true for user-mode ntdll.dll and kernelbase.dll.
CREATE OR REPLACE TEMP MACRO kernel_basename(p) AS regexp_extract(p, '([^/]+)$', 1);
CREATE OR REPLACE TEMP MACRO is_kernel_image(p, load_address) AS
    COALESCE(load_address >= 18446603336221196288, false)
    OR kernel_basename(p) IN ('vmlinux', 'vmlinuz', '[kernel.kallsyms]')
    OR regexp_matches(kernel_basename(p), '^(ntoskrnl|ntkrnlmp|ntkrnlpa|ntkrpamp)\.exe$')
    OR kernel_basename(p) = 'hal.dll'
    OR regexp_matches(kernel_basename(p), '\.(sys|ko)$');
-- Columns hidden from every projection: internal gap weights and totals, time_share unless the
-- dump exists, and tag_to_retire_share unless this sampler's tag column supplied values.
CREATE OR REPLACE TEMP MACRO projected(c) AS
    c NOT IN ('weight', 'tag_cycles', 'total_samples', 'total_weight', 'total_tag_cycles',
              'sort_key', 'rn')
    AND (getvariable('has_dump') OR c <> 'time_share')
    AND (getvariable('uprof_tag_available') = 'true' OR c <> 'tag_to_retire_share');

SET VARIABLE has_dump = (getvariable('uprof_ibs_dump') <> '');

-- Fail once, naming every missing table/column. A dump capture joins the capture tables for fold
-- labelling and function attribution; a PMC capture additionally reads its event column from
-- UnifiedSampleSeries.
SET VARIABLE schema_check = (
  SELECT CASE WHEN count(*) > 0
              THEN error('uProf export incomplete: missing ' || string_agg(item, ', ' ORDER BY item) ||
                         '; the capture probably comes from a uProf version with a different export')
              ELSE 1 END
  FROM (
    SELECT 'table ' || required_tables.table_name AS item
    FROM (VALUES ('UnifiedSampleSeries'), ('Process'), ('Module'), ('Function'))
         AS required_tables(table_name)
    WHERE NOT EXISTS (
      SELECT 1 FROM duckdb_tables() AS t
      WHERE lower(t.schema_name) = 'main' AND lower(t.table_name) = lower(required_tables.table_name))
    UNION ALL
    SELECT 'column ' || required_columns.table_name || '.' || required_columns.column_name AS item
    FROM (VALUES
      ('UnifiedSampleSeries', 'moduleId'), ('UnifiedSampleSeries', 'functionId'),
      ('UnifiedSampleSeries', 'moduleRVA'),
      ('Process', 'id'), ('Process', 'name'),
      ('Module', 'id'), ('Module', 'path'),
      ('Function', 'id'), ('Function', 'name')
    ) AS required_columns(table_name, column_name)
    WHERE NOT EXISTS (
      SELECT 1 FROM duckdb_columns() AS c
      WHERE lower(c.schema_name) = 'main' AND lower(c.table_name) = lower(required_columns.table_name)
        AND lower(c.column_name) = lower(required_columns.column_name))
    UNION ALL
    SELECT 'event column name ' || COALESCE(NULLIF(getvariable('uprof_event_column'), ''), '(none)')
           || ' (the name must begin with event#)'
    WHERE NOT getvariable('has_dump')
      AND NOT (COALESCE(NULLIF(getvariable('uprof_event_column'), ''), '') LIKE 'event#%')
    UNION ALL
    SELECT 'column UnifiedSampleSeries.' || getvariable('uprof_event_column')
    WHERE NOT getvariable('has_dump')
      AND COALESCE(NULLIF(getvariable('uprof_event_column'), ''), '') LIKE 'event#%'
      AND NOT EXISTS (
        SELECT 1 FROM duckdb_columns() AS c
        WHERE lower(c.schema_name) = 'main' AND lower(c.table_name) = 'unifiedsampleseries'
          AND lower(c.column_name) = lower(getvariable('uprof_event_column')))
  ) AS missing
);

-- The PMC branch reads processId; a dump capture reads only PID from the ASCII dump, so an export
-- without that column still works with a dump.
SET VARIABLE schema_check_pid = CASE WHEN NOT getvariable('has_dump') THEN (
  SELECT CASE WHEN count(*) = 0
              THEN error('uProf export incomplete: missing column UnifiedSampleSeries.processId; '
                         'the capture probably comes from a uProf version with a different export')
              ELSE 1 END
  FROM duckdb_columns() AS c
  WHERE lower(c.schema_name) = 'main' AND lower(c.table_name) = 'unifiedsampleseries'
    AND lower(c.column_name) = 'processid')
ELSE 1 END;

SET VARIABLE dump_guard = CASE
  WHEN NOT getvariable('has_dump') THEN 1
  WHEN NOT (SELECT count(*) > 0 FROM glob(getvariable('uprof_ibs_dump')))
    THEN error('IBS ASCII dump not found at ' || getvariable('uprof_ibs_dump') || '; generate it with AMDuProfCLI translate --ascii ibsop-event-dump on a COPY of the session')
  WHEN TRY_CAST(NULLIF(getvariable('uprof_time_end'), '') AS BIGINT)
       <= TRY_CAST(COALESCE(NULLIF(getvariable('uprof_time_start'), ''), '0') AS BIGINT)
    THEN error('the time filter ends before it starts; pass T1:T2 with T1 < T2')
  ELSE 1 END;

-- One DESCRIBE of the dump; a dump missing a column cannot be queried, and regenerating it is the
-- only fix.
SET VARIABLE dump_columns_guard = (
  SELECT CASE WHEN count(*) > 0
              THEN error('IBS ASCII dump ' || getvariable('uprof_ibs_dump') ||
                         ' is missing required column(s) ' ||
                         string_agg(missing_name, ', ' ORDER BY missing_name) ||
                         '; regenerate it with AMDuProfCLI translate --ascii ibsop-event-dump on a COPY of the session')
              ELSE 1 END
  FROM query(CASE WHEN getvariable('has_dump')
    THEN $cols$SELECT required.name AS missing_name
               FROM (VALUES ('DeltaTick'), ('CPU_Number'), ('TID'), ('PID'), ('Offset'), ('ModuleName'))
                    AS required(name)
               WHERE NOT EXISTS (
                 SELECT 1
                 FROM (DESCRIBE SELECT * FROM read_csv(getvariable('uprof_ibs_dump'),
                                                      header = true, all_varchar = true)) AS d
                 WHERE d.column_name = required.name)$cols$
    ELSE 'SELECT NULL::VARCHAR AS missing_name WHERE false' END)
);

-- Only IBS OP dumps carry a tag-to-retire counter; when the column is absent
-- tag_to_retire_share is omitted rather than filled with zeroes.
SET VARIABLE has_tag = (
  SELECT has_tag FROM query(CASE WHEN getvariable('has_dump')
    THEN $tag$SELECT count(*) > 0 AS has_tag
              FROM (DESCRIBE SELECT * FROM read_csv(getvariable('uprof_ibs_dump'),
                                                    header = true, all_varchar = true))
              WHERE column_name = 'IbsTagToRetCtr'$tag$
    ELSE 'SELECT false AS has_tag' END)
);

-- Older exports carry no ModuleInstance table; the kernel check then has only module paths.
SET VARIABLE has_module_instances = (
  SELECT count(*) > 0 FROM duckdb_tables()
  WHERE lower(schema_name) = 'main' AND lower(table_name) = 'moduleinstance');

SET VARIABLE top_n = greatest(CAST(COALESCE(getvariable('uprof_top'), 20) AS BIGINT), 0);
SET VARIABLE time_start = CAST(COALESCE(NULLIF(getvariable('uprof_time_start'), ''), '0') AS BIGINT);
SET VARIABLE time_end = CAST(COALESCE(NULLIF(getvariable('uprof_time_end'), ''), '9223372036854775807') AS BIGINT);

-- DeltaTick is a cumulative, start-relative nanosecond timestamp; all columns are read as text so
-- uProf's empty or non-numeric cells cannot force a column type. tid identifies the thread in an
-- attached capture: the achieved-cadence check uses it as the context there and cpu for a
-- system-wide capture. tag_cycles is the tag-to-retire cycle count of one sampled op (AMD: tagged
-- at dispatch, retired later; queue/stalls included), clipped at 65535, the 16-bit field's
-- maximum. Only an unsigned decimal integer cell counts; NULL, negative, fractional, exponent and
-- nan/inf spellings stay NULL and are excluded from the tag_to_retire_share denominator rather
-- than being charged the maximum. The empty branch gives a PMC capture a typed, empty relation so
-- the downstream views bind without touching a dump.
CREATE OR REPLACE TEMP VIEW v_dump AS
SELECT * FROM query(CASE
  WHEN NOT getvariable('has_dump') THEN $empty$
    SELECT CAST(NULL AS BIGINT) AS delta_tick,
           CAST(NULL AS INTEGER) AS cpu,
           CAST(NULL AS INTEGER) AS tid,
           CAST(NULL AS INTEGER) AS pid,
           CAST(NULL AS BIGINT) AS rva,
           CAST(NULL AS VARCHAR) AS module_name,
           CAST(NULL AS DOUBLE) AS tag_cycles
    WHERE false$empty$
  WHEN getvariable('has_tag') THEN $tagged$
    SELECT CAST(DeltaTick AS BIGINT) AS delta_tick,
           CAST(CPU_Number AS INTEGER) AS cpu,
           CAST(TID AS INTEGER) AS tid,
           CAST(PID AS INTEGER) AS pid,
           CAST("Offset" AS BIGINT) AS rva,
           module_key(ModuleName) AS module_name,
           -- The integer-only guard keeps the CSV reader from converting the column to DOUBLE and
           -- failing on a non-integer cell; signed, decimal, exponent and nan/inf spellings are
           -- NULL, never a clipped maximum.
           CASE WHEN regexp_full_match("IbsTagToRetCtr", '\s*[0-9]+\s*')
                THEN LEAST(TRY_CAST("IbsTagToRetCtr" AS DOUBLE), 65535)
                ELSE NULL END AS tag_cycles
    FROM read_csv(getvariable('uprof_ibs_dump'), header = true, all_varchar = true)$tagged$
  ELSE $untagged$
    SELECT CAST(DeltaTick AS BIGINT) AS delta_tick,
           CAST(CPU_Number AS INTEGER) AS cpu,
           CAST(TID AS INTEGER) AS tid,
           CAST(PID AS INTEGER) AS pid,
           CAST("Offset" AS BIGINT) AS rva,
           module_key(ModuleName) AS module_name,
           CAST(NULL AS DOUBLE) AS tag_cycles
    FROM read_csv(getvariable('uprof_ibs_dump'), header = true, all_varchar = true)$untagged$
END);

-- The capture span recorded in session.uprof, in capture-relative nanoseconds. Shared by the
-- dump-wide provenance check, the scoped coverage verdict and the weighting mode so none re-reads
-- the metadata.
CREATE OR REPLACE TEMP VIEW v_capture_span AS
WITH meta AS (
  SELECT * FROM query(CASE WHEN getvariable('uprof_session_meta') <> ''
    THEN $meta$SELECT translateCommand,
                      json_extract_string(collection, '$.schedule.elapsed') AS elapsed_ms,
                      json_extract_string(collection, '$.schedule.profileDuration') AS duration_s,
                      lower(json_extract_string(collection, '$.target.systemWide')) AS system_wide
               FROM read_json(getvariable('uprof_session_meta'),
                              columns = {'translateCommand': 'VARCHAR', 'collection': 'JSON'})$meta$
    ELSE $meta$SELECT NULL::VARCHAR AS translateCommand, NULL::VARCHAR AS elapsed_ms,
                      NULL::VARCHAR AS duration_s, NULL::VARCHAR AS system_wide$meta$ END)
), expected AS (
  SELECT translateCommand, system_wide,
         CASE WHEN translateCommand LIKE '%--time-filter%'
              THEN TRY_CAST(regexp_extract(
                     translateCommand, '--time-filter[ =]+([0-9.]+):([0-9.]+)', 1) AS DOUBLE) * 1e9
              END AS window_start_ns,
         CASE WHEN translateCommand LIKE '%--time-filter%'
              THEN TRY_CAST(regexp_extract(
                     translateCommand, '--time-filter[ =]+([0-9.]+):([0-9.]+)', 2) AS DOUBLE) * 1e9
              END AS window_end_ns,
         CASE
           WHEN translateCommand LIKE '%--time-filter%' THEN NULL
           WHEN TRY_CAST(elapsed_ms AS BIGINT) > 0 THEN TRY_CAST(elapsed_ms AS BIGINT) * 1e6
           WHEN TRY_CAST(duration_s AS BIGINT) > 0 THEN TRY_CAST(duration_s AS BIGINT) * 1e9
           ELSE NULL END AS elapsed_ns,
         CASE WHEN translateCommand LIKE '%--time-filter%' THEN 'window' ELSE 'elapsed' END AS span_label
  FROM meta
), recorded AS (
  -- A recorded translate window keeps DeltaTick start-relative, so its samples lie in
  -- [window_start_ns, window_end_ns), not in [0, width).
  SELECT translateCommand, system_wide, span_label,
         COALESCE(window_start_ns, 0.0) AS start_ns,
         COALESCE(window_end_ns, elapsed_ns) AS end_ns,
         COALESCE(window_end_ns - window_start_ns, elapsed_ns) AS expected_ns
  FROM expected
)
SELECT translateCommand, system_wide, span_label, start_ns, end_ns, expected_ns
FROM recorded;

-- A missing systemWide counts as attached, matching uprof_session.system_wide. An attached capture
-- is weighed per thread: a thread that migrates leaves a gap on every CPU it visited, and a
-- per-CPU lag charges those overlapping intervals separately.
SET VARIABLE attached = (
  SELECT COALESCE(system_wide, 'false') <> 'true' FROM v_capture_span
);

-- The weighting context: a CPU for a system-wide capture, one (pid, tid) thread for an attached
-- one. Exposed once so the lag and the sanity check cannot disagree about the partition. context
-- is the CPU number or the TID; context_pid is 0 system-wide and the PID attached.
CREATE OR REPLACE TEMP VIEW v_dump_context AS
SELECT *, CASE WHEN getvariable('attached') THEN pid ELSE 0 END AS context_pid,
          CASE WHEN getvariable('attached') THEN tid ELSE cpu END AS context
FROM v_dump;

-- The query window is applied before the lag, so the first sample per weighting context inside the
-- window has no preceding in-window interval and carries no weight. weight is that DeltaTick gap in
-- nanoseconds; a gap is charged whole to the later sample, so idle/wait time accrues to the sample
-- that ends it.
CREATE OR REPLACE TEMP VIEW v_dump_weighted AS
SELECT d.* EXCLUDE (context_pid, context),
       delta_tick - lag(delta_tick) OVER (
         PARTITION BY context_pid, context ORDER BY delta_tick) AS weight
FROM v_dump_context AS d
WHERE delta_tick >= getvariable('time_start') AND delta_tick < getvariable('time_end');

-- Provenance check for the whole dump, printed in the header of every dump-backed run. The weighted
-- CTE is unfiltered on purpose: the span check compares the dump with session.uprof, independent
-- of the query's --time-filter. The empty-dump and inconsistency errors surface when the view is
-- read. Its coverage verdict is dump-wide across all processes; v_scope_sanity judges the report's
-- --image/--pid/time-filter scope separately. A system-wide capture is judged as core time over
-- CPUs; an attached capture is judged per thread, because the CPUs a migrating thread visited are
-- a set of overlapping intervals rather than parallel capacity.
CREATE OR REPLACE TEMP VIEW v_dump_sanity AS
WITH weighted_all AS (
  SELECT *, delta_tick - lag(delta_tick) OVER (
      PARTITION BY context_pid, context ORDER BY delta_tick) AS weight
  FROM v_dump_context
), totals AS (
  SELECT count(*) AS dump_samples,
         count(DISTINCT cpu) AS dump_cpus,
         count(DISTINCT CASE WHEN getvariable('attached') THEN pid END) AS dump_pids,
         count(DISTINCT CASE WHEN getvariable('attached') THEN (pid, tid) END) AS dump_threads,
         count(*) FILTER (WHERE weight IS NULL) AS excluded_first,
         sum(weight) AS total_weight,
         max(delta_tick) AS latest_tick,
         count(tag_cycles) AS tag_values
  FROM weighted_all
), contexts AS (
  -- One row per weighting context: a CPU (system-wide) or a (pid, tid) thread (attached).
  SELECT context_pid, context,
         min(delta_tick) AS first_ns,
         max(delta_tick) AS last_ns,
         count(*) AS n,
         sum(weight) AS context_weight
  FROM weighted_all
  GROUP BY 1, 2
), recorded AS (
  SELECT * FROM v_capture_span
), boundary AS (
  SELECT max(n) AS max_context_samples,
         sum(first_ns - b.start_ns) AS leading_ns,
         sum(b.end_ns - last_ns) AS trailing_ns,
         min(first_ns) AS first_sample_ns,
         max(last_ns) AS max_last_ns,
         count(*) FILTER (WHERE first_ns < b.start_ns OR last_ns > b.end_ns) AS beyond_contexts,
         arg_max(context, b.start_ns + b.end_ns - last_ns + first_ns) AS worst_context
  FROM contexts CROSS JOIN recorded AS b
), busiest AS (
  -- The context with the most samples, so the coverage it reports belongs to that context.
  SELECT n, context_weight FROM contexts ORDER BY n DESC, context LIMIT 1
), activity AS (
  SELECT count(*) FILTER (WHERE n >= 0.1 * (SELECT n FROM busiest)) AS active_contexts
  FROM contexts
), measured AS (
  SELECT totals.dump_samples, totals.dump_cpus, totals.dump_pids, totals.dump_threads,
         totals.excluded_first, totals.total_weight, totals.latest_tick, totals.tag_values,
         recorded.expected_ns, recorded.span_label, recorded.system_wide,
         recorded.start_ns, recorded.end_ns,
         recorded.expected_ns * totals.dump_cpus AS expected_core_ns,
         boundary.max_context_samples,
         boundary.leading_ns, boundary.trailing_ns, boundary.first_sample_ns,
         boundary.max_last_ns, boundary.beyond_contexts, boundary.worst_context,
         activity.active_contexts, (SELECT n FROM busiest) AS busiest_samples,
         (SELECT context_weight FROM busiest) AS busiest_weight,
         CASE WHEN getvariable('attached') THEN 'TID' ELSE 'CPU' END AS context_label
  FROM totals, recorded, boundary, activity
)
SELECT CASE
  WHEN dump_samples = 0 THEN
    error('IBS ASCII dump ' || getvariable('uprof_ibs_dump') || ' has no sample rows; regenerate it with AMDuProfCLI translate --ascii ibsop-event-dump on a COPY of the session')
  WHEN expected_ns IS NULL OR expected_ns = 0 THEN
    'dump ' || getvariable('uprof_ibs_dump') || ': ' || CAST(dump_samples AS VARCHAR) ||
    ' samples, ' || CAST(excluded_first AS VARCHAR) ||
    ' first-per-' || context_label ||
    ' samples excluded; WARN (dump-wide across all processes): session.uprof has no usable duration; total weight was not checked against the capture span'
  WHEN NOT getvariable('attached') AND total_weight > expected_core_ns THEN
    error(printf('dump %s disagrees with session.uprof: %.3f s of core time over %d CPUs exceeds %s %.3f s x %d CPUs = %.3f s implied; delete the dump (or its cache directory) and re-run profile so it is regenerated on a copy of the session',
                 getvariable('uprof_ibs_dump'), total_weight / 1e9, dump_cpus,
                 span_label, expected_ns / 1e9, dump_cpus, expected_core_ns / 1e9))
  WHEN beyond_contexts > 0 THEN
    error(printf('dump %s disagrees with session.uprof: %d %s(s) hold samples outside the recorded %s [%.3f, %.3f) s (sample DeltaTick range %.3f..%.3f s); delete the dump (or its cache directory) and re-run profile so it is regenerated on a copy of the session',
                 getvariable('uprof_ibs_dump'), beyond_contexts, context_label,
                 span_label, start_ns / 1e9, end_ns / 1e9,
                 first_sample_ns / 1e9, max_last_ns / 1e9))
  WHEN getvariable('attached') THEN
    printf('dump %s: %d samples, %d first-per-%s samples excluded; OK (attached; dump-wide): %d process(es), %d TID(s), %d active %s (%d/%d samples in the busiest), %d CPU(s) visited; time_share partitions gaps by (PID, TID), so a migrating thread is counted once; a thread''s gaps include off-CPU time, so for a cycle-controlled capture sample_share (fixed cycles per sample) is the CPU-cycle share and the better ranking; the busiest TID covers %.3f s of the %s %.3f s span (%.3f s of thread time over %d TIDs)',
           getvariable('uprof_ibs_dump'), dump_samples, excluded_first, context_label,
           dump_pids, dump_threads, active_contexts,
           CASE WHEN active_contexts = 1 THEN 'TID' ELSE 'TIDs' END,
           busiest_samples, dump_samples, dump_cpus,
           COALESCE(busiest_weight, 0) / 1e9, span_label, expected_ns / 1e9,
           COALESCE(total_weight, 0) / 1e9, dump_threads)
  WHEN (end_ns - max_last_ns) > (expected_ns / greatest(max_context_samples, 1)) THEN
    printf('dump %s: %d samples, %d first-per-CPU samples excluded; WARN (dump-wide across all processes): the capture window ends %.3f s before the recorded span, longer than one sampling cadence, so late time is uncovered; boundary gaps %.3f s (leading %.3f s, trailing %.3f s, worst cpu %s)',
           getvariable('uprof_ibs_dump'), dump_samples, excluded_first,
           (end_ns - max_last_ns) / 1e9,
           (expected_core_ns - total_weight) / 1e9, leading_ns / 1e9, trailing_ns / 1e9,
           CAST(worst_context AS VARCHAR))
  ELSE
    printf('dump %s: %d samples, %d first-per-CPU samples excluded; OK (dump-wide across all processes): %.3f s of core time over %d CPUs against %s %.3f s x %d CPUs = %.3f s; boundary gaps %.3f s (leading %.3f s, trailing %.3f s, worst cpu %s)',
           getvariable('uprof_ibs_dump'), dump_samples, excluded_first,
           total_weight / 1e9, dump_cpus,
           span_label, expected_ns / 1e9, dump_cpus, expected_core_ns / 1e9,
           (expected_core_ns - total_weight) / 1e9, leading_ns / 1e9, trailing_ns / 1e9,
           CAST(worst_context AS VARCHAR))
END AS message,
CAST(tag_values AS BIGINT) AS tag_values
FROM measured;

-- Symbol artifact: one combined Parquet with a module_name column, else an empty typed view.
-- function/file/line are the innermost frame (the executed source position); function_short is
-- the same frame's short spelling, one identity per function across inline and out-of-line
-- copies; containing_function is the outermost frame, which is the name uProf's Function.id
-- carries and the agreement check compares against. The query(CASE) re-binds per query, so
-- uprof_symbols may be bound after this view is created.
CREATE OR REPLACE TEMP VIEW v_symbols AS
SELECT module_name, CAST(address AS BIGINT) AS rva, function, function_short,
       containing_function, file, line
FROM query(CASE WHEN getvariable('uprof_symbols') <> ''
  THEN $sym$SELECT module_name, address, function, function_short,
                   containing_function, file, line
            FROM read_parquet(getvariable('uprof_symbols'))$sym$
  ELSE $sym$SELECT NULL::VARCHAR AS module_name, NULL::VARCHAR AS address,
                   NULL::VARCHAR AS function, NULL::VARCHAR AS function_short,
                   NULL::VARCHAR AS containing_function, NULL::VARCHAR AS file,
                   NULL::INTEGER AS line WHERE false$sym$ END);

-- Public-symbol cache: module_name, rva, name_count, names. Empty when absent, so every address
-- stays 'unique' and no shared label is emitted.
CREATE OR REPLACE TEMP VIEW v_public_starts AS
SELECT * FROM query(CASE WHEN getvariable('uprof_aliases') <> ''
  THEN $pub$SELECT module_name, CAST(rva AS BIGINT) AS rva, CAST(name_count AS INTEGER) AS name_count, names
            FROM read_csv(getvariable('uprof_aliases'), header = true, all_varchar = true)$pub$
  ELSE $pub$SELECT NULL::VARCHAR AS module_name, NULL::BIGINT AS rva, NULL::INTEGER AS name_count,
                   NULL::VARCHAR AS names WHERE false$pub$ END);

-- PDB choice per module, recorded by symbolization: module_name, pdb. Empty when absent, so the
-- instruction context symbolizes without a PDB instead of discovering one a second time.
CREATE OR REPLACE TEMP VIEW v_pdbs AS
SELECT module_name, NULLIF(pdb, '') AS pdb
FROM query(CASE WHEN getvariable('uprof_pdbs') <> ''
  THEN $pdbs$SELECT module_name, pdb
              FROM read_csv(getvariable('uprof_pdbs'), header = true, all_varchar = true)$pdbs$
  ELSE $pdbs$SELECT NULL::VARCHAR AS module_name, NULL::VARCHAR AS pdb WHERE false$pdbs$ END);

-- Scope is every process image matching --image and, when given, the one PID named by --pid.
-- With a dump, samples are the DeltaTick-weighted ASCII rows and weight is the exact time; without
-- one (PMC), samples are the UnifiedSampleSeries event-column counts (fixed cycles; one row may
-- carry several), weight is NULL because there are no per-sample timestamps, and function_id comes
-- from the sample table. The process filter runs after weighting, so a gap is charged whole to the
-- sample that ends it. The COLUMNS lambda selects the event column by name, so the query string
-- needs no identifier splicing.
CREATE OR REPLACE TEMP VIEW v_scoped AS
SELECT * FROM query(CASE WHEN getvariable('has_dump') THEN $dump$
SELECT w.delta_tick, w.cpu, w.tid, w.pid, w.rva, w.module_name, w.weight,
       CAST(1 AS BIGINT) AS samples, w.tag_cycles,
       COALESCE(p.name, '<unknown process>') AS process,
       rf.function_id
FROM v_dump_weighted w
LEFT JOIN Process p ON p.id = w.pid
LEFT JOIN (
  SELECT module_key(m.path) AS module_name, CAST(s.moduleRVA AS BIGINT) AS rva,
         max(s.functionId) AS function_id
  FROM UnifiedSampleSeries s
  JOIN Module m ON m.id = s.moduleId
  WHERE s.functionId IS NOT NULL
  GROUP BY module_name, rva
) rf ON w.module_name = rf.module_name AND w.rva = rf.rva
WHERE (getvariable('uprof_pid') = '' OR w.pid = CAST(getvariable('uprof_pid') AS INTEGER))
  AND COALESCE(p.name, '') ILIKE getvariable('uprof_image')$dump$
ELSE $pmc$
SELECT CAST(NULL AS BIGINT) AS delta_tick,
       CAST(NULL AS INTEGER) AS cpu,
       CAST(NULL AS INTEGER) AS tid,
       CAST(s.processId AS INTEGER) AS pid,
       CAST(s.moduleRVA AS BIGINT) AS rva,
       module_key(m.path) AS module_name,
       CAST(NULL AS DOUBLE) AS weight,
       CAST(COLUMNS(lambda c: c = getvariable('uprof_event_column')) AS BIGINT) AS samples,
       CAST(NULL AS DOUBLE) AS tag_cycles,
       COALESCE(p.name, '<unknown process>') AS process,
       s.functionId AS function_id
FROM UnifiedSampleSeries s
LEFT JOIN Process p ON p.id = s.processId
LEFT JOIN Module m ON m.id = s.moduleId
WHERE (getvariable('uprof_pid') = '' OR s.processId = CAST(getvariable('uprof_pid') AS INTEGER))
  AND COALESCE(p.name, '') ILIKE getvariable('uprof_image')$pmc$ END);

-- Late-coverage verdict for the report's scope, appended to SANITY when --image/--pid or
-- --time-filter narrows the population below the whole dump. The scope window is the recorded
-- translate span clipped to the profile time filter; the scope's last sample is compared against
-- the end of that window using the same sampling-cadence threshold the dump-wide check uses.
-- Attached captures keep their OK (attached; dump-wide) verdict: their coverage is wherever the
-- attached threads ran. A missing systemWide counts as attached, matching uprof_session.system_wide.
-- An empty scope is already reported by the scope warning, so it gets no verdict here.
CREATE OR REPLACE TEMP VIEW v_scope_sanity AS
WITH span AS (
  SELECT start_ns, end_ns, system_wide,
         (getvariable('uprof_image') <> '%' OR getvariable('uprof_pid') <> ''
          OR getvariable('time_start') > 0
          OR getvariable('time_end') < 9223372036854775807) AS filtered,
         -- The scope window is the recorded span clipped to the profile time filter. A start-only
         -- filter leaves the end at the recorded span end instead of the unbounded default.
         GREATEST(COALESCE(start_ns, getvariable('time_start')),
                  getvariable('time_start')) AS scope_start_ns,
         CASE WHEN getvariable('time_end') < 9223372036854775807
              THEN LEAST(COALESCE(end_ns, getvariable('time_end')),
                         getvariable('time_end'))
              ELSE end_ns END AS scope_end_ns,
         concat_ws(' ',
           CASE WHEN getvariable('uprof_pid') <> ''
                THEN '--pid ' || getvariable('uprof_pid') END,
           CASE WHEN getvariable('uprof_image') <> '%'
                THEN '--image ' || getvariable('uprof_image') END,
           CASE WHEN getvariable('time_start') > 0
                     OR getvariable('time_end') < 9223372036854775807
                THEN printf('--time-filter %.3f:%.3f s',
                            getvariable('time_start') / 1e9,
                            getvariable('time_end') / 1e9) END)
           AS scope_label
  FROM v_capture_span
), scoped AS (
  -- A sampling context is one thread of one process. The cadence estimate is the window width
  -- divided by the busiest in-scope context's sample count, the same span/count form as the
  -- dump-wide check but measured on the scope's own population.
  SELECT max(last_tick) AS last_ns, sum(context_samples) AS scoped_samples,
         max(context_samples) AS max_context_samples
  FROM (
    SELECT pid, tid, max(delta_tick) AS last_tick, count(*) AS context_samples
    FROM v_scoped
    GROUP BY pid, tid
  )
), judged AS (
  SELECT span.*, scoped.last_ns, scoped.scoped_samples, scoped.max_context_samples,
         greatest(span.scope_end_ns - scoped.last_ns, 0) AS end_gap_ns,
         (span.scope_end_ns - span.scope_start_ns) / scoped.max_context_samples AS cadence_ns
  FROM span CROSS JOIN scoped
)
SELECT CASE
  WHEN NOT filtered THEN NULL
  WHEN COALESCE(system_wide, 'false') = 'false' THEN NULL
  WHEN COALESCE(scoped_samples, 0) = 0 THEN NULL
  WHEN last_ns IS NULL THEN NULL
  WHEN scope_end_ns IS NULL THEN NULL
  WHEN cadence_ns IS NULL OR cadence_ns <= 0 THEN NULL
  WHEN end_gap_ns > cadence_ns THEN
    printf('scoped coverage (%s): WARN: the last sample in scope is %.3f s before the scope window end at %.3f s, longer than one sampling cadence, so late time in the scope is uncovered',
           scope_label, end_gap_ns / 1e9, scope_end_ns / 1e9)
  ELSE
    printf('scoped coverage (%s): OK: the last sample in scope is %.3f s before the scope window end at %.3f s',
           scope_label, end_gap_ns / 1e9, scope_end_ns / 1e9)
END AS message
FROM judged;

-- total_samples is 0 for an empty scope so the caller can tell an empty filter from a failed one;
-- the profile row views compute their own window totals.
CREATE OR REPLACE TEMP VIEW v_scope_totals AS
SELECT CAST(COALESCE(sum(samples), 0) AS BIGINT) AS total_samples
FROM v_scoped;

-- One row per module path with its highest load address and whether that address is kernel-space,
-- or an empty relation when the export has no ModuleInstance table. v_capture_modules asks it
-- whether a module sits in kernel space, so the classification runs per module, not per sample.
CREATE OR REPLACE TEMP VIEW v_module_catalog AS
SELECT * FROM query(CASE WHEN getvariable('has_module_instances') THEN $instances$
  SELECT module_key(m.path) AS module_name,
         max(i.loadAddress) AS load_address,
         max(is_kernel_image(module_key(m.path), i.loadAddress)) AS kernel_image
  FROM Module m
  JOIN ModuleInstance i ON i.moduleId = m.id
  GROUP BY module_key(m.path)$instances$
ELSE $absent$
  SELECT CAST(NULL AS VARCHAR) AS module_name, CAST(NULL AS UBIGINT) AS load_address,
         CAST(NULL AS BOOLEAN) AS kernel_image
  WHERE false$absent$ END);

-- One row per whole-capture sample, before the process and time filters, with the module it landed
-- in, the context that produced it (CPU for a dump, coreId for PMC) and whether that module is a
-- kernel image. One dump row is one sample; a PMC row carries its event count. The ACHIEVED check
-- and the kernel-coverage check share this view, so both come from one scan: the kernel flag is
-- pruned when only the sample and context totals are asked for.
CREATE OR REPLACE TEMP VIEW v_capture_modules AS
SELECT * FROM query(CASE WHEN getvariable('has_dump') THEN $dump$
  SELECT d.module_name,
         CAST(d.cpu AS INTEGER) AS context,
         CAST(1 AS BIGINT) AS samples,
         COALESCE(c.kernel_image, is_kernel_image(d.module_name, NULL)) AS kernel_image
  FROM v_dump d
  LEFT JOIN v_module_catalog c ON c.module_name = d.module_name$dump$
ELSE $pmc$
  SELECT module_key(m.path) AS module_name,
         CAST(s.coreId AS INTEGER) AS context,
         CAST(COLUMNS(lambda col: col = getvariable('uprof_event_column')) AS BIGINT) AS samples,
         COALESCE(c.kernel_image,
                  is_kernel_image(module_key(m.path), NULL)) AS kernel_image
  FROM UnifiedSampleSeries s
  LEFT JOIN Module m ON m.id = s.moduleId
  LEFT JOIN v_module_catalog c ON c.module_name = module_key(m.path)$pmc$ END);

-- Process shares. samples is the per-row sample count summed over the group (one per dump row, the
-- event-column count per PMC row). When --top truncates, one <other processes> row carries the
-- rest so each emitted share still sums to 1.0; a share with a zero denominator is NULL, as an
-- all-zero tag column leaves tag_to_retire_share.
CREATE OR REPLACE TEMP VIEW v_process_rows AS
WITH agg AS (
  SELECT pid, process,
         CAST(sum(samples) AS BIGINT) AS samples,
         sum(weight) AS weight,
         sum(tag_cycles) AS tag_cycles
  FROM v_scoped
  GROUP BY pid, process
), ranked AS (
  SELECT *,
         CAST(sum(samples) OVER () AS BIGINT) AS total_samples,
         sum(weight) OVER () AS total_weight,
         sum(tag_cycles) OVER () AS total_tag_cycles,
         row_number() OVER (
           ORDER BY weight DESC NULLS LAST, samples DESC, pid, process) AS rn
  FROM agg
)
SELECT process, pid, samples, weight, tag_cycles,
       total_samples, total_weight, total_tag_cycles, 0 AS sort_key, rn
FROM ranked
WHERE getvariable('top_n') = 0 OR rn <= getvariable('top_n')
UNION ALL
SELECT '<other processes>', NULL, CAST(sum(samples) AS BIGINT), sum(weight), sum(tag_cycles),
       max(total_samples), max(total_weight), max(total_tag_cycles), 1, NULL
FROM ranked
WHERE getvariable('top_n') > 0 AND rn > getvariable('top_n')
HAVING count(*) > 0;

-- Function shares. Grouping is by Function.id, the unit uProf resolves; a source line is not
-- additive because one function's samples span many call sites. Samples with no Function.id are
-- the '<unresolved>' row. When --top truncates, one <other functions> row carries the rest.
CREATE OR REPLACE TEMP VIEW v_function_rows AS
WITH agg AS (
  SELECT w.function_id,
         CASE WHEN w.function_id IS NULL THEN '<unresolved>'
              ELSE COALESCE(NULLIF(max(f.name), ''), '<unnamed>') END AS function_name,
         CASE WHEN w.function_id IS NULL THEN '<unresolved>'
              ELSE COALESCE(NULLIF(max(w.module_name), ''), '<unresolved>') END AS module_name,
         CASE WHEN w.function_id IS NULL THEN 'unresolved' ELSE 'unique' END AS attribution,
         CAST(sum(w.samples) AS BIGINT) AS samples,
         sum(w.weight) AS weight,
         sum(w.tag_cycles) AS tag_cycles
  FROM v_scoped w
  LEFT JOIN Function f ON f.id = w.function_id
  GROUP BY w.function_id
), ranked AS (
  SELECT *,
         CAST(sum(samples) OVER () AS BIGINT) AS total_samples,
         sum(weight) OVER () AS total_weight,
         sum(tag_cycles) OVER () AS total_tag_cycles,
         row_number() OVER (
           ORDER BY weight DESC NULLS LAST, samples DESC, function_id NULLS LAST) AS rn
  FROM agg
)
SELECT function_name AS "function", module_name AS module, attribution, samples, weight, tag_cycles,
       total_samples, total_weight, total_tag_cycles, 0 AS sort_key, rn
FROM ranked
WHERE getvariable('top_n') = 0 OR rn <= getvariable('top_n')
UNION ALL
SELECT '<other functions>', NULL, NULL, CAST(sum(samples) AS BIGINT), sum(weight), sum(tag_cycles),
       max(total_samples), max(total_weight), max(total_tag_cycles), 1, NULL
FROM ranked
WHERE getvariable('top_n') > 0 AND rn > getvariable('top_n')
HAVING count(*) > 0;

-- Source-line shares from the combined llvm-symbolizer artifact. The file, line and function of a
-- group come from the innermost symbolizer frame, the executed source position inside an inlined
-- instance. The agreement check uses the containing function. A real source position is one row
-- per (file, line); function and module are annotations, so a position shared by several
-- instantiations shows '<multiple>' instead of splitting the share. '<no symbol>' groups per
-- module and '<no source line>' per module+function, so named functions without lines stay
-- visible; both are separate from real positions. The denominator is the whole scoped capture, so
-- unresolved addresses keep their sample weight instead of shrinking the shares.
--
-- Function identity is the innermost frame's short name: an inlined instance and the out-of-line
-- copy of the same function spell differently under --functions=linkage ('mix_inline' vs
-- 'mix_inline(int)') but share one short name. Out-of-line frames (innermost == containing) count
-- their distinct containing functions per (module, short name): one means a spelling variant and
-- keys on the short name, several mean a true overload set and key on the signature. An inlined
-- frame carries no signature and keeps the short key, so when only one out-of-line signature
-- exists it is displayed under that signature even if it belongs to a different same-named
-- overload. When all identities in a group agree, the displayed name is the deterministic max()
-- spelling, which prefers the signature form ('mix_inline(int)') over its short prefix.
--
-- Folded /OPT:ICF bodies: the public-symbol cache is ASOF-joined per module, so the merged body is
-- labelled 'shared: nameA | nameB'; a line whose addresses include one gets attribution 'shared'.
-- When --top truncates, one <other lines> row carries the rest.
CREATE OR REPLACE TEMP VIEW v_line_rows AS
WITH placed AS (
  SELECT w.module_name, w.samples, w.weight, w.tag_cycles,
         s.function AS sym_function, s.function_short AS sym_short,
         s.containing_function AS sym_containing,
         s.file AS sym_file, s.line AS sym_line,
         a.name_count, a.names
  FROM v_scoped w
  LEFT JOIN v_symbols s
    ON w.module_name = s.module_name AND w.rva = s.rva
  ASOF LEFT JOIN v_public_starts a
    ON w.module_name = a.module_name AND w.rva >= a.rva
), spellings AS (
  SELECT module_name AS spelling_module, sym_short AS spelling_short,
         count(DISTINCT sym_containing) FILTER (
           WHERE COALESCE(sym_function, '') <> ''
             AND COALESCE(sym_containing, '') <> ''
             AND sym_function = sym_containing) AS overload_count
  FROM placed
  WHERE COALESCE(sym_short, '') <> ''
  GROUP BY module_name, sym_short
), labelled AS (
  SELECT CASE WHEN COALESCE(sym_function, '') = '' THEN '<no symbol>'
              WHEN COALESCE(sym_file, '') = '' THEN '<no source line>'
              ELSE sym_file END AS file_name,
         CASE WHEN COALESCE(sym_function, '') = '' THEN 0 ELSE COALESCE(sym_line, 0) END AS line_number,
         CASE WHEN COALESCE(name_count, 1) > 1 THEN 'shared: ' || names
              WHEN COALESCE(sym_function, '') = '' THEN '<no symbol>'
              ELSE sym_function END AS function_label,
         CASE WHEN COALESCE(name_count, 1) > 1 THEN 'shared: ' || names
              WHEN COALESCE(sym_function, '') = '' THEN '<no symbol>'
              WHEN COALESCE(sp.overload_count, 0) > 1 THEN sym_function
              WHEN COALESCE(placed.sym_short, '') <> '' THEN placed.sym_short
              ELSE sym_function END AS function_key,
         COALESCE(NULLIF(module_name, ''), '<unknown module>') AS module_label,
         CASE WHEN COALESCE(name_count, 1) > 1 THEN 'shared'
              WHEN COALESCE(sym_function, '') = '' THEN 'unresolved'
              ELSE 'unique' END AS address_attribution,
         samples, weight, tag_cycles
  FROM placed
  LEFT JOIN spellings sp
    ON placed.module_name = sp.spelling_module AND placed.sym_short = sp.spelling_short
), agg AS (
  SELECT min(file_name) AS file_name,
         min(line_number) AS line_number,
         CASE WHEN min(file_name) = '<no symbol>' THEN '<no symbol>'
              WHEN count(DISTINCT function_key) = 1 THEN max(function_label)
              ELSE '<multiple>' END AS function_name,
         CASE WHEN count(DISTINCT module_label) = 1 THEN min(module_label)
              ELSE '<multiple>' END AS module_name,
         CASE WHEN min(file_name) = '<no symbol>' THEN 'unresolved'
              WHEN count(*) FILTER (WHERE address_attribution = 'shared') > 0 THEN 'shared'
              ELSE 'unique' END AS attribution,
         CAST(sum(samples) AS BIGINT) AS samples,
         sum(weight) AS weight,
         sum(tag_cycles) AS tag_cycles
  FROM labelled
  GROUP BY CASE WHEN file_name = '<no symbol>' THEN 'symbol:' || module_label
                WHEN file_name = '<no source line>' THEN 'source:' || module_label || chr(1) || function_key
                ELSE 'line:' || file_name || chr(1) || CAST(line_number AS VARCHAR) END
), ranked AS (
  SELECT *,
         CAST(sum(samples) OVER () AS BIGINT) AS total_samples,
         sum(weight) OVER () AS total_weight,
         sum(tag_cycles) OVER () AS total_tag_cycles,
         row_number() OVER (
           ORDER BY weight DESC NULLS LAST, samples DESC,
                    file_name, line_number, function_name, module_name) AS rn
  FROM agg
)
SELECT file_name AS "file", line_number AS line, function_name AS "function",
       module_name AS module, attribution, samples, weight, tag_cycles,
       total_samples, total_weight, total_tag_cycles, 0 AS sort_key, rn
FROM ranked
WHERE getvariable('top_n') = 0 OR rn <= getvariable('top_n')
UNION ALL
SELECT '<other lines>', NULL, NULL, NULL, NULL,
       CAST(sum(samples) AS BIGINT), sum(weight), sum(tag_cycles),
       max(total_samples), max(total_weight), max(total_tag_cycles), 1, NULL
FROM ranked
WHERE getvariable('top_n') > 0 AND rn > getvariable('top_n')
HAVING count(*) > 0;

-- Instruction shares, one row per module + RVA. Each address is annotated with the innermost
-- symbolizer frame (the executed source position, so an inlined instance names the inlinee);
-- addresses without a symbol keep their samples as '<no symbol>' with attribution 'unresolved'.
-- When --top truncates, one <other instructions> row carries the rest.
CREATE OR REPLACE TEMP VIEW v_instruction_rows AS
WITH placed AS (
  SELECT w.module_name, w.rva, w.samples, w.weight, w.tag_cycles,
         s.function AS sym_function, s.file AS sym_file, s.line AS sym_line,
         a.name_count, a.names
  FROM v_scoped w
  LEFT JOIN v_symbols s
    ON w.module_name = s.module_name AND w.rva = s.rva
  ASOF LEFT JOIN v_public_starts a
    ON w.module_name = a.module_name AND w.rva >= a.rva
), normalized AS (
  SELECT COALESCE(NULLIF(module_name, ''), '<unknown module>') AS module_name,
         rva,
         CASE WHEN COALESCE(name_count, 1) > 1 THEN 'shared: ' || names
              WHEN COALESCE(sym_function, '') = '' THEN '<no symbol>'
              ELSE sym_function END AS function_name,
         CASE WHEN COALESCE(sym_function, '') = '' THEN '<no symbol>'
              WHEN COALESCE(sym_file, '') = '' THEN '<no source line>'
              ELSE sym_file END AS file_name,
         CASE WHEN COALESCE(sym_function, '') = '' THEN 0 ELSE COALESCE(sym_line, 0) END AS line_number,
         CASE WHEN COALESCE(name_count, 1) > 1 THEN 'shared'
              WHEN COALESCE(sym_function, '') = '' THEN 'unresolved'
              ELSE 'unique' END AS attribution,
         samples, weight, tag_cycles
  FROM placed
), agg AS (
  SELECT module_name, rva, function_name, file_name, line_number, attribution,
         CAST(sum(samples) AS BIGINT) AS samples,
         sum(weight) AS weight,
         sum(tag_cycles) AS tag_cycles
  FROM normalized
  GROUP BY module_name, rva, function_name, file_name, line_number, attribution
), ranked AS (
  SELECT *,
         CAST(sum(samples) OVER () AS BIGINT) AS total_samples,
         sum(weight) OVER () AS total_weight,
         sum(tag_cycles) OVER () AS total_tag_cycles,
         row_number() OVER (
           ORDER BY weight DESC NULLS LAST, samples DESC,
                    module_name, rva NULLS LAST, function_name) AS rn
  FROM agg
)
SELECT module_name AS module,
       CASE WHEN rva IS NULL THEN NULL ELSE printf('0x%X', rva) END AS rva,
       function_name AS "function", file_name AS "file", line_number AS line,
       attribution, samples, weight, tag_cycles,
       total_samples, total_weight, total_tag_cycles, 0 AS sort_key, rn
FROM ranked
WHERE getvariable('top_n') = 0 OR rn <= getvariable('top_n')
UNION ALL
SELECT '<other instructions>', NULL, NULL, NULL, NULL, NULL,
       CAST(sum(samples) AS BIGINT), sum(weight), sum(tag_cycles),
       max(total_samples), max(total_weight), max(total_tag_cycles), 1, NULL
FROM ranked
WHERE getvariable('top_n') > 0 AND rn > getvariable('top_n')
HAVING count(*) > 0;
