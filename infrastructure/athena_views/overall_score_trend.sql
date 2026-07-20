CREATE OR REPLACE VIEW rag_analytics.overall_score_trend AS
SELECT
  date_trunc('week', date_parse(date, '%Y-%m-%d')) AS week,
  ROUND(AVG(overall_score) * 10, 1) AS avg_overall_percent,
  COUNT(*) AS sample_size
FROM rag_analytics.analytics
WHERE overall_score IS NOT NULL
  AND out_of_scope = false
GROUP BY 1;
