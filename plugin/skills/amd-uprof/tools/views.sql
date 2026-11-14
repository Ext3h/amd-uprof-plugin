-- Single schema for the profile command. uprof.py binds the uprof_* inputs as prepared
-- parameters, then loads this file once per run; every profile file is a projection over these
-- views. A hand run sets the same inputs with SET VARIABLE.
--
-- Bound inputs:
--   uprof_image         ILIKE pattern on the process image's file name or full path; '%'
--                       selects every process
--   uprof_pid           one PID as text; empty selects every process
--   uprof_top           row cap; 20 by default, 0 means no cap
--   uprof_time_start    window start in ns from capture start (inclusive)
--   uprof_time_end      window end in ns (exclusive)
--   uprof_ibs_dump      absolute IBS ASCII dump path; empty for a PMC capture
--   uprof_ibs_parquet   cached Parquet copy of the dump's SAMPLE_COLUMNS; empty to read the CSV
--   uprof_event_column  PMC sample column in UnifiedSampleSeries; empty for a dump capture
--   uprof_session_meta  session.uprof path when the file exists, else empty
--   uprof_tag_available 'true' only for an IBS OP dump whose IbsTagToRetCtr exists and has a
--                       non-null value; Python derives it from the sampler kind and v_dump_sanity
--   uprof_symbols       combined symbols.parquet path for line and instruction
--   uprof_aliases       combined symbols.publics.csv path for fold labelling
--   uprof_pdbs          combined symbols.pdbs.csv path: the PDB symbolization chose per module
--   uprof_sample_interval_s  requested cycle interval in seconds; empty when the interval is a
--                       work/fetch count or the core frequency is unavailable, so cpu_seconds is
--                       omitted
--   uprof_functions     function-range CSV derived from the modules' PE images; empty when no
--                       range was derived, so function rows fall back to uProf Function.id
--   uprof_range_attempted  chr(2)-joined modules for which derivation ran (fully named modules
--                       are skipped); lets function_reason say "skipped" only when it was
--   uprof_ambiguous_modules  chr(2)-joined module names the capture maps to more than one image
--                       version; dump samples of these get no Function.id
--   uprof_primary_measure  the column that orders function/region rows and --top: sample_share,
--                       time_share or samples
--   uprof_detail_views  'true' (the default when unset) binds the full line and instruction
--                       views; Python binds 'false' for scopes that never render them
--
-- The SET VARIABLE statements below are the mode flags, guards and typed top/time values; no view
-- file carries its own setup. The guards run before v_dump, so a bad capture fails once, with a
-- message naming what is missing.

-- Macros are TEMP because cpu.db is opened read-only.
CREATE OR REPLACE TEMP MACRO module_key(p) AS replace(lower(p), chr(92), '/');

-- read_csv, read_json and glob take a pattern: '[', '*' and '?' in a capture path (capture[1])
-- would match a sibling (capture1). A one-character class matches each of them literally.
CREATE OR REPLACE TEMP MACRO literal_path(p) AS regexp_replace(p, '([\[*?])', '[\1]', 'g');

-- --image selects a process when its pattern matches the image's file name or its full path:
-- uProf stores Process.name as the full image path, and callers name the file.
CREATE OR REPLACE TEMP MACRO image_matches(name, pattern) AS
  COALESCE(name, '') ILIKE pattern
  OR regexp_replace(COALESCE(name, ''), '^.*[\\/]', '') ILIKE pattern;
-- A dump row carries the module name, not Module.id, so a name with several image versions
-- cannot be tied to one version's Function rows.
CREATE OR REPLACE TEMP MACRO module_ambiguous(name) AS COALESCE(list_contains(
  string_split(NULLIF(getvariable('uprof_ambiguous_modules'), ''), chr(2)), name), false);
-- A share is a fraction of the scoped total (0.0-1.0); the unit is stated once in the output.
CREATE OR REPLACE TEMP MACRO share(part, whole) AS part / NULLIF(whole, 0);
-- The plain identifier a function name carries: before the first '@' of a decorated MSVC public
-- ('?f@@YAHH@Z', '?m@K@ns@@...', '??$f@H@@...' for a template), else the last identifier of a
-- demangled spelling once template arguments and the parameter list are removed
-- ('int __cdecl ns::f<float>(int)' -> 'f'). Empty when there is none (operators, constructors).
CREATE OR REPLACE TEMP MACRO name_identifier(n) AS CASE
  WHEN n LIKE '?%' THEN regexp_extract(n, '^\?(\?\$)?([A-Za-z_][A-Za-z0-9_]*)@', 2)
  ELSE regexp_extract(
         regexp_replace(
           regexp_replace(regexp_replace(regexp_replace(n, '<[^<>]*>', '', 'g'),
                                         '<[^<>]*>', '', 'g'), '<[^<>]*>', '', 'g'),
           '\(.*$', ''),
         '([A-Za-z_][A-Za-z0-9_]*)$', 1) END;
-- A folded public start (several names at one address) labels an address 'shared' unless the
-- symbolizer frame there names a function outside the fold: a static function after the folded
-- body matches the start too, because the start is joined ASOF. The frame is outside only when
-- every folded name and the frame have an identifier and none agrees; a truncated name list
-- ('... (+N more)') cannot show that.
CREATE OR REPLACE TEMP MACRO fold_holds(name_count, names, frame) AS COALESCE(
  COALESCE(name_count, 1) > 1
  AND (COALESCE(frame, '') = ''
       OR names LIKE '%... (+% more)'
       OR name_identifier(frame) = ''
       OR len(list_filter(string_split(names, ' | '),
                          lambda n: name_identifier(n) IN ('', name_identifier(frame)))) > 0),
  false);
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
-- dump exists, tag_to_retire_share unless this sampler's tag column supplied values, and
-- cpu_seconds unless the requested interval is a time (a work/fetch interval is not time).
-- Parent totals and the parent-relative shares exist only for a function-scoped instruction or
-- line view; the parent totals are internal and the parent shares are emitted only when a
-- --function filter selected the parent.
CREATE OR REPLACE TEMP MACRO projected(c) AS
    c NOT IN ('weight', 'tag_cycles', 'total_samples', 'total_weight', 'total_tag_cycles',
              'sort_key', 'rn', 'parent_samples', 'parent_weight', 'parent_tag_cycles')
    AND (COALESCE(getvariable('uprof_function'), '') <> ''
         OR c NOT IN ('parent_sample_share', 'parent_time_share',
                      'parent_tag_to_retire_share'))
    AND (getvariable('has_dump') OR c NOT IN ('time_share', 'parent_time_share'))
    AND (getvariable('uprof_tag_available') = 'true'
         OR c NOT IN ('tag_to_retire_share', 'parent_tag_to_retire_share'))
    AND (getvariable('uprof_sample_interval_s') <> '' OR c <> 'cpu_seconds');

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

