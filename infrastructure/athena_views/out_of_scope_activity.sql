CREATE OR REPLACE VIEW rag_analytics.out_of_scope_activity AS
SELECT
  date_trunc('week', date_parse(date, '%Y-%m-%d')) AS week,
  user_id,
  COUNT(*) AS out_of_scope_count
FROM rag_analytics.analytics
WHERE out_of_scope = true
GROUP BY 1, 2
ORDER BY 1 DESC, 3 DESC;
