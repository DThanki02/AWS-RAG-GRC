# Project summary

## 1. What this project is

Policy Retriever is an internal corporate RAG (Retrieval-Augmented Generation) system built on AWS, allowing employees to ask natural-language questions about company policy documents stored in Google Drive. It includes real authentication, live evaluation of every AI response, cost/token budget tracking, and an admin-only analytics dashboard.

## 2. Architecture overview

```
Google Drive (source documents)
        │
        ▼
Amazon Bedrock Managed Knowledge Base (Google Drive connector, OAuth 2.0)
        │
        ▼
Query Lambda ──► Claude Sonnet 4.6 (answer generation)
        │    ──► Amazon Nova Lite (answer evaluation)
        │    ──► DynamoDB (live token budget counter)
        │    ──► S3 (session logs + structured analytics records)
        │
        ▼
API Gateway (HTTP API, Cognito JWT-authorized)
        │
        ▼
Frontend (S3 + CloudFront): index.html — chat UI with Cognito login

Separately:
S3 (analytics/date=YYYY-MM-DD/*.json)
        │
        ▼
AWS Glue (crawler → schema catalog)
        │
        ▼
Amazon Athena (SQL views: weekly/monthly interactions, top users, cost trend, overall score, out-of-scope activity)
        │
        ▼
Dashboard Lambda (runs Athena queries, caches result in DynamoDB, admin-group-gated)
        │
        ▼
dashboard.html (S3 + CloudFront) — admin-only usage analytics dashboard
```

## 3. Core components built

### Knowledge Base
- Amazon Bedrock **Managed Knowledge Base** with **Google Drive** as the data source
- Automatic sync using Amazon Eventbridge

### Query Lambda (`lambda_function.py`)
- Retrieves top-5 relevant chunks from the KB 
- Generates answers via **Claude Sonnet 4.6** 
- Corporate-safe response handling: never says "I cannot find the answer" bluntly but instead provides a polite HR-contact message with successful answers getting an HR/contact line appended automatically
- **Evaluation**: every answer scored by **Amazon Nova Lite** on three independent 0–10 metrics:
  - **Faithfulness** - is the answer grounded in retrieved context, or hallucinated?
  - **Answer Relevance** - did it actually address the question?
  - **Context Relevance** - did retrieval pull precise, relevant chunks, or noise?
  - Plus a computed **Overall** average, displayed as a percentage in the UI
- **Out-of-scope detection**: flags and tags questions the KB genuinely can't answer, so they can be excluded from quality metrics rather than skewing them
- **Logging**: writes a human-readable markdown transcript per session to S3, plus a structured JSON analytics record per interaction (partitioned by date for Athena)
- **Token/budget tracking**: atomically increments monthly input/output token counters in DynamoDB after every call, returned live in each response
- **Latency tracking**: total round-trip time returned with every response

### Frontend (`index.html`)
- Chat-style UI with a warm amber/serif theme
- **Real Cognito authentication** replaced an earlier placeholder login
- Displays the response, retrieved source chunks, evaluation scores that are collapsible, latency badges and live monthly input/output token usage bars
- Info dropdown explaining each evaluation metric and an overall monthly token budget progress bar
- Logout function using Cognito
- Current logged-in user shown in the header
- Shows the last time the KB was synced to the Google Drive

### Analytics pipeline
- **S3**: structured JSON interaction records, Hive-partitioned by date
- **Glue**: crawler catalogs the schema into a Data Catalog table, scheduled daily
- **Athena**: SQL views for weekly/monthly interaction counts, use-case breakdown, top users, cost trend, cumulative + weekly overall evaluation score, and out-of-scope query activity (so that abuse/off-topic queries don't distort the genuine quality metric)
- **Dashboard Lambda**: executes all Athena queries, caches results in DynamoDB (5-minute TTL) to avoid re-querying on every page load
- **QuickSight was considered but dropped** (requires payment setup even on trial) in favor of a fully custom, cost-free dashboard

### Dashboard (`dashboard.html`)
- Same Cognito login as the main app, **plus checks for admin group membership in Cognito** that is enforced both in the frontend and backend (Lambda rejects non-admins with 403), so it's a real security boundary, not just a UI hint
- Headline card: cumulative overall evaluation score (%), across all evaluated in-scope responses
- Charts + tables: weekly/monthly interactions, interactions by use case, top users, cost trend, overall score trend, out-of-scope query activity by user

## 4. Key technical challenges solved along the way

- Google Drive OAuth consent screen and syncing Google Drive to AWS Knowledge Bases
- Managed KB requiring `managedSearchConfiguration` instead of `vectorSearchConfiguration`
- CORS failures caused by missing `Authorization` header support in both API Gateway and the Lambda's own CORS response headers
- Column name collisions in Glue causing Athena to fail 
- Athena partition discovery lag (new day's data invisible until the Glue crawler runs)

## 5. Current state

A fully functioning, authenticated, evaluated, cost-tracked RAG system with a separate admin analytics dashboard that is built entirely on serverless AWS primitives (Lambda, S3, DynamoDB, Glue, Athena, Cognito, API Gateway, CloudFront) with no paid BI tool required.