-- The PMC branch reads processId and coreId; a dump capture reads only PID from the ASCII dump,
-- so an export without those columns still works with a dump.
SET VARIABLE schema_check_pid = CASE WHEN NOT getvariable('has_dump') THEN (
  SELECT CASE WHEN count(*) > 0
              THEN error('uProf export incomplete: missing ' ||
                         string_agg('column UnifiedSampleSeries.' || column_name, ', ' ORDER BY column_name) ||
                         '; the capture probably comes from a uProf version with a different export')
              ELSE 1 END
  FROM (VALUES ('processId'), ('coreId')) AS pmc_columns(column_name)
  WHERE NOT EXISTS (
    SELECT 1 FROM duckdb_columns() AS c
    WHERE lower(c.schema_name) = 'main' AND lower(c.table_name) = 'unifiedsampleseries'
      AND lower(c.column_name) = lower(pmc_columns.column_name))
) ELSE 1 END;

-- END OF THE EXPORT CONTRACT GUARD: the schema command runs this file up to this line.

SET VARIABLE dump_guard = CASE
  WHEN NOT getvariable('has_dump') THEN 1
  WHEN NOT (SELECT count(*) > 0 FROM glob(literal_path(getvariable('uprof_ibs_dump'))))
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
                 FROM (DESCRIBE SELECT * FROM read_csv(literal_path(getvariable('uprof_ibs_dump')),
                                                      header = true, all_varchar = true)) AS d
                 WHERE d.column_name = required.name)$cols$
    ELSE 'SELECT NULL::VARCHAR AS missing_name WHERE false' END)
);

-- Only IBS OP dumps carry a tag-to-retire counter; when the column is absent
-- tag_to_retire_share is omitted rather than filled with zeroes.
SET VARIABLE has_tag = (
  SELECT has_tag FROM query(CASE WHEN getvariable('has_dump')
    THEN $tag$SELECT count(*) > 0 AS has_tag
              FROM (DESCRIBE SELECT * FROM read_csv(literal_path(getvariable('uprof_ibs_dump')),
                                                    header = true, all_varchar = true))
              WHERE column_name = 'IbsTagToRetCtr'$tag$
    ELSE 'SELECT false AS has_tag' END)
);

-- Older exports carry no ModuleInstance table; the kernel check then has only module paths.
SET VARIABLE has_module_instances = (
  SELECT count(*) > 0 FROM duckdb_tables()
  WHERE lower(schema_name) = 'main' AND lower(table_name) = 'moduleinstance');

-- Older exports carry no Module.isSystemModule; module rows then leave the classification NULL.
SET VARIABLE has_system_module = (
  SELECT count(*) > 0 FROM duckdb_columns()
  WHERE lower(schema_name) = 'main' AND lower(table_name) = 'module'
    AND lower(column_name) = 'issystemmodule');

SET VARIABLE top_n = greatest(CAST(COALESCE(getvariable('uprof_top'), 20) AS BIGINT), 0);
SET VARIABLE time_start = CAST(COALESCE(NULLIF(getvariable('uprof_time_start'), ''), '0') AS BIGINT);
SET VARIABLE time_end = CAST(COALESCE(NULLIF(getvariable('uprof_time_end'), ''), '9223372036854775807') AS BIGINT);

-- The dump rows time_share reads. A run may hand over a Parquet copy of exactly these columns
-- (uprof_dump.samples_parquet); otherwise the ASCII dump is read as text. The copy holds the same
-- cell text, so the casts below behave identically either way. The variable is optional, so a
-- direct load of this file reads the dump. A PMC capture has no dump to bind.
CREATE OR REPLACE TEMP VIEW v_dump_source AS
SELECT * FROM query(CASE
  WHEN NOT getvariable('has_dump') THEN $empty$
    SELECT CAST(NULL AS VARCHAR) AS "DeltaTick",
           CAST(NULL AS VARCHAR) AS "CPU_Number",
           CAST(NULL AS VARCHAR) AS "TID",
           CAST(NULL AS VARCHAR) AS "PID",
           CAST(NULL AS VARCHAR) AS "Offset",
           CAST(NULL AS VARCHAR) AS "ModuleName",
           CAST(NULL AS VARCHAR) AS "IbsTagToRetCtr"
    WHERE false$empty$
  WHEN COALESCE(getvariable('uprof_ibs_parquet'), '') <> ''
    THEN $parquet$SELECT * FROM read_parquet(getvariable('uprof_ibs_parquet'))$parquet$
  -- The sample only infers types and every column is read as text, so sample_size = 1 reads the
  -- header and stops instead of scanning data rows at bind time.
  ELSE $csv$SELECT * FROM read_csv(literal_path(getvariable('uprof_ibs_dump')),
                                   header = true, all_varchar = true,
                                   sample_size = 1)$csv$
END);

