CREATE OR REPLACE VIEW rag_analytics.overall_score_cumulative AS
SELECT
  ROUND(AVG(overall_score) * 10, 1) AS avg_overall_percent,
  COUNT(*) AS total_evaluated_responses
FROM rag_analytics.analytics
WHERE overall_score IS NOT NULL
  AND out_of_scope = false;
