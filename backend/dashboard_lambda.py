"""
dashboard_lambda.py
Runs the 5 usage-analytics Athena queries and returns them as JSON for the dashboard frontend.
Results are cached in DynamoDB for CACHE_TTL_SECONDS to avoid re-running Athena queries on every page load.
"""

import json
import os
import time
import boto3
from decimal import Decimal

REGION            = os.environ.get("AWS_REGION", "XXXXXX")
ATHENA_DATABASE   = os.environ.get("ATHENA_DATABASE", "XXXXXX")
ATHENA_OUTPUT_S3  = os.environ.get("ATHENA_OUTPUT_S3", "")
CACHE_TABLE_NAME  = os.environ.get("CACHE_TABLE_NAME", "XXXXXX")
CACHE_TTL_SECONDS = int(os.environ.get("CACHE_TTL_SECONDS", "300"))

athena   = boto3.client("athena", region_name=REGION)
dynamodb = boto3.resource("dynamodb", region_name=REGION)
cache_table = dynamodb.Table(CACHE_TABLE_NAME)

QUERIES = {
    "weekly_interactions":       "SELECT * FROM weekly_interactions ORDER BY week",
    "monthly_interactions":      "SELECT * FROM monthly_interactions ORDER BY month",
    "interactions_by_use_case":  "SELECT * FROM interactions_by_use_case ORDER BY week, use_case",
    "top_users":                 "SELECT * FROM top_users ORDER BY week DESC, total_interactions DESC",
    "cost_trend":                "SELECT * FROM cost_trend ORDER BY week",
    "overall_score_trend":       "SELECT * FROM overall_score_trend ORDER BY week",
    "overall_score_cumulative":  "SELECT * FROM overall_score_cumulative",
    "out_of_scope_activity":     "SELECT * FROM out_of_scope_activity ORDER BY week DESC, out_of_scope_count DESC",
}


def run_athena_query(sql: str) -> list:
    """Executes an Athena query synchronously (polls until done) and returns rows as list of dicts."""
    exec_response = athena.start_query_execution(
        QueryString=sql,
        QueryExecutionContext={"Database": ATHENA_DATABASE},
        ResultConfiguration={"OutputLocation": ATHENA_OUTPUT_S3}
    )
    query_id = exec_response["QueryExecutionId"]

    for _ in range(30):
        status_response = athena.get_query_execution(QueryExecutionId=query_id)
        state = status_response["QueryExecution"]["Status"]["State"]
        if state in ("SUCCEEDED", "FAILED", "CANCELLED"):
            break
        time.sleep(1)

    if state != "SUCCEEDED":
        reason = status_response["QueryExecution"]["Status"].get("StateChangeReason", "unknown error")
        raise Exception(f"Athena query failed ({state}): {reason}")

    results = athena.get_query_results(QueryExecutionId=query_id)
    rows = results["ResultSet"]["Rows"]

    if not rows:
        return []

    headers = [col.get("VarCharValue", "") for col in rows[0]["Data"]]
    data = []
    for row in rows[1:]:
        values = [col.get("VarCharValue", "") for col in row["Data"]]
        data.append(dict(zip(headers, values)))

    return data


def get_cached_or_fetch() -> dict:
    """Returns cached dashboard data if fresh, otherwise re-runs all Athena queries and re-caches."""
    now = int(time.time())

    try:
        cached = cache_table.get_item(Key={"cache_key": "dashboard"}).get("Item")
    except Exception:
        cached = None

    if cached and (now - int(cached.get("cached_at", 0))) < CACHE_TTL_SECONDS:
        return {
            "data": json.loads(cached["data"]),
            "cached": True,
            "cached_at": int(cached["cached_at"])
        }

    fresh_data = {}
    for key, sql in QUERIES.items():
        fresh_data[key] = run_athena_query(sql)

    try:
        cache_table.put_item(Item={
            "cache_key": "dashboard",
            "data": json.dumps(fresh_data),
            "cached_at": now
        })
    except Exception as e:
        print(f"CACHE WRITE ERROR: {e}") 

    return {"data": fresh_data, "cached": False, "cached_at": now}


def lambda_handler(event, context):
    method = event.get("requestContext", {}).get("http", {}).get("method")

    if method == "OPTIONS":
        return cors_response(200, {})

    # Enforce admin-only access using the group claim from the Cognito JWT authorizer.
    claims = event.get("requestContext", {}).get("authorizer", {}).get("jwt", {}).get("claims", {})
    groups_raw = claims.get("cognito:groups", "")
    groups = groups_raw if isinstance(groups_raw, list) else str(groups_raw).replace("[", "").replace("]", "").split(",")
    groups = [g.strip() for g in groups]

    if "ADMIN_GROUP" not in groups:
        return cors_response(403, {"error": "Admin access required."})

    try:
        result = get_cached_or_fetch()
        return cors_response(200, result)
    except Exception as e:
        print(f"DASHBOARD ERROR: {e}")
        return cors_response(500, {"error": str(e)})


def cors_response(status_code: int, body: dict) -> dict:
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type, Authorization"
        },
        "body": json.dumps(body)
    }