-- DeltaTick is a cumulative, start-relative nanosecond timestamp; all columns are read as text so
-- uProf's empty or non-numeric cells cannot force a column type. tid identifies the thread in an
-- attached capture: the achieved-cadence check uses it as the context there and cpu for a
-- system-wide capture. tag_cycles is the tag-to-retire cycle count of one sampled op (AMD: tagged
-- at dispatch, retired later; queue/stalls included), clipped at 65535, the 16-bit field's
-- maximum. Only an unsigned decimal integer cell counts; NULL, negative, fractional, exponent and
-- nan/inf spellings stay NULL and are excluded from the tag_to_retire_share denominator rather
-- than being charged the maximum. The empty branch gives a PMC capture a typed, empty relation so
-- the downstream views bind without touching a dump. Materialized as a table: the ASCII dump is
-- parsed once per run, and every downstream view scans these typed rows instead of re-reading it.
CREATE OR REPLACE TEMP TABLE v_dump AS
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
    FROM v_dump_source$tagged$
  ELSE $untagged$
    SELECT CAST(DeltaTick AS BIGINT) AS delta_tick,
           CAST(CPU_Number AS INTEGER) AS cpu,
           CAST(TID AS INTEGER) AS tid,
           CAST(PID AS INTEGER) AS pid,
           CAST("Offset" AS BIGINT) AS rva,
           module_key(ModuleName) AS module_name,
           CAST(NULL AS DOUBLE) AS tag_cycles
    FROM v_dump_source$untagged$
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
               FROM read_json(literal_path(getvariable('uprof_session_meta')),
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
    (CASE WHEN getvariable('attached') THEN 'WARN, attached: ' ELSE 'WARN, system-wide: ' END) ||
    'dump ' || getvariable('uprof_ibs_dump') || ': ' || CAST(dump_samples AS VARCHAR) ||
    ' samples, ' || CAST(excluded_first AS VARCHAR) ||
    ' first-per-' || context_label ||
    ' samples excluded; session.uprof has no usable duration; total weight was not checked against the capture span'
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
    printf('OK, attached: dump %s: %d samples, %d first-per-%s samples excluded; %d process(es), %d TID(s), %d active %s (%d/%d samples in the busiest), %d CPU(s) visited; time_share partitions gaps by (PID, TID), so a migrating thread is counted once; a thread''s gaps include off-CPU time, so for a cycle-controlled capture sample_share (fixed cycles per sample) is the CPU-cycle share and the better ranking',
           getvariable('uprof_ibs_dump'), dump_samples, excluded_first, context_label,
           dump_pids, dump_threads, active_contexts,
           CASE WHEN active_contexts = 1 THEN 'TID' ELSE 'TIDs' END,
           busiest_samples, dump_samples, dump_cpus)
  WHEN (end_ns - max_last_ns) > (expected_ns / greatest(max_context_samples, 1)) THEN
    printf('WARN, system-wide: dump %s: %d samples, %d first-per-CPU samples excluded; the capture window ends %.3f s before the recorded span, longer than one sampling cadence, so late time is uncovered; boundary gaps %.3f s (leading %.3f s, trailing %.3f s, worst cpu %s)',
           getvariable('uprof_ibs_dump'), dump_samples, excluded_first,
           (end_ns - max_last_ns) / 1e9,
           (expected_core_ns - total_weight) / 1e9, leading_ns / 1e9, trailing_ns / 1e9,
           CAST(worst_context AS VARCHAR))
  ELSE
    printf('OK, system-wide: dump %s: %d samples, %d first-per-CPU samples excluded; %d CPU(s); the last sample lies within one sampling cadence of the recorded window end',
           getvariable('uprof_ibs_dump'), dump_samples, excluded_first, dump_cpus)
END AS message,
CAST(tag_values AS BIGINT) AS tag_values
FROM measured;

-- Artifact sources: file-backed by default, so views.sql is usable on its own (a hand-run
-- query with the artifact variables set); a cache hit replaces these TEMP views with table
-- reads. The empty branch keeps the columns typed when the variable is empty.
CREATE OR REPLACE TEMP VIEW uprof_symbols_source AS
SELECT * FROM query(CASE WHEN getvariable('uprof_symbols') <> ''
  THEN $s1$SELECT * FROM read_parquet(getvariable('uprof_symbols'))$s1$
  ELSE $s1$SELECT NULL::VARCHAR AS module_name, NULL::VARCHAR AS address,
                   NULL::VARCHAR AS function, NULL::VARCHAR AS function_short,
                   NULL::VARCHAR AS containing_function, NULL::VARCHAR AS file,
                   NULL::INTEGER AS line WHERE false$s1$ END);

CREATE OR REPLACE TEMP VIEW uprof_aliases_source AS
SELECT * FROM query(CASE WHEN getvariable('uprof_aliases') <> ''
  THEN $s2$SELECT * FROM read_csv(getvariable('uprof_aliases'), header = true,
                                  all_varchar = true)$s2$
  ELSE $s2$SELECT NULL::VARCHAR AS module_name, NULL::VARCHAR AS rva,
                   NULL::INTEGER AS name_count, NULL::VARCHAR AS names WHERE false$s2$ END);

CREATE OR REPLACE TEMP VIEW uprof_functions_source AS
SELECT * FROM query(CASE WHEN getvariable('uprof_functions') <> ''
  THEN $s3$SELECT * FROM read_csv(getvariable('uprof_functions'), header = true,
                                  all_varchar = true)$s3$
  ELSE $s3$SELECT NULL::VARCHAR AS module_name, NULL::VARCHAR AS function_start,
                   NULL::VARCHAR AS function_end, NULL::VARCHAR AS function_source,
                   NULL::VARCHAR AS function_name WHERE false$s3$ END);

CREATE OR REPLACE TEMP VIEW uprof_pdbs_source AS
SELECT * FROM query(CASE WHEN getvariable('uprof_pdbs') <> ''
  THEN $s4$SELECT * FROM read_csv(getvariable('uprof_pdbs'), header = true,
                                  all_varchar = true)$s4$
  ELSE $s4$SELECT NULL::VARCHAR AS module_name, NULL::VARCHAR AS pdb WHERE false$s4$ END);

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
            FROM uprof_symbols_source$sym$
  ELSE $sym$SELECT NULL::VARCHAR AS module_name, NULL::VARCHAR AS address,
                   NULL::VARCHAR AS function, NULL::VARCHAR AS function_short,
                   NULL::VARCHAR AS containing_function, NULL::VARCHAR AS file,
                   NULL::INTEGER AS line WHERE false$sym$ END);

-- Public-symbol cache: module_name, rva, name_count, names. Empty when absent, so every address
-- stays 'unique' and no shared label is emitted.
CREATE OR REPLACE TEMP VIEW v_public_starts AS
SELECT * FROM query(CASE WHEN getvariable('uprof_aliases') <> ''
  THEN $pub$SELECT module_name, CAST(rva AS BIGINT) AS rva, CAST(name_count AS INTEGER) AS name_count, names
            FROM uprof_aliases_source$pub$
  ELSE $pub$SELECT NULL::VARCHAR AS module_name, NULL::BIGINT AS rva, NULL::INTEGER AS name_count,
                   NULL::VARCHAR AS names WHERE false$pub$ END);

-- Function ranges derived from the PE images by uprof_functions.py: one row per [start, end) in
-- a module. source is export, pdb_public, pdata or none; function_name is empty when the boundary
-- carries no name, and the function view makes that name visibly synthetic. Empty when no range
-- was derived, so function rows group by uProf Function.id instead.
CREATE OR REPLACE TEMP VIEW v_function_ranges AS
SELECT * FROM query(CASE WHEN getvariable('uprof_functions') <> ''
  THEN $ranges$SELECT module_name,
                      CAST(function_start AS BIGINT) AS function_start,
                      CAST(function_end AS BIGINT) AS function_end,
                      function_source,
                      NULLIF(function_name, '') AS function_name
               FROM uprof_functions_source$ranges$
  ELSE $ranges$SELECT NULL::VARCHAR AS module_name, NULL::BIGINT AS function_start,
                       NULL::BIGINT AS function_end, NULL::VARCHAR AS function_source,
                       NULL::VARCHAR AS function_name WHERE false$ranges$ END);

-- PDB choice per module, recorded by symbolization: module_name, pdb. Empty when absent, so the
-- instruction context symbolizes without a PDB instead of discovering one a second time.
CREATE OR REPLACE TEMP VIEW v_pdbs AS
SELECT module_name, NULLIF(pdb, '') AS pdb
FROM query(CASE WHEN getvariable('uprof_pdbs') <> ''
  THEN $pdbs$SELECT module_name, pdb
              FROM uprof_pdbs_source$pdbs$
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
  WHERE s.functionId IS NOT NULL AND NOT module_ambiguous(module_key(m.path))
  GROUP BY module_name, rva
) rf ON w.module_name = rf.module_name AND w.rva = rf.rva
WHERE (getvariable('uprof_pid') = '' OR w.pid = CAST(getvariable('uprof_pid') AS INTEGER))
  AND image_matches(p.name, getvariable('uprof_image'))$dump$
