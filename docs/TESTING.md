# Testing & Verification

Two end-to-end scenarios for verifying the proxy against a real Metaview account.

Related: [Architecture](ARCHITECTURE.md) · [Deployment](DEPLOYMENT.md) · [Security](SECURITY.md)

> [!IMPORTANT]
> The interactive browser console described below is **local-development only** and is
> not mounted unless `ENABLE_TEST_CONSOLE=true`. It serves unauthenticated endpoints
> that hand out a live user's proxy access token. Never enable it on a deployed
> service. See [Security](SECURITY.md#test-console).

## Running the unit tests

```bash
pip install -r requirements.txt
TESTING=true python -m pytest tests/ -q
```

## Enabling the console locally

```bash
export ENABLE_TEST_CONSOLE=true
export STORAGE_BACKEND=memory
uvicorn app.main:app --reload --port 8080
# then open http://localhost:8080/test
```

---

## 5. Testing & Verification Guide

### 5.1 Interactive Web Test Console (Local & Cloud Run)

The proxy includes a built-in interactive web console allowing you to test the entire OAuth and MCP handshake in a single visual interface before connecting Gemini Enterprise:

1. **Console URLs:**
   - **Local Environment:**
     - Start Flow: [http://localhost:8080/test](http://localhost:8080/test)
     - Active Console: [http://localhost:8080/test-callback](http://localhost:8080/test-callback)
   - **Google Cloud Run (Production Deployment):**
     - Start Flow: `https://<YOUR_CLOUD_RUN_URL>/test`
     - Active Console: `https://<YOUR_CLOUD_RUN_URL>/test-callback`

2. **One-Click Test Actions:**
   - `📋 1. Discover Tools (tools/list)`: Verifies upstream SSE streaming and returns all 51 live Metaview tools.
   - `👤 2. Get User Context (get_user_context)`: Confirms personal user identity, role, and workspace name.
   - `🔍 3. Sample Question (search_conversations)`: Executes a live conversation query against the user's Metaview data boundary.

---

### 5.2 Scenario A: Upstream MCP Tool Verification (Live Query)

To verify end-to-end tool execution against the upstream MCP server:

1. **Test with Existing Account Data:**
   - Log into your upstream service account (e.g. Metaview, Greenhouse, Carta).
   - Ensure at least one search query or item exists in your profile.

2. **Sample Verification Questions in Gemini Enterprise:**
   Open Gemini Enterprise chat and try asking prompts relevant to your upstream data:

   | Category | Sample Prompt for Gemini Enterprise |
   | :--- | :--- |
   | **Context Discovery** | *"What tools and context do you have access to from my connected account?"* |
   | **Recent Data** | *"Summarize my recent conversations and meeting notes."* |
   | **Specific Search** | *"Search my account records for updates related to project roadmap."* |

3. **Direct CLI / cURL Verification:**
   You can also test the proxy's `search_conversations` MCP tool directly from your terminal:
   ```bash
   curl -X POST "https://<YOUR_CLOUD_RUN_URL>/mcp" \
     -H "Authorization: Bearer <YOUR_PROXY_TOKEN>" \
     -H "Content-Type: application/json" \
     -H "Accept: application/json, text/event-stream" \
     -d '{
       "jsonrpc": "2.0",
       "id": "test-search-interview",
       "method": "tools/call",
       "params": {
         "name": "search_conversations",
         "arguments": {
           "rationale": "Retrieve Alex Rivera interview transcript and AI summary",
           "query": "Alex Rivera"
         }
       }
     }'
   ```

4. **Behind-the-Scenes MCP Mechanics:**
   - Gemini Enterprise calls `search_conversations(rationale="Retrieve Alex Rivera interview summary", query="Alex Rivera")`.
   - The proxy injects your personal Metaview OAuth token and strips browser headers.
   - Metaview returns the conversation record, AI notes, and timestamped highlights.
   - Gemini Enterprise synthesizes a structured evaluation card quoting specific moments from the interview!

---

### 5.3 Scenario B: AI Sourcing & Talent Pipeline Testing (Zero Audio Files Needed)

If you prefer testing without uploading any media files, you can test Metaview's **AI Sourcing Agent** immediately:

#### How to Test:
1. **Create an AI Sourcing Search in Metaview:**
   - Log into **[my.metaview.app](https://my.metaview.app)**.
   - In the sidebar, click on **Sourcing** (or **Search**).
   - Click **New Search** and enter a role description, for example:  
     *`Senior Python Engineer in London with Kubernetes and distributed systems experience`*
   - Metaview's AI will automatically draft an **Ideal Candidate Profile (ICP)** with required skills and populate your search with matched candidate profiles.

2. **Sample Questions to Ask Gemini Enterprise:**
   In Gemini Enterprise chat, try these candidate sourcing and pipeline prompts:

   | Category | Sample Prompt for Gemini Enterprise |
   | :--- | :--- |
   | **Pipeline Discovery** | *"What active candidate sourcing searches do I have in Metaview?"* |
   | **Top Candidate Review** | *"Show me the top 3 candidate profiles surfaced in my Senior Python Engineer sourcing search."* |
   | **ICP Criteria Analysis** | *"What are the mandatory requirements and skills in our Ideal Candidate Profile (ICP) for the London Python role?"* |
   | **Candidate Backgrounds** | *"Summarize the previous employers and technical achievements of candidates found in my sourcing search."* |
   | **Pipeline Health** | *"Give me a breakdown of candidate stages, match feedback, and outreach status in our sourcing pipeline."* |

3. **Direct CLI / cURL Verification:**
   You can verify candidate search discovery directly through the proxy:
   ```bash
   curl -X POST "https://<YOUR_CLOUD_RUN_URL>/mcp" \
     -H "Authorization: Bearer <YOUR_PROXY_TOKEN>" \
     -H "Content-Type: application/json" \
     -H "Accept: application/json, text/event-stream" \
     -d '{
       "jsonrpc": "2.0",
       "id": "test-list-sourcing",
       "method": "tools/call",
       "params": {
         "name": "list_sourcing_searches",
         "arguments": {
           "rationale": "Discover active sourcing searches for recruitment dashboard"
         }
       }
     }'
   ```

4. **Behind-the-Scenes MCP Mechanics:**
   - Gemini Enterprise calls:
     - `list_sourcing_searches`: Discovers active sourcing campaigns and IDs.
     - `get_search_details`: Retrieves the Ideal Candidate Profile (ICP) version and criteria.
     - `list_sourcing_candidates`: Pulls surfaced candidate cards and match scores.
     - `fetch_candidates`: Retrieves candidate employment history and technical backgrounds.
   - Because all calls pass through your per-user proxy token, Metaview enforces that only searches and candidate pipelines you created or have access to are visible!

---
