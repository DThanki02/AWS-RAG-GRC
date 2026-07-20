CREATE OR REPLACE VIEW rag_analytics.interactions_by_use_case AS
SELECT date_trunc('week', date_parse(date, '%Y-%m-%d')) AS week, use_case, COUNT(*) AS total_interactions
FROM rag_analytics.analytics GROUP BY 1, 2;