ELSE $pmc$
SELECT CAST(NULL AS BIGINT) AS delta_tick,
       CAST(NULL AS INTEGER) AS cpu,
       CAST(NULL AS INTEGER) AS tid,
       CAST(s.processId AS INTEGER) AS pid,
       CAST(s.moduleRVA AS BIGINT) AS rva,
       module_key(m.path) AS module_name,
       CAST(NULL AS DOUBLE) AS weight,
       CAST(COLUMNS(lambda c: lower(c) = lower(getvariable('uprof_event_column'))) AS BIGINT) AS samples,
       CAST(NULL AS DOUBLE) AS tag_cycles,
       COALESCE(p.name, '<unknown process>') AS process,
       s.functionId AS function_id
FROM UnifiedSampleSeries s
LEFT JOIN Process p ON p.id = s.processId
LEFT JOIN Module m ON m.id = s.moduleId
WHERE (getvariable('uprof_pid') = '' OR s.processId = CAST(getvariable('uprof_pid') AS INTEGER))
  AND image_matches(p.name, getvariable('uprof_image'))$pmc$ END);

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

-- Capture denominators for the row views. They come from the whole scoped capture, so a
-- function-scoped instruction or line view keeps capture-level sample_share/time_share/
-- tag_to_retire_share instead of silently shrinking the denominator to the filtered rows.
CREATE OR REPLACE TEMP VIEW v_scope_measures AS
SELECT CAST(COALESCE(sum(samples), 0) AS BIGINT) AS total_samples,
       sum(weight) AS total_weight,
       sum(tag_cycles) AS total_tag_cycles
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

-- Module.isSystemModule per module, keyed by the full normalized path and by basename. A dump's
-- ModuleName may carry a full path while the catalog path differs only in directory, so the
-- basename is the fallback key; an ambiguous basename (disagreeing full paths) returns NULL.
CREATE OR REPLACE TEMP VIEW v_module_flags_exact AS
SELECT * FROM query(CASE WHEN getvariable('has_system_module') THEN $flags$
  SELECT module_key(m.path) AS module_name,
         bool_or(CAST(m.isSystemModule AS BOOLEAN)) AS "isSystemModule"
  FROM Module m
  WHERE COALESCE(m.path, '') <> ''
  GROUP BY module_key(m.path)$flags$
ELSE $absent$
  SELECT CAST(NULL AS VARCHAR) AS module_name, CAST(NULL AS BOOLEAN) AS "isSystemModule"
  WHERE false$absent$ END);

CREATE OR REPLACE TEMP VIEW v_module_flags_basename AS
SELECT * FROM query(CASE WHEN getvariable('has_system_module') THEN $flags$
  SELECT regexp_extract(module_key(m.path), '([^/]+)$', 1) AS basename,
         CASE WHEN bool_and(CAST(m.isSystemModule AS BOOLEAN)) = bool_or(CAST(m.isSystemModule AS BOOLEAN))
              THEN bool_or(CAST(m.isSystemModule AS BOOLEAN))
              ELSE NULL END AS "isSystemModule"
  FROM Module m
  WHERE COALESCE(m.path, '') <> ''
  GROUP BY regexp_extract(module_key(m.path), '([^/]+)$', 1)$flags$
ELSE $absent$
  SELECT CAST(NULL AS VARCHAR) AS basename, CAST(NULL AS BOOLEAN) AS "isSystemModule"
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
         CAST(COLUMNS(lambda col: lower(col) = lower(getvariable('uprof_event_column'))) AS BIGINT) AS samples,
         COALESCE(c.kernel_image,
                  is_kernel_image(module_key(m.path), NULL)) AS kernel_image
  FROM UnifiedSampleSeries s
  LEFT JOIN Module m ON m.id = s.moduleId
  LEFT JOIN v_module_catalog c ON c.module_name = module_key(m.path)$pmc$ END);

-- Process shares. samples is the per-row sample count summed over the group (one per dump row, the
-- event-column count per PMC row). Rows and --top follow uprof_primary_measure: gap weight for a
-- time-based capture, otherwise the sample count (the same order as sample_share). When --top
-- truncates, one <other processes> row carries the rest so each emitted share still sums to 1.0;
-- a share with a zero denominator is NULL, as an all-zero tag column leaves tag_to_retire_share.
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
           ORDER BY CASE getvariable('uprof_primary_measure')
                      WHEN 'time_share' THEN weight END DESC NULLS LAST,
                    samples DESC, pid, process) AS rn
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

-- Module shares. The key is the normalized module path the dump or UnifiedSampleSeries carries,
-- the same key line and instruction rows use. isSystemModule comes from Module and stays NULL
-- when the export predates the column or the module is not in the catalog; the remainder row has
-- no single classification. Rows and --top follow uprof_primary_measure: gap weight for a
-- time-based capture, otherwise the sample count (the same order as sample_share). When --top
-- truncates, one <other modules> row carries the rest. The module totals and the per-module RVA
-- samples share one materialized scan of v_scoped; a second reference would evaluate it again.
CREATE OR REPLACE TEMP VIEW v_module_rows AS
WITH scanned AS MATERIALIZED (
  SELECT w.module_name, w.rva, w.samples, w.weight, w.tag_cycles
  FROM v_scoped w
), agg AS (
  SELECT COALESCE(NULLIF(module_name, ''), '<unknown module>') AS module,
         CAST(sum(samples) AS BIGINT) AS samples,
         sum(weight) AS weight,
         sum(tag_cycles) AS tag_cycles
  FROM scanned
  GROUP BY 1
), ranked AS (
  SELECT *,
         CAST(sum(samples) OVER () AS BIGINT) AS total_samples,
         sum(weight) OVER () AS total_weight,
         sum(tag_cycles) OVER () AS total_tag_cycles,
         row_number() OVER (
           ORDER BY CASE getvariable('uprof_primary_measure')
                      WHEN 'time_share' THEN weight END DESC NULLS LAST,
                    samples DESC, module) AS rn
  FROM agg
), rva_samples AS (
  SELECT COALESCE(NULLIF(module_name, ''), '<unknown module>') AS module,
         rva, CAST(sum(samples) AS BIGINT) AS samples
  FROM scanned
  GROUP BY 1, 2
), hot_ranked AS (
  SELECT module, rva AS hot_rva, samples AS hot_samples,
         row_number() OVER (PARTITION BY module ORDER BY samples DESC, rva) AS hot_rank
  FROM rva_samples
), hot AS (
  SELECT module, hot_rva, hot_samples FROM hot_ranked WHERE hot_rank = 1
), flagged AS (
  SELECT r.*,
         COALESCE(fe."isSystemModule", fb."isSystemModule") AS "isSystemModule",
         h.hot_rva, h.hot_samples
  FROM ranked r
  LEFT JOIN v_module_flags_exact fe ON fe.module_name = r.module
  LEFT JOIN v_module_flags_basename fb ON fb.basename = regexp_extract(r.module, '([^/]+)$', 1)
  LEFT JOIN hot h ON h.module = r.module
)
SELECT module, samples, weight, tag_cycles, "isSystemModule", hot_rva, hot_samples,
       total_samples, total_weight, total_tag_cycles, 0 AS sort_key, rn
