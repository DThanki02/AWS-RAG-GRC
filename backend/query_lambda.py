"""
lambda_function.py
Handles RAG queries via Bedrock Managed Knowledge Base (Google Drive data source) + Nova Lite eval scores.
"""

import json                              # parse and create JSON (reading request body, building response, parsing eval scores)
import re                                # regex which is used to strip markdown fences from Nova Lite's eval response
import os                                # access env vars
import time                              # measure latency for QA and eval calls
import boto3                             # connects to bedrock, S3 and other AWS services
from boto3.dynamodb.conditions import Key  # DynamoDB query condition expressions
from decimal import Decimal              # DynamoDB requires Decimal, not float, for numeric attributes

KNOWLEDGE_BASE_ID    = os.environ.get("KNOWLEDGE_BASE_ID", "")
REGION               = os.environ.get("AWS_REGION", "XXXXXX")
ACCOUNT_ID           = os.environ.get("ACCOUNT_ID", "")
MODEL_REGION_PREFIX  = os.environ.get("MODEL_REGION_PREFIX", "XX")
LOG_BUCKET           = os.environ.get("LOG_BUCKET", "")

# Model ARNs
QA_MODEL_ARN   = f"arn:aws:bedrock:{REGION}:{ACCOUNT_ID}:inference-profile/{MODEL_REGION_PREFIX}.anthropic.claude-sonnet-4-6"
EVAL_MODEL_ID  = f"{MODEL_REGION_PREFIX}.amazon.nova-lite-v1:0"

USE_CASE           = os.environ.get("USE_CASE", "XXXXXX")

RETRIEVE_K          = 20
MIN_SCORE_THRESHOLD = 0.5
MAX_CHUNKS          = 10
ANALYTICS_BUCKET   = os.environ.get("ANALYTICS_BUCKET", "")

# Fixed per-user monthly token caps — same policy applies to every user, not configurable per-person
MONTHLY_INPUT_TOKEN_LIMIT  = 420000
MONTHLY_OUTPUT_TOKEN_LIMIT = 80000

# Pricing per 1K tokens (USD) — VERIFY against current Bedrock pricing page for your region before relying on these
PRICE_PER_1K = {
    "qa_input":    0.003,    # Claude Sonnet 4.5 input
    "qa_output":   0.015,    # Claude Sonnet 4.5 output
    "eval_input":  0.00006,  # Nova Lite input
    "eval_output": 0.00024,  # Nova Lite output
}

# Clients (initialised outside handler for Lambda reuse)
bedrock_agent  = boto3.client("bedrock-agent-runtime", region_name=REGION)
bedrock_rt     = boto3.client("bedrock-runtime",       region_name=REGION)
s3_client      = boto3.client("s3",                    region_name=REGION)
dynamodb       = boto3.resource("dynamodb",             region_name=REGION)

BUDGET_TABLE_NAME  = os.environ.get("BUDGET_TABLE_NAME", "XXXXXX")
HISTORY_TABLE_NAME = os.environ.get("HISTORY_TABLE_NAME", "XXXXXX")
budget_table       = dynamodb.Table(BUDGET_TABLE_NAME) if BUDGET_TABLE_NAME else None
history_table      = dynamodb.Table(HISTORY_TABLE_NAME) if HISTORY_TABLE_NAME else None

SYSTEM_PROMPT = """You are a precise document question-answering assistant for internal corporate use.
Your sole source of truth is the Context provided below.
The Context is extracted from documents and may contain tables, key-value pairs, or imperfectly formatted text.
Use your full reasoning ability to locate and extract the answer from the Context,
including identifying relevant values from tables, inferring field relationships,
and interpreting abbreviated or irregularly spaced text.
Do not generate any emojis as part of the response
If the answer can be reasonably derived from what is written in the Context, state it clearly and concisely.
Do not mention HR, contacting anyone, or add any closing remarks yourself — that will be handled separately.
If, after careful reading, the Context genuinely contains no information related to the question,
respond with exactly this marker and nothing else: [[NOT_FOUND]]

$search_results$"""

HR_CONTACT_LINE = "\n\nFor more information, please contact HR."
NOT_FOUND_MESSAGE = (
    "This information is not available in my knowledge base. "
    "If you'd like to enquire further, please contact HR."
)


