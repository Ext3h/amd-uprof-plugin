-- Live schema of the capture, ordered for reading and diffing. Run when the capture may come
-- from a different uProf version or when the views.sql guard reports a missing column.
SELECT table_name, column_name, data_type
FROM information_schema.columns
WHERE table_schema = 'main'
ORDER BY table_name, ordinal_position;