FROM flagged
WHERE getvariable('top_n') = 0 OR rn <= getvariable('top_n')
UNION ALL
SELECT '<other modules>', CAST(sum(samples) AS BIGINT), sum(weight), sum(tag_cycles), NULL,
       NULL, NULL, max(total_samples), max(total_weight), max(total_tag_cycles), 1, NULL
FROM flagged
WHERE getvariable('top_n') > 0 AND rn > getvariable('top_n')
HAVING count(*) > 0;

-- Every scoped sample with the function row it belongs to: the derived range that covers its RVA,
-- else its uProf Function.id, else its module. The function rows group by group_key, and a
-- function-scoped instruction or line view selects its samples by the parent row's group_key, so
-- a parent's children are exactly the samples its measurement holds.
CREATE OR REPLACE TEMP VIEW v_scoped_keyed AS
SELECT w.*,
       COALESCE(NULLIF(w.module_name, ''), '<unknown module>') AS module_label,
       r.function_start, r.function_end, r.function_source,
       r.function_name AS range_name,
       CASE WHEN r.function_start IS NOT NULL
            THEN 'r:' || COALESCE(NULLIF(w.module_name, ''), '<unknown module>') || chr(2)
                 || CAST(r.function_start AS VARCHAR)
            WHEN w.function_id IS NOT NULL THEN 'f:' || CAST(w.function_id AS VARCHAR)
            ELSE 'u:' || COALESCE(NULLIF(w.module_name, ''), '<unknown module>') END AS group_key,
       CASE WHEN r.function_start IS NOT NULL THEN 'range'
            WHEN w.function_id IS NOT NULL THEN 'uprof_db'
            ELSE 'none' END AS range_kind
FROM v_scoped w
LEFT JOIN v_function_ranges r
  ON w.module_name = r.module_name
 AND w.rva >= r.function_start AND w.rva < r.function_end;

-- Function shares. A sample is grouped by the derived function range that covers its RVA (from
-- v_function_ranges) when one exists; the range name, else the uProf Function.name, else a visibly
-- synthetic pdata_0x<start>/sub_0x<start> names the row, and a name names it only when the
-- range's samples agree on it. Samples outside every derived range fall
-- back to Function.id, so a module whose binary is unavailable keeps its uProf naming. hot_rva is
-- the highest-sample RVA in the row; attribution 'derived' marks a synthetic binary name, not a
-- source symbol, and 'shared' a range whose start carries several public names. Rows and --top follow uprof_primary_measure: gap weight for a time-based capture,
-- otherwise the sample count (the same order as sample_share). This view is uncapped, so the
-- parent lookup of a function-scoped instruction or line view is not hidden by --top; the public
-- v_function_rows adds the cap and the <other functions> remainder.
CREATE OR REPLACE TEMP VIEW v_function_measured AS
WITH keyed AS (
  SELECT w.module_label AS module_name, w.rva, w.function_id, w.samples, w.weight,
         w.tag_cycles, COALESCE(NULLIF(f.name, ''), '') AS db_name,
         w.function_start, w.function_end, w.function_source, w.range_name,
         w.group_key, w.range_kind
  FROM v_scoped_keyed w
  LEFT JOIN Function f ON f.id = w.function_id
), rva_samples AS (
  SELECT group_key, rva, CAST(sum(samples) AS BIGINT) AS samples
  FROM keyed
  GROUP BY group_key, rva
), hot_ranked AS (
  SELECT group_key, rva AS hot_rva, samples AS hot_samples,
         row_number() OVER (PARTITION BY group_key ORDER BY samples DESC, rva) AS hot_rank
  FROM rva_samples
), hot AS (
  SELECT group_key, hot_rva, hot_samples FROM hot_ranked WHERE hot_rank = 1
), agg AS (
  SELECT group_key,
         min(module_name) AS module_name,
         min(range_kind) AS range_kind,
         max(function_id) AS function_id,
         max(function_start) AS function_start,
         max(function_end) AS function_end,
         max(function_source) AS function_source,
         -- A range absorbs code without its own boundary, so a name holds for the row only
         -- when its samples agree: the range name while they carry at most one uProf name,
         -- the uProf name when every sample carries the same one. Otherwise the row keeps
         -- the synthetic range name and is derived.
         CASE WHEN count(DISTINCT NULLIF(db_name, '')) <= 1 THEN max(range_name) END
           AS range_name,
         CASE WHEN count(DISTINCT NULLIF(db_name, '')) = 1 AND bool_and(db_name <> '')
              THEN max(db_name) ELSE '' END AS db_name,
         CAST(sum(samples) AS BIGINT) AS samples,
         sum(weight) AS weight,
         sum(tag_cycles) AS tag_cycles
  FROM keyed
  GROUP BY group_key
), labelled AS (
  SELECT a.*, h.hot_rva, CAST(h.hot_samples AS BIGINT) AS hot_samples,
         CASE
           WHEN a.range_kind = 'range' AND a.range_name IS NOT NULL THEN a.range_name
           WHEN a.range_kind = 'range' AND a.db_name <> '' THEN a.db_name
           WHEN a.range_kind = 'range' AND a.function_source = 'pdata'
             THEN printf('pdata_0x%X', a.function_start)
           WHEN a.range_kind = 'range' THEN printf('sub_0x%X', a.function_start)
           WHEN a.range_kind = 'uprof_db' AND a.db_name <> '' THEN a.db_name
           ELSE '<unresolved>'
         END AS function_name,
         CASE WHEN a.range_kind = 'range' AND a.range_name IS NULL AND a.db_name = ''
              THEN true ELSE false END AS function_derived,
         CASE WHEN a.range_kind = 'range' AND a.range_name IS NULL AND a.db_name = ''
                   THEN 'derived'
              WHEN a.range_kind = 'range' AND COALESCE(p.name_count, 1) > 1 THEN 'shared'
              WHEN a.range_kind = 'range' THEN 'unique'
              WHEN a.range_kind = 'uprof_db' AND a.db_name <> '' THEN 'unique'
              ELSE 'unresolved' END AS attribution,
         CASE WHEN a.range_kind = 'range' THEN NULL
              WHEN a.range_kind = 'uprof_db' AND NOT COALESCE(list_contains(
                     string_split(NULLIF(getvariable('uprof_range_attempted'), ''), chr(2)),
                     a.module_name), false)
                THEN 'fully named from the uProf DB; function-range derivation skipped'
              WHEN a.range_kind = 'uprof_db'
                THEN 'no boundary source (export, PDB public or .pdata) covers these samples: the module file is unavailable, not a PE image, or the RVA is outside a code section; the row groups by uProf Function.id, and --by region is the page view'
              WHEN getvariable('has_dump') AND module_ambiguous(a.module_name)
                THEN 'the capture maps this module name to more than one image version and IBS dump rows carry the name, not Module.id, so no version''s functions are attributed; --by module is the module view'
              ELSE 'no derived range and no uProf Function.id for these samples; --by region is the page view' END
           AS function_reason
  FROM agg a
  LEFT JOIN hot h USING (group_key)
  -- A range that starts at a folded public start (several names at one address) is shared, as
  -- the line, instruction and region views label that address; its name is the first of them.
  LEFT JOIN v_public_starts p
    ON a.range_kind = 'range' AND p.module_name = a.module_name AND p.rva = a.function_start
), measured AS (
  SELECT labelled.*,
         CAST(sum(samples) OVER () AS BIGINT) AS total_samples,
         sum(weight) OVER () AS total_weight,
         sum(tag_cycles) OVER () AS total_tag_cycles,
         row_number() OVER (
           ORDER BY CASE getvariable('uprof_primary_measure')
                      WHEN 'time_share' THEN weight END DESC NULLS LAST,
                    samples DESC, function_name, module_name) AS rn
  FROM labelled
)
SELECT * FROM measured;