def _extract_source(location: dict) -> str:
    if not location:
        return "unknown"

    gdrive_loc = location.get("googleDriveLocation")
    if gdrive_loc and gdrive_loc.get("url"):
        return gdrive_loc["url"]

    for key in ("webLocation", "sharePointLocation", "confluenceLocation", "salesforceLocation"):
        loc = location.get(key)
        if loc and loc.get("url"):
            return loc["url"]

    s3_loc = location.get("s3Location")
    if s3_loc and s3_loc.get("uri"):
        return s3_loc["uri"].split("/")[-1]

    return location.get("type", "unknown")

def query_knowledge_base(question: str) -> dict:
    retrieve_response = bedrock_agent.retrieve(
        knowledgeBaseId=KNOWLEDGE_BASE_ID,
        retrievalQuery={"text": question},
        retrievalConfiguration={"managedSearchConfiguration": {"numberOfResults": RETRIEVE_K}}
    )

    chunks = []
    for result in retrieve_response.get("retrievalResults", []):
        content   = result.get("content", {}).get("text", "")
        location  = result.get("location", {})
        metadata  = result.get("metadata", {})
        file_name = metadata.get("_document_title")
        source    = file_name if file_name else _extract_source(location)
        score     = float(result.get("score", 0))
        if content and score >= MIN_SCORE_THRESHOLD:
            chunks.append({
                "content": content,
                "source":  source,
                "url":     _extract_source(location),
                "score":   round(score, 4)
            })
            if len(chunks) >= MAX_CHUNKS:
                break

    # Step 2: build context and call Claude directly
    context = "\n\n".join(c["content"] for c in chunks)
    prompt  = SYSTEM_PROMPT.replace("$search_results$", context)

    response = bedrock_rt.converse(
        modelId=QA_MODEL_ARN,
        messages=[{"role": "user", "content": [{"text": question}]}],
        system=[{"text": prompt}],
        inferenceConfig={"temperature": 0.0, "maxTokens": 1000}
    )

    answer = response["output"]["message"]["content"][0]["text"].strip()
    usage  = response.get("usage", {})
    return {
        "answer": answer,
        "chunks": chunks,
        "qa_input_tokens":  usage.get("inputTokens", 0),
        "qa_output_tokens": usage.get("outputTokens", 0)
    }


