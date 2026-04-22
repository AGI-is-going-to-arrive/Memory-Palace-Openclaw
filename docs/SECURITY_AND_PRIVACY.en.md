> [中文版](SECURITY_AND_PRIVACY.md)

# Memory Palace Security and Privacy Guide

<p align="center">
  <img src="images/write_guard_review_loop_en_4k.png" width="1100" />
</p>

This page is for users who deploy, maintain, or share Memory Palace.
It covers secret handling, API auth, Docker boundaries, and pre-publish checks.

---

## 1. What Must Stay Private

These values should stay in local `.env` files or protected deployment
environment variables. They should not be committed to git.

| Secret | Purpose | Example env key |
|---|---|---|
| `MCP_API_KEY` | maintenance / review / browse / SSE auth | `MCP_API_KEY` |
| embedding API key | embedding provider access | `RETRIEVAL_EMBEDDING_API_KEY` |
| reranker API key | reranker provider access | `RETRIEVAL_RERANKER_API_KEY` |
| write-guard LLM key | write guard LLM access | `WRITE_GUARD_LLM_API_KEY` |
| compact-gist LLM key | compact gist LLM access | `COMPACT_GIST_LLM_API_KEY` |
| intent LLM key | optional intent routing LLM | `INTENT_LLM_API_KEY` |

Use [`.env.example`](../.env.example) as the public template.

---

## 2. Recommended Rules

- commit `.env.example`, not `.env`
- use placeholders such as `<YOUR_API_KEY>` in documentation
- remove usernames, absolute local paths, and secrets from screenshots
- do not print auth headers or secret values into public logs
- rotate keys regularly
- prefer server-side auth injection in Docker deployments

Current code-grounded safety notes:

- runtime env loading ignores sensitive process-level overrides such as
  `PATH`, `PYTHONPATH`, `HOME`, and similar variables
- generated local env files are tightened to restrictive file permissions

---

## 3. API Authentication Boundary

When `MCP_API_KEY` is configured, these surfaces require auth:

| Surface | Scope |
|---|---|
| `/maintenance/*` | all requests |
| `/review/*` | all requests |
| `/browse/*` | all requests, including reads |
| `/sse` and `/messages` | MCP SSE transport |

Accepted auth styles:

```text
X-MCP-API-Key: <MCP_API_KEY>
```

or

```text
Authorization: Bearer <MCP_API_KEY>
```

The system follows a fail-closed default:

- correct key -> pass
- missing or wrong key -> reject
- insecure local override -> only loopback, never forwarded traffic

Important bootstrap boundary:

- `/bootstrap/*` is loopback-only
- if the backend already has `MCP_API_KEY`, bootstrap write actions must also
  carry that key

---

## 4. Frontend Runtime Key Injection

The frontend does not hard-code secrets at build time.
If you need browser-side auth injection, use runtime config:

```html
<script>
  window.__MEMORY_PALACE_RUNTIME__ = {
    maintenanceApiKey: "<YOUR_MCP_API_KEY>",
    maintenanceApiKeyMode: "header"
  };
</script>
```

Current boundary:

- this is a runtime-only convenience path
- local fallback key input in the dashboard is memory-only
- it is cleared on refresh / close

For Docker one-click deployments, prefer server-side proxy injection rather
than exposing the real key to the page.

---

## 5. Docker Security Notes

Current deployment defaults already include:

- non-root runtime for backend/frontend containers
- `no-new-privileges`
- persistent volume for data
- backend/frontend health checks
- startup ordering with backend health gating the frontend

That still does **not** remove your responsibility to:

- keep deployment secrets out of git
- protect reverse-proxy config
- verify exposed ports and auth headers

---

## 6. Pre-Publish Checklist

Before pushing to a public repository, do at least this:

```bash
bash scripts/pre_publish_check.sh
git status
git ls-files -ci --exclude-standard
git ls-files --others --exclude-standard
```

Also manually review:

- public screenshots and videos
- `.env.example`
- README / docs wording
- package metadata such as repository URLs

Public documentation should describe:

- what users must configure
- what is optional
- what is only for maintainers

It should **not** depend on local-only report files or private machine state.