-- The public function rows: the aggregated rows with --top applied and one remainder row.
CREATE OR REPLACE TEMP VIEW v_function_rows AS
SELECT m.function_name AS "function", m.module_name AS module,
       CASE WHEN m.function_start IS NULL THEN NULL
            ELSE printf('0x%X', m.function_start) END AS function_start,
       CASE WHEN m.function_end IS NULL THEN NULL
            ELSE printf('0x%X', m.function_end) END AS function_end,
       COALESCE(m.function_source, 'none') AS function_source,
       m.function_derived, m.attribution, m.function_reason,
       m.samples, m.weight, m.tag_cycles,
       CASE WHEN m.hot_rva IS NULL THEN NULL ELSE printf('0x%X', m.hot_rva) END AS hot_rva,
       m.hot_samples,
       m.total_samples, m.total_weight, m.total_tag_cycles, 0 AS sort_key, m.rn,
       COALESCE(fe."isSystemModule", fb."isSystemModule") AS "isSystemModule"
FROM v_function_measured m
LEFT JOIN v_module_flags_exact fe ON fe.module_name = m.module_name
LEFT JOIN v_module_flags_basename fb ON fb.basename = regexp_extract(m.module_name, '([^/]+)$', 1)
WHERE getvariable('top_n') = 0 OR m.rn <= getvariable('top_n')
UNION ALL
SELECT '<other functions>', NULL, NULL, NULL, NULL, NULL, NULL, NULL,
       CAST(sum(samples) AS BIGINT), sum(weight), sum(tag_cycles), NULL, NULL,
       max(total_samples), max(total_weight), max(total_tag_cycles), 1, NULL, NULL
FROM v_function_measured
WHERE getvariable('top_n') > 0 AND rn > getvariable('top_n')
HAVING count(*) > 0;

-- The parent function selected by --function/--module/--function-start. Empty unless a
-- function-scoped instruction or line view was requested. It reads the same uncapped aggregated
-- rows as v_function_rows, so the parent measurement is a sum/join of the capture, never a second
-- measurement. Its children are the scoped samples with its group_key (v_scoped_keyed), so a
-- uProf Function.id row holds only the samples outside every derived range. --module is
-- normalized like every module key, so
-- Module.path or a Windows path in any case selects the same module.
CREATE OR REPLACE TEMP VIEW v_function_scope AS
SELECT * FROM v_function_measured
WHERE COALESCE(getvariable('uprof_function'), '') <> ''
  AND function_name = getvariable('uprof_function')
  AND module_name = module_key(COALESCE(getvariable('uprof_function_module'), ''))
  AND (COALESCE(getvariable('uprof_function_start'), '') = ''
       OR function_start = CAST(getvariable('uprof_function_start') AS BIGINT));

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
-- The folded body ends where a derived range starts after the folded start or where the
-- symbolizer frame names a function outside the fold (fold_holds).
-- When --top truncates, one <other lines> row carries the rest.
CREATE OR REPLACE TEMP VIEW v_line_rows AS
SELECT * FROM query(CASE
  WHEN COALESCE(getvariable('uprof_detail_views'), 'true') = 'true' THEN $line_rows$
-- Keep fs to two readers, the EXISTS test and the fs_one build: extra scalar reads of the
-- materialized snapshot stall DuckDB 2.0.0's task scheduler. MATERIALIZED still prevents
-- re-running v_function_measured per scoped row.
WITH fs AS MATERIALIZED (SELECT * FROM v_function_scope),
fs_one AS MATERIALIZED (
  SELECT samples AS parent_samples, weight AS parent_weight,
         tag_cycles AS parent_tag_cycles
  FROM fs LIMIT 1
),
scoped AS (
  SELECT w.*
  FROM v_scoped_keyed w
  WHERE COALESCE(getvariable('uprof_function'), '') = ''
     OR EXISTS (SELECT 1 FROM fs WHERE fs.group_key = w.group_key)
), placed AS (
  SELECT w.module_name, w.samples, w.weight, w.tag_cycles,
         s.function AS sym_function, s.function_short AS sym_short,
         s.containing_function AS sym_containing,
         s.file AS sym_file, s.line AS sym_line,
         a.names,
         fold_holds(a.name_count, a.names, s.containing_function)
           -- A derived range that starts after the folded public start ends the folded body.
           AND COALESCE(w.function_start <= a.rva, true) AS folded
  FROM scoped w
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
         CASE WHEN folded THEN 'shared: ' || names
              WHEN COALESCE(sym_function, '') = '' THEN '<no symbol>'
              ELSE sym_function END AS function_label,
         CASE WHEN folded THEN 'shared: ' || names
              WHEN COALESCE(sym_function, '') = '' THEN '<no symbol>'
              WHEN COALESCE(sp.overload_count, 0) > 1 THEN sym_function
              WHEN COALESCE(placed.sym_short, '') <> '' THEN placed.sym_short
              ELSE sym_function END AS function_key,
         COALESCE(NULLIF(module_name, ''), '<unknown module>') AS module_label,
         CASE WHEN folded THEN 'shared'
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
         (SELECT total_samples FROM v_scope_measures) AS total_samples,
         (SELECT total_weight FROM v_scope_measures) AS total_weight,
         (SELECT total_tag_cycles FROM v_scope_measures) AS total_tag_cycles,
         row_number() OVER (
           ORDER BY weight DESC NULLS LAST, samples DESC,
                    file_name, line_number, function_name, module_name) AS rn
  FROM agg LEFT JOIN fs_one ON true
)
SELECT file_name AS "file", line_number AS line, function_name AS "function",
       module_name AS module, attribution, samples, weight, tag_cycles,
       total_samples, total_weight, total_tag_cycles,
       parent_samples, parent_weight, parent_tag_cycles, 0 AS sort_key, rn
FROM ranked
WHERE getvariable('top_n') = 0 OR rn <= getvariable('top_n')
UNION ALL
SELECT '<other lines>', NULL, NULL, NULL, NULL,
       CAST(sum(samples) AS BIGINT), sum(weight), sum(tag_cycles),
       max(total_samples), max(total_weight), max(total_tag_cycles),
       max(parent_samples), max(parent_weight), max(parent_tag_cycles), 1, NULL
FROM ranked
WHERE getvariable('top_n') > 0 AND rn > getvariable('top_n')
HAVING count(*) > 0$line_rows$
  ELSE $line_rows_empty$
    SELECT CAST(NULL AS VARCHAR) AS "file", CAST(NULL AS INTEGER) AS line,
           CAST(NULL AS VARCHAR) AS "function", CAST(NULL AS VARCHAR) AS module,
           CAST(NULL AS VARCHAR) AS attribution, CAST(NULL AS BIGINT) AS samples,
           CAST(NULL AS HUGEINT) AS weight, CAST(NULL AS DOUBLE) AS tag_cycles,
           CAST(NULL AS BIGINT) AS total_samples, CAST(NULL AS HUGEINT) AS total_weight,
           CAST(NULL AS DOUBLE) AS total_tag_cycles, CAST(NULL AS BIGINT) AS parent_samples,
           CAST(NULL AS HUGEINT) AS parent_weight, CAST(NULL AS DOUBLE) AS parent_tag_cycles,
           CAST(NULL AS INTEGER) AS sort_key, CAST(NULL AS BIGINT) AS rn
    WHERE false$line_rows_empty$
  END);

