CREATE OR REPLACE VIEW rag_analytics.top_users AS
SELECT date_trunc('week', date_parse(date, '%Y-%m-%d')) AS week, user_id, COUNT(*) AS total_interactions
FROM rag_analytics.analytics GROUP BY 1, 2;