def evaluate(question: str, answer: str, chunks: list) -> dict:
    # Pass chunks individually and numbered so the evaluator can judge retrieval precision
    # (i.e. identify exactly which chunks were actually needed vs. noise), not just a flattened blob.
    numbered_chunks = "\n\n".join(
        f"[Chunk {i+1}]\n{c['content']}" for i, c in enumerate(chunks)
    )[:4000]

    system_prompt = (
        "You are a strict RAG evaluation assistant. "
        "You will be given a User Question, a set of numbered Retrieved Context chunks, and a Generated Answer. "
        "Score on THREE dimensions, each from 0 to 10:\n"
        "\n"
        "1. faithfulness (groundedness / anti-hallucination): Is the Answer entirely backed up by the Retrieved "
        "Context, or is the LLM making things up? Check every claim in the Answer against the Context — do not "
        "use outside knowledge or judge real-world plausibility, only check against what the Context actually says. "
        "10 = every claim in the Answer is directly supported by the Context, nothing invented. "
        "7-9 = mostly supported, with only minor unsupported detail. "
        "4-6 = a mix — some claims supported, others invented or unverifiable from the Context. "
        "1-3 = mostly fabricated, with only a small supported element. "
        "0 = the Answer is entirely invented or directly contradicts the Context. "
        "If the Answer correctly says it cannot find the information AND the Context truly lacks it, score 10.\n"
        "\n"
        "2. answer_relevance: Did the LLM actually answer the User Question, or did it go off on a tangent? "
        "Judge this independently of whether the Answer is correct or grounded — this is purely about whether "
        "it addresses what was actually asked. "
        "10 = directly and completely addresses the Question. "
        "4-6 = partially addresses it, or answers a related-but-different question. "
        "0 = does not address the Question at all.\n"
        "\n"
        "3. context_relevance (retrieval precision): Look strictly at the User Question and the numbered Retrieved "
        "Context chunks. Identify exactly which chunks (or which sentences within them) are actually necessary to "
        "answer the Question. Penalize the score if the retrieval pulled in a lot of irrelevant, off-topic, or "
        "redundant chunks alongside (or instead of) the useful ones — this measures retrieval precision, not just "
        "whether the answer *could* be found somewhere in there. "
        "10 = nearly all retrieved chunks are directly relevant and necessary, minimal noise. "
        "7-9 = mostly relevant, with one chunk or so of avoidable noise. "
        "4-6 = a mix of relevant and irrelevant chunks — meaningful noise diluting precision. "
        "1-3 = mostly irrelevant chunks, with only a small relevant portion. "
        "0 = none of the retrieved chunks are relevant to the Question.\n"
        "\n"
        "IMPORTANT: These three scores are independent and can diverge — e.g. context_relevance can be low "
        "(noisy retrieval) even if the Answer is still faithfully grounded in the one relevant chunk that was "
        "present, and answer_relevance can be low even if faithfulness is high (a well-grounded but off-topic answer).\n"
        "\n"
        "Respond with ONLY a valid JSON object, nothing else — no markdown fences, no preamble, no explanation outside the JSON. "
        "Use this exact structure with real integer values:\n"
        '{"faithfulness": 0, "answer_relevance": 0, "context_relevance": 0, "reasoning": "one or two sentences, '
        'noting specifically which chunks (by number) were actually necessary if context_relevance is not 10"}'
    )

    user_message = (
        f"User Question: {question}\n\n"
        f"Retrieved Context:\n{numbered_chunks}\n\n"
        f"Generated Answer: {answer}"
    )

    response = bedrock_rt.converse(
        modelId=EVAL_MODEL_ID,
        system=[{"text": system_prompt}],
        messages=[{"role": "user", "content": [{"text": user_message}]}],
        inferenceConfig={"temperature": 0.0, "maxTokens": 350}
    )

    raw = response["output"]["message"]["content"][0]["text"].strip()
    clean = re.sub(r"```(?:json)?|```", "", raw).strip()
    eval_usage = response.get("usage", {})

    try:
        scores = json.loads(clean)
        for k in ["faithfulness", "answer_relevance", "context_relevance"]:
            if k in scores:
                scores[k] = max(0, min(10, int(scores[k])))

        metric_vals = [scores.get(k) for k in ["faithfulness", "answer_relevance", "context_relevance"]]
        if all(v is not None for v in metric_vals):
            scores["overall"] = round(sum(metric_vals) / len(metric_vals), 1)
        else:
            scores["overall"] = None

        scores["eval_input_tokens"]  = eval_usage.get("inputTokens", 0)
        scores["eval_output_tokens"] = eval_usage.get("outputTokens", 0)
        return scores
    except Exception:
        return {
            "faithfulness":     None,
            "answer_relevance": None,
            "context_relevance": None,
            "overall":          None,
            "reasoning":        raw,
            "eval_input_tokens":  eval_usage.get("inputTokens", 0),
            "eval_output_tokens": eval_usage.get("outputTokens", 0)
        }



def log_to_s3(session_id: str, question: str, answer: str, scores: dict, latency_ms: int, chunks: list):
    if not LOG_BUCKET or not session_id:
        return  

    key = f"logs/{session_id}.md"

    chunks_md = ""
    if chunks:
        chunks_md = "**Retrieved Chunks:**\n\n"
        for i, c in enumerate(chunks, start=1):
            snippet = c.get("content", "").replace("\n", " ").strip()
            if len(snippet) > 400:
                snippet = snippet[:400] + "…"
            chunks_md += (
                f"{i}. Source: {c.get('source', 'unknown')} | Score: {c.get('score', '—')}\n"
                f"   > {snippet}\n\n"
            )
    else:
        chunks_md = "**Retrieved Chunks:** none\n\n"

    entry = (
        f"## Query at {__import__('datetime').datetime.utcnow().isoformat()}Z\n\n"
        f"**Question:**\n{question}\n\n"
        f"**Answer:**\n{answer}\n\n"
        f"**Evaluation:**\n"
        f"- Faithfulness: {scores.get('faithfulness', '—')}/10\n"
        f"- Answer Relevance: {scores.get('answer_relevance', '—')}/10\n"
        f"- Context Relevance: {scores.get('context_relevance', '—')}/10\n"
        f"- Overall: {scores.get('overall', '—')}/10\n"
        f"- Reasoning: {scores.get('reasoning', '—')}\n\n"
        f"{chunks_md}"
        f"**Latency:** {latency_ms} ms\n\n"
        f"---\n\n"
    )

    try:
        existing = s3_client.get_object(Bucket=LOG_BUCKET, Key=key)["Body"].read().decode("utf-8")
    except s3_client.exceptions.NoSuchKey:
        existing = f"# Session Log — {session_id}\n\n"
    except Exception:
        existing = f"# Session Log — {session_id}\n\n"

    s3_client.put_object(
        Bucket=LOG_BUCKET,
        Key=key,
        Body=(existing + entry).encode("utf-8"),
        ContentType="text/markdown"
    )