-- Instruction shares, one row per module + RVA. Each address is annotated with the innermost
-- symbolizer frame (the executed source position, so an inlined instance names the inlinee);
-- addresses without a symbol keep their samples as '<no symbol>' with attribution 'unresolved'.
-- Rows and --top follow uprof_primary_measure: gap weight for a time-based capture, otherwise the
-- sample count (the same order as sample_share). When --top truncates, one <other instructions>
-- row carries the rest.
CREATE OR REPLACE TEMP VIEW v_instruction_rows AS
SELECT * FROM query(CASE
  WHEN COALESCE(getvariable('uprof_detail_views'), 'true') = 'true' THEN $instruction_rows$
-- Keep fs to two readers, the EXISTS test and the fs_one build: extra scalar reads of the
-- materialized snapshot stall DuckDB 2.0.0's task scheduler. MATERIALIZED still prevents
-- re-running v_function_measured per scoped row.
WITH fs AS MATERIALIZED (SELECT * FROM v_function_scope),
fs_one AS MATERIALIZED (
  SELECT samples AS parent_samples, weight AS parent_weight,
         tag_cycles AS parent_tag_cycles
  FROM fs LIMIT 1
),
scoped AS (
  SELECT w.*
  FROM v_scoped_keyed w
  WHERE COALESCE(getvariable('uprof_function'), '') = ''
     OR EXISTS (SELECT 1 FROM fs WHERE fs.group_key = w.group_key)
), placed AS (
  SELECT w.module_name, w.rva, w.samples, w.weight, w.tag_cycles,
         s.function AS sym_function, s.file AS sym_file, s.line AS sym_line,
         a.names,
         fold_holds(a.name_count, a.names, s.containing_function)
           -- A derived range that starts after the folded public start ends the folded body.
           AND COALESCE(w.function_start <= a.rva, true) AS folded
  FROM scoped w
  LEFT JOIN v_symbols s
    ON w.module_name = s.module_name AND w.rva = s.rva
  ASOF LEFT JOIN v_public_starts a
    ON w.module_name = a.module_name AND w.rva >= a.rva
), normalized AS (
  SELECT COALESCE(NULLIF(module_name, ''), '<unknown module>') AS module_name,
         rva,
         CASE WHEN folded THEN 'shared: ' || names
              WHEN COALESCE(sym_function, '') = '' THEN '<no symbol>'
              ELSE sym_function END AS function_name,
         CASE WHEN COALESCE(sym_function, '') = '' THEN '<no symbol>'
              WHEN COALESCE(sym_file, '') = '' THEN '<no source line>'
              ELSE sym_file END AS file_name,
         CASE WHEN COALESCE(sym_function, '') = '' THEN 0 ELSE COALESCE(sym_line, 0) END AS line_number,
         CASE WHEN folded THEN 'shared'
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
         (SELECT total_samples FROM v_scope_measures) AS total_samples,
         (SELECT total_weight FROM v_scope_measures) AS total_weight,
         (SELECT total_tag_cycles FROM v_scope_measures) AS total_tag_cycles,
         row_number() OVER (
           ORDER BY CASE getvariable('uprof_primary_measure')
                      WHEN 'time_share' THEN weight END DESC NULLS LAST,
                    samples DESC,
                    module_name, rva NULLS LAST, function_name) AS rn
  FROM agg LEFT JOIN fs_one ON true
)
SELECT module_name AS module,
       CASE WHEN rva IS NULL THEN NULL ELSE printf('0x%X', rva) END AS rva,
       function_name AS "function", file_name AS "file", line_number AS line,
       attribution, samples, weight, tag_cycles,
       total_samples, total_weight, total_tag_cycles,
       parent_samples, parent_weight, parent_tag_cycles, 0 AS sort_key, rn
FROM ranked
WHERE getvariable('top_n') = 0 OR rn <= getvariable('top_n')
UNION ALL
SELECT '<other instructions>', NULL, NULL, NULL, NULL, NULL,
       CAST(sum(samples) AS BIGINT), sum(weight), sum(tag_cycles),
       max(total_samples), max(total_weight), max(total_tag_cycles),
       max(parent_samples), max(parent_weight), max(parent_tag_cycles), 1, NULL
FROM ranked
WHERE getvariable('top_n') > 0 AND rn > getvariable('top_n')
HAVING count(*) > 0$instruction_rows$
  ELSE $instruction_rows_empty$
    SELECT CAST(NULL AS VARCHAR) AS module, CAST(NULL AS VARCHAR) AS rva,
           CAST(NULL AS VARCHAR) AS "function", CAST(NULL AS VARCHAR) AS "file",
           CAST(NULL AS INTEGER) AS line, CAST(NULL AS VARCHAR) AS attribution,
           CAST(NULL AS BIGINT) AS samples, CAST(NULL AS HUGEINT) AS weight,
           CAST(NULL AS DOUBLE) AS tag_cycles, CAST(NULL AS BIGINT) AS total_samples,
           CAST(NULL AS HUGEINT) AS total_weight, CAST(NULL AS DOUBLE) AS total_tag_cycles,
           CAST(NULL AS BIGINT) AS parent_samples, CAST(NULL AS HUGEINT) AS parent_weight,
           CAST(NULL AS DOUBLE) AS parent_tag_cycles, CAST(NULL AS INTEGER) AS sort_key,
           CAST(NULL AS BIGINT) AS rn
    WHERE false$instruction_rows_empty$
  END);

