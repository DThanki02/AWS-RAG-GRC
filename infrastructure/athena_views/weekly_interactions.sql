CREATE OR REPLACE VIEW rag_analytics.weekly_interactions AS
SELECT date_trunc('week', date_parse(date, '%Y-%m-%d')) AS week, COUNT(*) AS total_interactions
FROM rag_analytics.analytics GROUP BY 1;