def write_analytics_record(session_id: str, user_id: str, question: str, answer: str,
                            scores: dict, latency_ms: int, qa_tokens: dict, out_of_scope: bool = False):
    """
    Writes one structured JSON record per interaction to S3, partitioned by date
    """
    if not ANALYTICS_BUCKET:
        return  # analytics not configured, skip silently

    now = __import__('datetime').datetime.utcnow()
    date_str = now.strftime("%Y-%m-%d")

    qa_in    = qa_tokens.get("qa_input_tokens", 0)
    qa_out   = qa_tokens.get("qa_output_tokens", 0)
    eval_in  = scores.get("eval_input_tokens", 0)
    eval_out = scores.get("eval_output_tokens", 0)

    cost = (
        (qa_in    / 1000) * PRICE_PER_1K["qa_input"] +
        (qa_out   / 1000) * PRICE_PER_1K["qa_output"] +
        (eval_in  / 1000) * PRICE_PER_1K["eval_input"] +
        (eval_out / 1000) * PRICE_PER_1K["eval_output"]
    )

    record = {
        "timestamp":           now.isoformat() + "Z",
        "record_date":         date_str,
        "session_id":          session_id,
        "user_id":             user_id or "anonymous",
        "use_case":            USE_CASE,
        "question":            question,
        "answer_length":       len(answer),
        "faithfulness_score":     scores.get("faithfulness"),
        "answer_relevance_score": scores.get("answer_relevance"),
        "context_relevance_score": scores.get("context_relevance"),
        "overall_score":          scores.get("overall"),
        "out_of_scope":        out_of_scope,
        "latency_ms":          latency_ms,
        "qa_input_tokens":     qa_in,
        "qa_output_tokens":    qa_out,
        "eval_input_tokens":   eval_in,
        "eval_output_tokens":  eval_out,
        "estimated_cost_usd":  round(cost, 6)
    }

    # One JSON object per line (newline-delimited JSON) — Athena/Glue read this natively
    key = f"analytics/date={date_str}/{session_id}_{int(time.time()*1000)}.json"

    s3_client.put_object(
        Bucket=ANALYTICS_BUCKET,
        Key=key,
        Body=(json.dumps(record) + "\n").encode("utf-8"),
        ContentType="application/json"
    )


def write_query_history(user_id: str, question: str, answer: str, scores: dict, latency_ms: int):
    """
    Stores the query in a per-user history table so the my-usage page can show past questions
    with evaluation scores
    """
    if not history_table:
        return

    now = __import__('datetime').datetime.utcnow()
    try:
        history_table.put_item(Item={
            "user_id":            user_id or "anonymous",
            "timestamp":          now.isoformat() + "Z",
            "question":           question,
            "answer":             answer[:4000],
            "evaluation": {
                "faithfulness":      Decimal(str(scores.get("faithfulness", 0))),
                "answer_relevance":  Decimal(str(scores.get("answer_relevance", 0))),
                "context_relevance": Decimal(str(scores.get("context_relevance", 0))),
                "overall":           Decimal(str(scores.get("overall", 0))),
            },
            "latency_ms":         latency_ms,
        })
    except Exception as e:
        print(f"HISTORY WRITE ERROR: {e}")


