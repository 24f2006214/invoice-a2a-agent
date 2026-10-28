# Invoice A2A Agent

Small FastAPI implementation for the invoice-agent assignment.

## Run locally

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export INVOICE_AGENT_TOKEN='replace-with-a-long-secret'
uvicorn main:app --reload --port 8000
```

Check the card at `http://127.0.0.1:8000/.well-known/agent-card.json`.

## Render deployment

Push this repository to GitHub. In Render, create a new Web Service from the repository. Render can use `render.yaml`; otherwise use:

- Build command: `pip install -r requirements.txt`
- Start command: `uvicorn main:app --host 0.0.0.0 --port $PORT`
- Environment variable: `INVOICE_AGENT_TOKEN` = a long random secret

The final URL to submit is the A2A base URL ending in `/a2a/`, for example `https://your-service.onrender.com/a2a/`.

## Important limitation

This starter includes a deterministic evidence/action parser so it can run without an AI API key. Before the final grading Save, test it against the assignment's real check batches. If the grader's document fields require deeper semantic interpretation, the decision function in `choose_action` is the place to connect an AI provider or improve the evidence extraction rules.
