# Atlas Backend

FastAPI service that orchestrates channel-based agents and writes their responses to Supabase.

## What This Service Does

`main.py` listens for message webhooks, finds agents assigned to the channel, runs each agent, and stores generated replies.

High-level flow:

1. Receives `POST /webhook/messages` from Supabase.
2. Validates webhook auth using `WEBHOOK_SECRET`.
3. Ignores messages already created by agents (`agent_id` present) to avoid loops.
4. Loads channel agents from Supabase table `channel_agents` (with joined `agents` data).
5. For each agent:
   - `WEBHOOK` type: calls external webhook URL with message payload.
   - Other types: runs hosted LLM call via OpenRouter using OpenAI SDK.
6. Saves each agent reply to Supabase table `messages` with:
   - `status = PENDING` when `requires_approval = true`
   - `status = APPROVED` otherwise

## Tech Stack

- Python + FastAPI
- Supabase Python client
- OpenAI Python SDK (configured to use OpenRouter base URL)
- Requests (for external webhook agents)
- python-dotenv

## Project Structure

- `main.py` - API app and orchestration logic
- `requirements.txt` - Python dependencies

## Required Environment Variables

Create a `.env` file with:

- `SUPABASE_URL`
- `SUPABASE_SERVICE_KEY`
- `SUPABASE_ANON_KEY` (currently loaded but not used by active client init)
- `WEBHOOK_SECRET`
- `OPENROUTER_API_KEY`

## API Endpoints

- `GET /`
  - Health check
  - Returns local running status

- `POST /webhook/messages`
  - Triggered by Supabase webhook
  - Requires header:
    - `Authorization: Bearer <WEBHOOK_SECRET>`
  - Processes message asynchronously using FastAPI background task

## Run Locally

1. Create and activate virtual environment.
2. Install dependencies:

```bash
pip install -r requirements.txt
```

3. Start server:

```bash
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

4. Health check:

```bash
curl http://localhost:8000/
```

## Supabase Tables Expected by Current Code

- `channel_agents`
  - needs `channel_id`, `agent_id`
  - queried with `agents(*)` relationship

- `agents`
  - expected fields include:
    - `id`, `name`, `type`
    - `system_prompt`
    - `webhook_url`, `webhook_headers`
    - `requires_approval`

- `messages`
  - inserts include:
    - `channel_id`, `content`, `agent_id`
    - `status`, `is_processed`

## Notes

- The service currently uses synchronous HTTP (`requests`) inside agent processing.
- Background tasks are in-process (not a persistent queue).
- For production scale, consider durable workers/queues, retries, and idempotency keys.


Need to change the `WEBHOOK_NOTIFY_URL` field in lovable -> cloud -> secrets
to point the backend to local or to the render
local WEBHOOK_NOTIFY_URL: https://flexibly-winish-zulema.ngrok-free.dev/webhook/messages