def get_query_history(user_id: str, limit: int = 50) -> list:
    """
    Retrieves recent query history for a user, sorted by most recent first.
    """
    if not history_table:
        return []

    safe_user = user_id or "anonymous"
    try:
        response = history_table.query(
            KeyConditionExpression=Key("user_id").eq(safe_user),
            ScanIndexForward=False,
            Limit=limit,
        )
        items = response.get("Items", [])
        history = []
        for item in items:
            eval_raw = item.get("evaluation", {})
            history.append({
                "timestamp":  item.get("timestamp", ""),
                "question":   item.get("question", ""),
                "answer":     item.get("answer", ""),
                "evaluation": {
                    "faithfulness":      int(eval_raw.get("faithfulness", 0)),
                    "answer_relevance":  int(eval_raw.get("answer_relevance", 0)),
                    "context_relevance": int(eval_raw.get("context_relevance", 0)),
                    "overall":           int(eval_raw.get("overall", 0)),
                },
            })
        return history
    except Exception as e:
        print(f"HISTORY READ ERROR: {e}")
        return []


def update_budget_consumption(input_tokens: int, output_tokens: int, user_id: str) -> dict:
    """
    Atomically increments THIS USER's consumed input/output token counts for the current month in DynamoDB.
    """
    if not budget_table:
        return {"consumed_input_tokens": None, "consumed_output_tokens": None}

    now = __import__('datetime').datetime.utcnow()
    safe_user = user_id or "anonymous"
    budget_id = f"{USE_CASE}#{safe_user}#{now.strftime('%Y-%m')}"

    try:
        response = budget_table.update_item(
            Key={"budget_id": budget_id},
            UpdateExpression="ADD consumed_input_tokens :in_tok, consumed_output_tokens :out_tok, consumed_tokens :total_tok",
            ExpressionAttributeValues={
                ":in_tok": input_tokens,
                ":out_tok": output_tokens,
                ":total_tok": input_tokens + output_tokens
            },
            ReturnValues="UPDATED_NEW"
        )
        attrs = response.get("Attributes", {})
        return {
            "consumed_input_tokens": int(attrs.get("consumed_input_tokens", 0)),
            "consumed_output_tokens": int(attrs.get("consumed_output_tokens", 0))
        }
    except Exception as e:
        print(f"BUDGET UPDATE ERROR: {e}")
        return {"consumed_input_tokens": None, "consumed_output_tokens": None}


def get_budget_status(user_id: str) -> dict:
    """
    Reads THIS USER's current month budget item and returns consumption vs the fixed per-user caps
    for display in the frontend.
    """
    if not budget_table:
        return {"available": False}

    now = __import__('datetime').datetime.utcnow()
    safe_user = user_id or "anonymous"
    budget_id = f"{USE_CASE}#{safe_user}#{now.strftime('%Y-%m')}"

    response = budget_table.get_item(Key={"budget_id": budget_id})
    item = response.get("Item") or {}

    consumed_input  = int(item.get("consumed_input_tokens", 0))
    consumed_output = int(item.get("consumed_output_tokens", 0))
    consumed_total   = int(item.get("consumed_tokens", 0))

    percent_input  = round((consumed_input / MONTHLY_INPUT_TOKEN_LIMIT) * 100, 1) if MONTHLY_INPUT_TOKEN_LIMIT > 0 else 0
    percent_output = round((consumed_output / MONTHLY_OUTPUT_TOKEN_LIMIT) * 100, 1) if MONTHLY_OUTPUT_TOKEN_LIMIT > 0 else 0
    combined_limit = MONTHLY_INPUT_TOKEN_LIMIT + MONTHLY_OUTPUT_TOKEN_LIMIT
    percent_total  = round((consumed_total / combined_limit) * 100, 1) if combined_limit > 0 else 0

    sync_resp = budget_table.get_item(Key={"budget_id": "__kb_sync_timestamp"})
    sync_item = sync_resp.get("Item") or {}
    kb_synced_at = sync_item.get("synced_at", "")

    return {
        "available":       True,
        "user_id":         safe_user,
        "consumed_tokens": consumed_total,
        "consumed_input_tokens":  consumed_input,
        "consumed_output_tokens": consumed_output,
        "input_token_limit":  MONTHLY_INPUT_TOKEN_LIMIT,
        "output_token_limit": MONTHLY_OUTPUT_TOKEN_LIMIT,
        "token_limit":     combined_limit,
        "percent_used":    percent_total,
        "percent_input_used":  percent_input,
        "percent_output_used": percent_output,
        "month":           now.strftime('%Y-%m'),
        "history":         get_query_history(safe_user),
        "kb_synced_at":    kb_synced_at,
    }



