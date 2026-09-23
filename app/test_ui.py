"""Local development test console for the Metaview & Gemini Enterprise OAuth 2.0 MCP Proxy.

> SECURITY: These routes are for localhost development only and are NOT mounted
> unless `ENABLE_TEST_CONSOLE=true`. `/test/active-token` deliberately returns a live
> proxy access token for the most recent sign-in and performs no authentication, so
> exposing this router on a public endpoint would let any anonymous caller read that
> user's Metaview data. See app/main.py for the mounting logic.
"""

import logging

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse
from app.config import settings
from app.storage import get_storage, StorageBackend


logger = logging.getLogger(__name__)
router = APIRouter(tags=["Testing Console"])


def _require_console_enabled() -> None:
    """Refuse to serve console routes unless explicitly enabled.

    Defence in depth: app.main only mounts this router when the flag is set, but a
    future refactor could reintroduce it unconditionally.
    """
    if not settings.ENABLE_TEST_CONSOLE:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not Found")

TEST_PAGE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Metaview & Gemini Enterprise OAuth 2.0 Test Console</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
  <style>
    :root {
      --bg: #090d16;
      --card-bg: rgba(18, 24, 38, 0.75);
      --card-border: rgba(99, 102, 241, 0.2);
      --text: #f1f5f9;
      --text-muted: #94a3b8;
      --primary: #6366f1;
      --primary-glow: rgba(99, 102, 241, 0.4);
      --accent: #06b6d4;
      --success: #10b981;
      --warning: #f59e0b;
      --code-bg: #030712;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: 'Outfit', -apple-system, sans-serif;
      background-color: var(--bg);
      background-image: 
        radial-gradient(circle at 20% 15%, rgba(99, 102, 241, 0.15) 0%, transparent 40%),
        radial-gradient(circle at 80% 85%, rgba(6, 182, 212, 0.15) 0%, transparent 40%);
      color: var(--text);
      min-height: 100vh;
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      padding: 2rem 1rem;
    }
    .container {
      max-width: 780px;
      width: 100%;
    }
    .card {
      background: var(--card-bg);
      backdrop-filter: blur(16px);
      -webkit-backdrop-filter: blur(16px);
      border: 1px solid var(--card-border);
      border-radius: 20px;
      padding: 2.5rem;
      box-shadow: 0 25px 50px -12px rgba(0, 0, 0, 0.5), 0 0 40px -10px var(--primary-glow);
    }
    .badge {
      display: inline-flex;
      align-items: center;
      gap: 0.5rem;
      background: rgba(99, 102, 241, 0.15);
      border: 1px solid rgba(99, 102, 241, 0.3);
      color: #a5b4fc;
      font-size: 0.82rem;
      font-weight: 500;
      padding: 0.35rem 0.85rem;
      border-radius: 9999px;
      margin-bottom: 1.25rem;
      letter-spacing: 0.02em;
    }
    .badge::before {
      content: '';
      width: 7px;
      height: 7px;
      border-radius: 50%;
      background: var(--accent);
      box-shadow: 0 0 8px var(--accent);
    }
    h1 {
      font-size: 2rem;
      font-weight: 700;
      letter-spacing: -0.02em;
      margin-bottom: 0.75rem;
      background: linear-gradient(135deg, #ffffff 0%, #cbd5e1 50%, #818cf8 100%);
      -webkit-background-clip: text;
      -webkit-text-fill-color: transparent;
    }
    p.lead {
      color: var(--text-muted);
      font-size: 1.05rem;
      line-height: 1.6;
      margin-bottom: 2rem;
    }
    .specs {
      background: rgba(3, 7, 18, 0.6);
      border: 1px solid rgba(255, 255, 255, 0.06);
      border-radius: 12px;
      padding: 1.25rem;
      margin-bottom: 2rem;
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
      gap: 1rem;
    }
    .spec-item {
      display: flex;
      flex-direction: column;
      gap: 0.25rem;
    }
    .spec-label {
      font-size: 0.75rem;
      text-transform: uppercase;
      letter-spacing: 0.05em;
      color: var(--text-muted);
    }
    .spec-value {
      font-family: 'JetBrains Mono', monospace;
      font-size: 0.88rem;
      color: #e2e8f0;
      word-break: break-all;
    }
    .btn {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 0.6rem;
      background: linear-gradient(135deg, #4f46e5 0%, #06b6d4 100%);
      color: white;
      font-weight: 600;
      font-size: 1.05rem;
      padding: 0.9rem 1.8rem;
      border-radius: 12px;
      text-decoration: none;
      transition: all 0.2s ease;
      box-shadow: 0 10px 25px -5px rgba(79, 70, 229, 0.4);
      width: 100%;
      cursor: pointer;
      border: none;
    }
    .btn:hover {
      transform: translateY(-2px);
      box-shadow: 0 15px 30px -5px rgba(79, 70, 229, 0.6);
    }
    .footer-note {
      text-align: center;
      color: var(--text-muted);
      font-size: 0.85rem;
      margin-top: 1.5rem;
    }
  </style>
</head>
<body>
  <div class="container">
    <div class="card">
      <div class="badge">Identity Broker Proxy &bull; Local Sandbox</div>
      <h1>Metaview &amp; Gemini Enterprise</h1>
      <p class="lead">Test end-to-end 3-legged OAuth 2.0 authentication and live Model Context Protocol (MCP) tool execution with your Metaview account.</p>
      
      <div class="specs">
        <div class="spec-item">
          <span class="spec-label">Proxy Endpoint</span>
          <span class="spec-value">__PROXY_BASE_URL__</span>
        </div>
        <div class="spec-item">
          <span class="spec-label">Test User ID</span>
          <span class="spec-value">user@example.com</span>
        </div>
        <div class="spec-item">
          <span class="spec-label">Storage Backend</span>
          <span class="spec-value">GCP Secret Manager (__GCP_PROJECT_ID__)</span>
        </div>
        <div class="spec-item">
          <span class="spec-label">Target MCP Upstream</span>
          <span class="spec-value">https://mcp.metaview.ai/mcp</span>
        </div>
      </div>

      <a href="__AUTH_LINK__" class="btn">
        <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <path d="M15 3h4a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2h-4"></path>
          <polyline points="10 17 15 12 10 7"></polyline>
          <line x1="15" y1="12" x2="3" y2="12"></line>
        </svg>
        Authenticate with Metaview (OAuth 2.0)
      </a>

      <p class="footer-note">Redirects to Metaview sign-in &rarr; Captures Authorization Code &rarr; Mints Proxy Bearer Token &rarr; Calls MCP.</p>
    </div>
  </div>
</body>
</html>
"""

CALLBACK_PAGE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>OAuth Callback &amp; MCP Testing Console</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
  <style>
    :root {
      --bg: #090d16;
      --card-bg: rgba(18, 24, 38, 0.8);
      --card-border: rgba(99, 102, 241, 0.25);
      --text: #f1f5f9;
      --text-muted: #94a3b8;
      --primary: #6366f1;
      --accent: #06b6d4;
      --success: #10b981;
      --error: #ef4444;
      --code-bg: #030712;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: 'Outfit', -apple-system, sans-serif;
      background-color: var(--bg);
      background-image: 
        radial-gradient(circle at 15% 15%, rgba(16, 185, 129, 0.12) 0%, transparent 45%),
        radial-gradient(circle at 85% 85%, rgba(99, 102, 241, 0.12) 0%, transparent 45%);
      color: var(--text);
      min-height: 100vh;
      padding: 2.5rem 1rem;
      display: flex;
      flex-direction: column;
      align-items: center;
    }
    .container {
      max-width: 860px;
      width: 100%;
    }
    .header {
      margin-bottom: 2rem;
      display: flex;
      align-items: center;
      justify-content: space-between;
    }
    .status-pill {
      display: inline-flex;
      align-items: center;
      gap: 0.5rem;
      background: rgba(16, 185, 129, 0.15);
      border: 1px solid rgba(16, 185, 129, 0.3);
      color: #6ee7b7;
      font-size: 0.85rem;
      font-weight: 500;
      padding: 0.35rem 0.9rem;
      border-radius: 9999px;
    }
    .status-dot {
      width: 8px;
      height: 8px;
      border-radius: 50%;
      background: var(--success);
      box-shadow: 0 0 10px var(--success);
    }
    .step-card {
      background: var(--card-bg);
      backdrop-filter: blur(14px);
      -webkit-backdrop-filter: blur(14px);
      border: 1px solid var(--card-border);
      border-radius: 16px;
      padding: 1.75rem;
      margin-bottom: 1.5rem;
      box-shadow: 0 10px 30px -5px rgba(0, 0, 0, 0.4);
    }
    .step-title {
      font-size: 1.15rem;
      font-weight: 600;
      margin-bottom: 0.75rem;
      display: flex;
      align-items: center;
      gap: 0.6rem;
    }
    .step-num {
      width: 26px;
      height: 26px;
      border-radius: 50%;
      background: var(--primary);
      color: white;
      font-size: 0.82rem;
      display: flex;
      align-items: center;
      justify-content: center;
      font-weight: 700;
    }
    .code-box {
      background: var(--code-bg);
      border: 1px solid rgba(255, 255, 255, 0.08);
      border-radius: 10px;
      padding: 1rem;
      font-family: 'JetBrains Mono', monospace;
      font-size: 0.84rem;
      color: #38bdf8;
      overflow-x: auto;
      white-space: pre-wrap;
      word-break: break-all;
      margin-top: 0.5rem;
    }
    .btn-group {
      display: flex;
      gap: 0.75rem;
      flex-wrap: wrap;
      margin-top: 1rem;
    }
    .btn {
      display: inline-flex;
      align-items: center;
      gap: 0.5rem;
      background: var(--primary);
      color: white;
      border: none;
      font-weight: 500;
      font-size: 0.92rem;
      padding: 0.65rem 1.25rem;
      border-radius: 10px;
      cursor: pointer;
      transition: all 0.15s ease;
    }
    .btn:hover {
      background: #4f46e5;
      transform: translateY(-1px);
    }
    .btn.accent {
      background: linear-gradient(135deg, #06b6d4 0%, #3b82f6 100%);
    }
    .btn.secondary {
      background: rgba(255, 255, 255, 0.1);
      border: 1px solid rgba(255, 255, 255, 0.15);
    }
    .btn.secondary:hover {
      background: rgba(255, 255, 255, 0.18);
    }
    .loader {
      display: inline-block;
      width: 14px;
      height: 14px;
      border: 2px solid rgba(255,255,255,0.3);
      border-radius: 50%;
      border-top-color: white;
      animation: spin 0.8s ease-in-out infinite;
    }
    @keyframes spin { to { transform: rotate(360deg); } }
    .response-area {
      margin-top: 1.25rem;
      display: none;
    }
    .response-area.visible {
      display: block;
    }
  </style>
</head>
<body>
  <div class="container">
    <div class="header">
      <div>
        <h1 style="font-size: 1.6rem; font-weight: 700;">OAuth 2.0 Testing Console</h1>
        <p style="color: var(--text-muted); font-size: 0.95rem; margin-top: 0.2rem;">Connected to Metaview with authenticated user session</p>
      </div>
      <div style="display: flex; align-items: center; gap: 0.75rem;">
        <a href="__AUTH_LINK__" class="btn secondary" style="text-decoration: none; padding: 0.4rem 0.8rem; font-size: 0.85rem;">🔑 New Sign-In</a>
        <div class="status-pill">
          <span class="status-dot"></span>
          <span>Active</span>
        </div>
      </div>
    </div>

    <!-- Step 1: Authorization Code Received -->
    <div class="step-card">
      <div class="step-title">
        <span class="step-num">1</span>
        <span>Proxy Authorization Code</span>
      </div>
      <p style="color: var(--text-muted); font-size: 0.9rem;">Metaview tokens are secured in Google Cloud Secret Manager (__GCP_PROJECT_ID__). Authorization code / session status:</p>
      <div class="code-box" id="authCodeBox">Checking OAuth session status...</div>
    </div>

    <!-- Step 2: Token Exchange -->
    <div class="step-card">
      <div class="step-title">
        <span class="step-num">2</span>
        <span>Gemini Enterprise Proxy Token</span>
      </div>
      <p style="color: var(--text-muted); font-size: 0.9rem;">Simulate Gemini Enterprise backend calling <code>POST /oauth/token</code> with client credentials:</p>
      <div class="btn-group">
        <button id="exchangeBtn" class="btn" onclick="exchangeToken()">
          <span>Exchange Code for Bearer Token</span>
        </button>
      </div>
      <div id="tokenBox" class="code-box" style="display: none;"></div>
    </div>

    <!-- Step 3: MCP Query Testing -->
    <div class="step-card" id="mcpCard" style="opacity: 0.5; pointer-events: none;">
      <div class="step-title">
        <span class="step-num">3</span>
        <span>Query Metaview MCP Server via Proxy</span>
      </div>
      <p style="color: var(--text-muted); font-size: 0.9rem;">Issue live JSON-RPC requests to <code>POST /mcp</code>. The proxy swaps your session token for the live Metaview OAuth token:</p>
      
      <div class="btn-group">
        <button class="btn accent" onclick="runMcpRequest('tools/list', {})">
          <span>📋 1. Discover Tools (tools/list)</span>
        </button>
        <button class="btn" style="background: #10b981;" onclick="runGetUserContext()">
          <span>👤 2. Get User Context (get_user_context)</span>
        </button>
        <button class="btn secondary" onclick="runSampleSearch()">
          <span>🔍 3. Sample Question (Search Interviews)</span>
        </button>
      </div>

      <div id="mcpResponseArea" class="response-area">
        <div style="font-size: 0.8rem; color: var(--text-muted); margin-bottom: 0.4rem; display: flex; justify-content: space-between;">
          <span id="responseStatusLabel">MCP Upstream Response</span>
          <span id="responseTimeLabel"></span>
        </div>
        <div class="code-box" id="mcpOutputBox" style="max-height: 420px; overflow-y: auto;"></div>
      </div>
    </div>
  </div>

  <script>
    console.log('[TestConsole] Script started.');
    const urlParams = new URLSearchParams(window.location.search);
    const authCode = urlParams.get('code');
    const state = urlParams.get('state');
    const authError = urlParams.get('error');
    const authErrorDesc = urlParams.get('error_description');

    let proxyAccessToken = null;

    function setMcpCardActive(active) {
      const mcpCard = document.getElementById('mcpCard');
      if (!mcpCard) return;
      if (active) {
        mcpCard.style.opacity = '1';
        mcpCard.style.pointerEvents = 'auto';
      } else {
        mcpCard.style.opacity = '0.5';
        mcpCard.style.pointerEvents = 'none';
      }
    }

    async function loadActiveSession() {
      const authBox = document.getElementById('authCodeBox');
      authBox.textContent = 'Checking for active OAuth session in GCP Secret Manager...';
      authBox.style.color = '#38bdf8';
      try {
        const resp = await fetch('/test/active-token');
        const data = await resp.json();
        if (data.active && data.proxy_access_token) {
          proxyAccessToken = data.proxy_access_token;
          authBox.textContent = '✓ Active session loaded from GCP Secret Manager - Proxy Token: ' + proxyAccessToken.substring(0, 24) + '...';
          authBox.style.color = '#38bdf8';

          const btn = document.getElementById('exchangeBtn');
          btn.innerHTML = '✓ Active Session Connected';
          btn.style.background = '#10b981';
          btn.disabled = true;

          const box = document.getElementById('tokenBox');
          box.style.display = 'block';
          box.textContent = JSON.stringify({
            access_token: proxyAccessToken,
            token_type: 'Bearer',
            storage: 'GCP Secret Manager',
            active: true
          }, null, 2);

          setMcpCardActive(true);
          runMcpRequest('tools/list', {});
        } else {
          authBox.textContent = 'No active session found. Click "New Sign-In" above to authenticate with Metaview.';
          authBox.style.color = '#fbbf24';
        }
      } catch (err) {
        authBox.textContent = 'Error loading session: ' + err.toString();
        authBox.style.color = '#ef4444';
      }
    }

    async function exchangeToken() {
      if (!authCode) {
        await loadActiveSession();
        return;
      }
      const btn = document.getElementById('exchangeBtn');
      btn.innerHTML = '<span class="loader"></span> Exchanging Code for Bearer Token...';
      btn.disabled = true;

      const formData = new URLSearchParams();
      formData.append('code', authCode);

      try {
        // Exchanged server-side so the client secret is never sent to the browser.
        const resp = await fetch('/test/exchange', {
          method: 'POST',
          headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
          body: formData
        });
        const data = await resp.json();
        const box = document.getElementById('tokenBox');
        box.style.display = 'block';

        if (resp.ok) {
          proxyAccessToken = data.access_token;
          btn.innerHTML = '✓ Token Minted Successfully';
          btn.style.background = '#10b981';
          box.textContent = JSON.stringify(data, null, 2);
          
          setMcpCardActive(true);
          runMcpRequest('tools/list', {});
        } else {
          btn.innerHTML = 'Code Already Used / Expired';
          btn.style.background = '#f59e0b';
          box.textContent = JSON.stringify(data, null, 2);
          await loadActiveSession();
        }
      } catch (err) {
        btn.innerHTML = 'Network Error';
        document.getElementById('tokenBox').textContent = err.toString();
        await loadActiveSession();
      }
    }

    async function initPage() {
      if (authError) {
        document.getElementById('authCodeBox').textContent = 'Auth Error: ' + authError + ' - ' + (authErrorDesc || '');
        document.getElementById('authCodeBox').style.color = '#ef4444';
        return;
      }

      if (authCode) {
        document.getElementById('authCodeBox').textContent = 'Proxy Auth Code: ' + authCode + ' (state: ' + (state || 'none') + ')';
        await exchangeToken();
        return;
      }

      await loadActiveSession();
    }

    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', initPage);
    } else {
      initPage();
    }

    async function runMcpRequest(method, params) {
      if (!proxyAccessToken) {
        alert('Please mint or load a proxy access token first!');
        return;
      }
      const area = document.getElementById('mcpResponseArea');
      const box = document.getElementById('mcpOutputBox');
      const statusLabel = document.getElementById('responseStatusLabel');
      const timeLabel = document.getElementById('responseTimeLabel');

      area.classList.add('visible');
      statusLabel.textContent = 'Calling Metaview MCP method: ' + method + '...';
      box.textContent = 'Contacting upstream https://mcp.metaview.ai/mcp...';
      timeLabel.textContent = '';

      const t0 = performance.now();
      const payload = {
        jsonrpc: '2.0',
        id: Date.now(),
        method: method,
        params: params
      };

      try {
        const resp = await fetch('/mcp', {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'Accept': 'application/json, text/event-stream',
            'Authorization': 'Bearer ' + proxyAccessToken
          },
          body: JSON.stringify(payload)
        });
        const elapsed = Math.round(performance.now() - t0);
        timeLabel.textContent = elapsed + ' ms';
        const rawText = await resp.text();
        let data = null;
        try {
          const lines = rawText.split('\\n');
          for (let i = lines.length - 1; i >= 0; i--) {
            const line = lines[i].trim();
            if (line.startsWith('data: ')) {
              const parsed = JSON.parse(line.substring(6));
              if (parsed.result || parsed.error || !data) {
                data = parsed;
              }
            }
          }
          if (!data) {
            data = JSON.parse(rawText);
          }
        } catch (e) {
          data = rawText;
        }

        if (resp.ok) {
          statusLabel.textContent = 'HTTP ' + resp.status + ' OK • JSON-RPC Response:';
          box.style.color = '#38bdf8';
          box.textContent = typeof data === 'object' ? JSON.stringify(data, null, 2) : data;
        } else {
          statusLabel.textContent = 'HTTP ' + resp.status + ' Error:';
          box.style.color = '#ef4444';
          box.textContent = typeof data === 'object' ? JSON.stringify(data, null, 2) : data;
        }
      } catch (err) {
        statusLabel.textContent = 'Request Failed:';
        box.style.color = '#ef4444';
        box.textContent = err.toString();
      }
    }

    function runGetUserContext() {
      runMcpRequest('tools/call', {
        name: 'get_user_context',
        arguments: {
          rationale: 'Initial identity and workspace verification for Bruce'
        }
      });
    }

    function runSampleSearch() {
      runMcpRequest('tools/call', {
        name: 'search_conversations',
        arguments: {
          rationale: 'Search recent interviews and calls in workspace',
          limit: 10
        }
      });
    }
  </script>
</body>
</html>
"""


@router.post("/test/exchange")
async def test_exchange_code(request: Request):
    """Exchange an authorization code for a proxy token on the server side.

    The console used to embed GE_CLIENT_SECRET directly in the returned HTML so the
    browser could call /oauth/token itself. Doing the exchange here keeps the secret
    in the process and out of the page source.
    """
    _require_console_enabled()

    form = await request.form()
    code = form.get("code")
    if not code:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing code")

    token_url = f"{settings.PROXY_BASE_URL.rstrip('/')}/oauth/token"
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            token_url,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "client_id": settings.GE_CLIENT_ID,
                "client_secret": settings.GE_CLIENT_SECRET,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )

    try:
        payload = resp.json()
    except Exception:
        payload = {"error": "invalid_response", "error_description": resp.text[:500]}
    return JSONResponse(status_code=resp.status_code, content=payload)


@router.get("/test/active-token")
async def get_active_token(storage: StorageBackend = Depends(get_storage)):
    """Return the most recent proxy token so the console can resume a session.

    Unauthenticated by design and therefore development-only: it hands the caller a
    working credential for whoever signed in last.
    """
    _require_console_enabled()

    token_data = await storage.get_latest_user_token()
    if token_data:
        return {
            "active": True,
            "proxy_access_token": token_data.proxy_access_token,
            "created_at": token_data.created_at,
        }
    return {"active": False}


@router.get("/test", response_class=HTMLResponse)
async def test_console(request: Request):
    """Interactive landing page to start OAuth test flow."""
    _require_console_enabled()

    auth_link = (
        f"{settings.PROXY_BASE_URL.rstrip('/')}/oauth/authorize?"
        f"client_id={settings.GE_CLIENT_ID}&"
        f"redirect_uri={settings.PROXY_BASE_URL.rstrip('/')}/test-callback&"
        f"state=local-test-session&"
        f"response_type=code"
    )
    html = (
        TEST_PAGE_HTML
        .replace("__PROXY_BASE_URL__", settings.PROXY_BASE_URL)
        .replace("__GCP_PROJECT_ID__", settings.GCP_PROJECT_ID or "not-configured")
        .replace("__AUTH_LINK__", auth_link)
    )
    return HTMLResponse(content=html)


@router.get("/test-callback", response_class=HTMLResponse)
async def test_callback(request: Request):
    """Callback landing page that receives the auth code and allows interactive MCP testing."""
    _require_console_enabled()

    auth_link = (
        f"{settings.PROXY_BASE_URL.rstrip('/')}/oauth/authorize?"
        f"client_id={settings.GE_CLIENT_ID}&"
        f"redirect_uri={settings.PROXY_BASE_URL.rstrip('/')}/test-callback&"
        f"state=local-test-session&"
        f"response_type=code"
    )
    html = (
        CALLBACK_PAGE_HTML
        .replace("__AUTH_LINK__", auth_link)
        .replace("__GCP_PROJECT_ID__", settings.GCP_PROJECT_ID or "not-configured")
    )
    return HTMLResponse(content=html)