-- Region shares: one row per module + 4 KiB address page. Samples are grouped by address, not by
-- control flow; a page may span unrelated code, so it is a coarse bucket, not a contiguous region.
-- sampled_addresses says whether the page is a tight cluster or a diffuse bucket. hot_rva is the
-- highest-sample RVA in the page (ties pick the lowest); its symbolizer frame names function, file
-- and line, and the derived function range names it when the symbolizer has no frame. Rows and
-- --top follow uprof_primary_measure as v_function_rows does. When --top truncates, one
-- <other regions> row carries the rest.
CREATE OR REPLACE TEMP VIEW v_region_rows AS
WITH paged AS (
  SELECT COALESCE(NULLIF(w.module_name, ''), '<unknown module>') AS module_name,
         w.rva, w.samples, w.weight, w.tag_cycles,
         (w.rva // 4096) * 4096 AS page_start
  FROM v_scoped w
  WHERE w.rva IS NOT NULL
), page_totals AS (
  SELECT module_name, page_start,
         CAST(sum(samples) AS BIGINT) AS samples,
         sum(weight) AS weight,
         sum(tag_cycles) AS tag_cycles,
         count(DISTINCT rva) AS sampled_addresses
  FROM paged
  GROUP BY module_name, page_start
), rva_samples AS (
  SELECT module_name, page_start, rva, CAST(sum(samples) AS BIGINT) AS samples
  FROM paged
  GROUP BY module_name, page_start, rva
), rva_ranked AS (
  SELECT *,
         row_number() OVER (PARTITION BY module_name, page_start
                            ORDER BY samples DESC, rva) AS hot_rank
  FROM rva_samples
), hot AS (
  SELECT module_name, page_start, rva AS hot_rva, samples AS hot_samples
  FROM rva_ranked
  WHERE hot_rank = 1
), placed AS (
  SELECT p.*, h.hot_rva, h.hot_samples,
         s.function AS sym_function, s.file AS hot_file, s.line AS hot_line,
         a.names,
         fold_holds(a.name_count, a.names, s.containing_function)
           AND COALESCE(r.function_start <= a.rva, true) AS folded,
         r.function_start AS hot_function_start,
         r.function_source AS ranged_source,
         r.function_name AS hot_range_name
  FROM page_totals p
  JOIN hot h USING (module_name, page_start)
  LEFT JOIN v_symbols s
    ON s.module_name = p.module_name AND s.rva = h.hot_rva
  ASOF LEFT JOIN v_public_starts a
    ON a.module_name = p.module_name AND h.hot_rva >= a.rva
  LEFT JOIN v_function_ranges r
    ON r.module_name = p.module_name
   AND h.hot_rva >= r.function_start AND h.hot_rva < r.function_end
), labelled AS (
  SELECT *,
         CASE WHEN folded THEN 'shared: ' || names
              WHEN COALESCE(sym_function, '') <> '' THEN sym_function
              WHEN hot_range_name IS NOT NULL THEN hot_range_name
              WHEN hot_function_start IS NOT NULL AND ranged_source = 'pdata'
                THEN printf('pdata_0x%X', hot_function_start)
              WHEN hot_function_start IS NOT NULL THEN printf('sub_0x%X', hot_function_start)
              ELSE NULL END AS hot_function,
         CASE WHEN folded THEN 'shared'
              WHEN COALESCE(sym_function, '') <> '' THEN 'unique'
              WHEN hot_range_name IS NOT NULL THEN 'unique'
              WHEN hot_function_start IS NOT NULL THEN 'derived'
              ELSE 'unresolved' END AS attribution,
         CASE WHEN NOT folded AND COALESCE(sym_function, '') = ''
                   AND hot_function_start IS NULL
              THEN 'no symbolizer frame and no derived function range cover hot_rva; the page is the coarse bucket'
              WHEN folded AND COALESCE(sym_function, '') = ''
              THEN 'no source line: hot_rva is in a folded body that several public names share, and there is no symbolizer file/line'
              WHEN COALESCE(sym_function, '') = '' AND hot_function_start IS NOT NULL
                   AND COALESCE(hot_file, '') = ''
              THEN 'no source line: the derived function range names hot_rva but there is no symbolizer file/line'
              WHEN COALESCE(sym_function, '') <> '' AND COALESCE(hot_file, '') = ''
              THEN 'no source line: the symbolizer named hot_rva''s function but returned no file/line'
              ELSE NULL END AS symbol_reason,
         CASE WHEN hot_function_start IS NULL THEN 'none' ELSE ranged_source END
           AS function_source,
         CASE WHEN hot_function_start IS NOT NULL AND COALESCE(sym_function, '') = ''
                   AND hot_range_name IS NULL
              THEN true ELSE false END AS function_derived
  FROM placed
), measured AS (
  SELECT labelled.*,
         CAST(sum(samples) OVER () AS BIGINT) AS total_samples,
         sum(weight) OVER () AS total_weight,
         sum(tag_cycles) OVER () AS total_tag_cycles,
         row_number() OVER (
           ORDER BY CASE getvariable('uprof_primary_measure')
                      WHEN 'time_share' THEN weight END DESC NULLS LAST,
                    samples DESC, page_start, module_name) AS rn
  FROM labelled
)
SELECT m.module_name AS module,
       printf('0x%X', m.page_start) AS page_start,
       printf('0x%X', m.page_start + 4096) AS page_end,
       m.samples, m.weight, m.tag_cycles,
       m.total_samples, m.total_weight, m.total_tag_cycles, 0 AS sort_key, m.rn,
       m.sampled_addresses, m.hot_rva, m.hot_samples,
       m.hot_function, m.hot_file, m.hot_line,
       m.attribution, m.symbol_reason,
       COALESCE(m.function_source, 'none') AS function_source, m.function_derived,
       COALESCE(fe."isSystemModule", fb."isSystemModule") AS "isSystemModule"
FROM measured m
LEFT JOIN v_module_flags_exact fe ON fe.module_name = m.module_name
LEFT JOIN v_module_flags_basename fb ON fb.basename = regexp_extract(m.module_name, '([^/]+)$', 1)
WHERE getvariable('top_n') = 0 OR m.rn <= getvariable('top_n')
UNION ALL
SELECT '<other regions>', NULL, NULL, CAST(sum(samples) AS BIGINT), sum(weight), sum(tag_cycles),
       max(total_samples), max(total_weight), max(total_tag_cycles), 1, NULL,
       NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL
FROM measured
WHERE getvariable('top_n') > 0 AND rn > getvariable('top_n')
HAVING count(*) > 0;