def lambda_handler(event, context):
    # CORS preflight
    if event.get("requestContext", {}).get("http", {}).get("method") == "OPTIONS":
        return cors_response(200, {})

    http_method = event.get("requestContext", {}).get("http", {}).get("method")

    if http_method == "GET":
        try:
            query_params = event.get("queryStringParameters") or {}
            user_id_param = query_params.get("user_id", "")
            return cors_response(200, get_budget_status(user_id_param))
        except Exception as e:
            print(f"BUDGET FETCH ERROR: {e}")
            return cors_response(500, {"error": str(e)})

    try:
        request_start = time.time()
        body       = json.loads(event.get("body", "{}"))
        question   = body.get("question", "").strip()
        session_id = body.get("session_id", "").strip()
        user_id    = body.get("user_id", "").strip()

        if not question:
            return cors_response(400, {"error": "question is required"})

        # Check if user has exceeded their total token budget before processing
        budget_status = get_budget_status(user_id)
        if budget_status.get("available") and budget_status.get("percent_used", 0) >= 100:
            return cors_response(429, {"error": "Monthly token limit reached. Please try again next month.", "budget_exceeded": True})

        # Query KB
        kb_result = query_knowledge_base(question)
        raw_answer = kb_result["answer"]
        chunks     = kb_result["chunks"]

        is_not_found = "[[NOT_FOUND]]" in raw_answer

        # Evaluate the substantive answer (pre-boilerplate) so scoring reflects actual content
        eval_answer = "The requested information is not present in the provided context." if is_not_found else raw_answer
        scores = evaluate(question, eval_answer, chunks)

        # Now build the final, corporate-toned response text for display/logging
        if is_not_found:
            answer = NOT_FOUND_MESSAGE
        else:
            answer = raw_answer.strip() + HR_CONTACT_LINE

        latency_ms = round((time.time() - request_start) * 1000)

        # Log this query/response to S3 (best-effort, never blocks the response)
        try:
            log_to_s3(session_id, question, answer, scores, latency_ms, chunks)
        except Exception as log_err:
            print(f"LOGGING ERROR: {log_err}")

        # Write structured analytics record (best-effort, never blocks the response)
        try:
            write_analytics_record(session_id, user_id, question, answer, scores, latency_ms, kb_result, is_not_found)
        except Exception as analytics_err:
            print(f"ANALYTICS ERROR: {analytics_err}")

        # Write to query history for my-usage page (best-effort)
        try:
            write_query_history(user_id, question, answer, scores, latency_ms)
        except Exception as hist_err:
            print(f"HISTORY ERROR: {hist_err}")

        # Update live token consumption counter (best-effort, never blocks the response)
        monthly_totals = {"consumed_input_tokens": None, "consumed_output_tokens": None}
        try:
            qa_in    = kb_result.get("qa_input_tokens", 0)
            qa_out   = kb_result.get("qa_output_tokens", 0)
            eval_in  = scores.get("eval_input_tokens", 0)
            eval_out = scores.get("eval_output_tokens", 0)
            total_input  = qa_in + eval_in
            total_output = qa_out + eval_out
            monthly_totals = update_budget_consumption(total_input, total_output, user_id)
        except Exception as budget_err:
            print(f"BUDGET ERROR: {budget_err}")

        return cors_response(200, {
            "question":         question,
            "answer":           answer,
            "retrieved_chunks": chunks,
            "evaluation":       scores,
            "latency_ms":       latency_ms,
            "monthly_input_tokens":  monthly_totals.get("consumed_input_tokens"),
            "monthly_output_tokens": monthly_totals.get("consumed_output_tokens")
        })

    except Exception as e:
        print(f"ERROR: {e}")
        return cors_response(500, {"error": str(e)})


def cors_response(status_code: int, body: dict) -> dict:
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type":                "application/json",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods":"GET, POST, OPTIONS",
            "Access-Control-Allow-Headers":"Content-Type, Authorization"
        },
        "body": json.dumps(body)
    }
