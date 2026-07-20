CREATE OR REPLACE VIEW rag_analytics.cost_trend AS
SELECT date_trunc('week', date_parse(date, '%Y-%m-%d')) AS week, SUM(estimated_cost_usd) AS total_cost_usd
FROM rag_analytics.analytics GROUP BY 1;
