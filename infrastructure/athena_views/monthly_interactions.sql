CREATE OR REPLACE VIEW rag_analytics.monthly_interactions AS
SELECT date_trunc('month', date_parse(date, '%Y-%m-%d')) AS month, COUNT(*) AS total_interactions
FROM rag_analytics.analytics GROUP BY 1;
