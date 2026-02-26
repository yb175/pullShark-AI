import os
import json
import requests
import sqlite3
from typing import TypedDict, List, Dict, Any, Optional
from datetime import datetime, timezone
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import google.generativeai as genai
import base64




# Try importing Supabase
try:
    from supabase import create_client, Client
    HAS_SUPABASE = True
except ImportError:
    HAS_SUPABASE = False
    print("⚠️ Supabase not installed. Using SQLite fallback.")

# Load environment variables
load_dotenv()

# ============================================================================
# CONFIGURATION
# ============================================================================

GITHUB_API_BASE = "https://api.github.com"
GITHUB_TOKEN = os.getenv('GITHUB_TOKEN')
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')

# Supabase config
SUPABASE_URL = os.getenv('SUPABASE_URL')
SUPABASE_KEY = os.getenv('SUPABASE_KEY')

# SQLite fallback
SQLITE_DB = 'pullshark.db'

# Test mode flag
TEST_MODE = os.getenv('TEST_MODE', 'false').lower() == 'true'

# Determine Database
USE_SUPABASE = HAS_SUPABASE and SUPABASE_URL and SUPABASE_KEY
DB_TYPE = 'supabase' if USE_SUPABASE else 'sqlite'



SAMPLE_BUGS = [
    {
        'issue_id': 'BUG-001',
        'issue_title': 'Payment processing race condition',
        'issue_description': 'Concurrent payment transactions causing duplicate charges during high load.',
        'repo': 'myorg/payment-service',
        'pattern': 'race_condition',
        'solution': 'Add transaction locks and idempotency keys'
    },
    {
        'issue_id': 'BUG-002',
        'issue_title': 'Auth token expiration bug',
        'issue_description': 'JWT token refresh logic fails on concurrent requests causing 401 errors.',
        'repo': 'myorg/auth-service',
        'pattern': 'token_management',
        'solution': 'Implement proper token TTL and grace period'
    },
    {
        'issue_id': 'BUG-003',
        'issue_title': 'SQL Injection in Search',
        'issue_description': 'Search endpoint does not sanitize inputs allowing SQL injection.',
        'repo': 'myorg/api-gateway',
        'pattern': 'security_bypass',
        'solution': 'Use parameterized queries'
    }
]

def semantic_search_bugs(query_text: str, k: int = 3) -> List[Dict]:
    """
    Simulates a semantic search. 
    In a production Pathway app, this would use pw.io.http to query a running vector index.
    """
    print(f"🔍 Searching knowledge base for: '{query_text[:50]}...'")
    
    keywords = set(query_text.lower().split())
    results = []
    
    # Simple relevance scoring for demo purposes
    for bug in SAMPLE_BUGS:
        score = 0
        text = (bug['issue_title'] + " " + bug['issue_description']).lower()
        
        for word in keywords:
            if len(word) > 4 and word in text:
                score += 1
        
        if score > 0:
            bug_copy = bug.copy()
            bug_copy['score'] = score
            results.append(bug_copy)
            
    results = sorted(results, key=lambda x: x['score'], reverse=True)[:k]
    return results

# ============================================================================
# DATABASE LAYER
# ============================================================================

class Database:
    def __init__(self):
        if USE_SUPABASE:
            self.client: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
            print("✅ DB: Connected to Supabase")
        else:
            self._init_sqlite()
            print("✅ DB: Using Local SQLite")
    
    def _init_sqlite(self):
        conn = sqlite3.connect(SQLITE_DB)
        c = conn.cursor()
        
        # Create tables
        c.execute('''CREATE TABLE IF NOT EXISTS historical_prs 
                     (id INTEGER PRIMARY KEY, repo TEXT, title TEXT, status TEXT, bugs_found INTEGER, created_at TIMESTAMP)''')
        c.execute('''CREATE TABLE IF NOT EXISTS documentation 
                     (id INTEGER PRIMARY KEY, repo TEXT, module TEXT, content TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS pr_analyses 
                     (id INTEGER PRIMARY KEY, pr_number INTEGER, repo TEXT, author TEXT, 
                      test_plan TEXT, status TEXT, risk_score INTEGER, bugs_found INTEGER, created_at TIMESTAMP)''')
        conn.commit()
        conn.close()
    
    def get_historical_data(self, repo: str) -> List[Dict]:
        if USE_SUPABASE:
            try:
                response = self.client.table('historical_prs').select('*').eq('repo', repo).execute()
                return response.data
            except:
                pass
        else:
            try:
                conn = sqlite3.connect(SQLITE_DB)
                c = conn.cursor()
                c.execute('SELECT title, status as outcome, bugs_found FROM historical_prs WHERE repo = ?', (repo,))
                rows = c.fetchall()
                conn.close()
                return [{'title': r[0], 'outcome': r[1], 'bugs_found': r[2]} for r in rows]
            except:
                pass
        
        # Fallback mock data
        return [
            {'title': 'Fix payment retry logic', 'outcome': 'merged', 'bugs_found': 2},
            {'title': 'Update dependencies', 'outcome': 'merged', 'bugs_found': 0}
        ]

    def log_analysis(self, record: Dict) -> bool:
        if USE_SUPABASE:
            try:
                self.client.table('pr_analyses').insert(record).execute()
                return True
            except Exception as e:
                print(f"❌ Supabase Error: {e}")
                return False
        else:
            try:
                conn = sqlite3.connect(SQLITE_DB)
                c = conn.cursor()
                c.execute('''INSERT INTO pr_analyses (pr_number, repo, author, test_plan, status, risk_score, bugs_found, created_at)
                             VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
                          (record['pr_number'], record['repo'], record['author'], json.dumps(record['test_plan']),
                           record['status'], record['risk_score'], record['pathway_bugs_found'], record['timestamp']))
                conn.commit()
                conn.close()
                return True
            except Exception as e:
                print(f"❌ SQLite Error: {e}")
                return False
    
    def get_all_analyses(self, limit: int = 50) -> List[Dict]:
        """Get recent PR analyses"""
        if USE_SUPABASE:
            try:
                response = self.client.table('pr_analyses').select('*').order('created_at', desc=True).limit(limit).execute()
                return response.data
            except:
                return []
        else:
            try:
                conn = sqlite3.connect(SQLITE_DB)
                c = conn.cursor()
                c.execute('SELECT * FROM pr_analyses ORDER BY created_at DESC LIMIT ?', (limit,))
                rows = c.fetchall()
                conn.close()
                return [{'id': r[0], 'pr_number': r[1], 'repo': r[2], 'author': r[3], 
                        'test_plan': r[4], 'status': r[5], 'risk_score': r[6], 
                        'bugs_found': r[7], 'created_at': r[8]} for r in rows]
            except:
                return []

db = Database()

# ============================================================================
# STATE MANAGEMENT
# ============================================================================

class PullSharkState(TypedDict):
    pr_number: int
    pr_title: str
    pr_description: str
    author: str
    repo: str
    diff_content: str
    risk_score: int
    timestamp: str
    similar_bugs: List[Dict]
    historical_prs: List[Dict]
    test_plan: Dict
    formatted_comment: str
    status: str

# ============================================================================
# WORKFLOW NODES
# ============================================================================

def get_github_headers():
    return {
        'Authorization': f'token {GITHUB_TOKEN}',
        'Accept': 'application/vnd.github.v3+json'
    }

def extract_pr_data_from_string(pr_str: str) -> PullSharkState:
    """
    Parse the compressed PR string coming from Node backend.
    Example compressed format:
        t: title
        a: author
        f[10]: file1,file2,...
        diff: "...."
    """

    lines = pr_str.split("\n")
    title = ""
    author = ""
    files = []
    diff = ""

    for line in lines:
        if line.startswith("t:"):
            title = line.replace("t:", "").strip()
        elif line.startswith("a:"):
            author = line.replace("a:", "").strip()
        elif line.startswith("f["):
            part = line.split("]:")[1]
            files = [f.strip() for f in part.split(",")]
        elif line.startswith("diff:"):
            diff = line.replace("diff:", "").strip()

    return {
        'pr_number': -1,      # irrelevant now
        'pr_title': title,
        'pr_description': "",
        'author': author,
        'repo': "",
        'diff_content': diff,
        'risk_score': 5,      # or calculate from diff
        'timestamp': datetime.now(timezone.utc).isoformat(),
        'similar_bugs': [],
        'historical_prs': [],
        'test_plan': {},
        'formatted_comment': '',
        'status': 'pending'
    }

def augment_context(state: PullSharkState) -> PullSharkState:
    print(f"\n🧠 [2/5] Retrieving Context (RAG)...")
    
    query = f"{state['pr_title']} {state['diff_content'][:200]}"
    state['similar_bugs'] = semantic_search_bugs(query)
    
    state['historical_prs'] = db.get_historical_data(state['repo'])
    
    print(f"   ✅ Found {len(state['similar_bugs'])} relevant past bugs")
    return state

def generate_test_plan(state: PullSharkState) -> PullSharkState:
    print(f"\n🤖 [3/5] Generating Test Plan with Gemini...")
    
    if not GEMINI_API_KEY:
        print("   ⚠️ No Gemini Key. Skipping LLM.")
        state['test_plan'] = {'error': 'No API Key'}
        return state

    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel('gemini-2.5-flash')
    
    prompt = f"""
    Act as a Senior QA Engineer. Create a JSON test plan for this Pull Request.
    
    PR Title: {state['pr_title']}
    Risk Score: {state['risk_score']}/10
    Diff Summary: {state['diff_content'][:500]}...
    
    Known Bugs in similar code:
    {json.dumps(state['similar_bugs'], indent=2)}
    
    Return ONLY valid JSON with these keys:
    - edge_cases (list of strings)
    - security_risks (list of strings)
    - recommended_tests (list of strings)
    - priority (High/Medium/Low)
    """
    
    try:
        response = model.generate_content(prompt, generation_config={'response_mime_type': 'application/json'})
        state['test_plan'] = json.loads(response.text)
        state['status'] = 'success'
    except Exception as e:
        print(f"❌ Gemini Error: {e}")
        state['test_plan'] = {"error": "Generation failed"}
        state['status'] = 'failed'
        
    return state

def post_comment(state: PullSharkState) -> PullSharkState:
    print(f"\n📝 [4/5] Formatting & Posting Comment...")
    
    plan = state.get('test_plan', {})
    if 'error' in plan:
        print("   ⚠️ Skipping comment due to generation error.")
        return state
        
    priority_emoji = "🔴" if plan.get('priority') == 'High' else "🟡"
    
    comment = f"""## 🦈 PullShark AI Analysis
    
**Risk Level**: {priority_emoji} {plan.get('priority', 'Unknown')}

### 🧪 Recommended Tests
{chr(10).join(f"- [ ] {t}" for t in plan.get('recommended_tests', []))}

### ⚠️ Edge Cases & Security
{chr(10).join(f"- {t}" for t in plan.get('edge_cases', []) + plan.get('security_risks', []))}

---
*Generated by PullShark using Gemini & Pathway*
    """
    state['formatted_comment'] = comment
    
    if not TEST_MODE and GITHUB_TOKEN:
        try:
            url = f"{GITHUB_API_BASE}/repos/{state['repo']}/issues/{state['pr_number']}/comments"
            res = requests.post(url, json={'body': comment}, headers=get_github_headers())
            res.raise_for_status()
            print("   ✅ Comment posted to GitHub")
        except Exception as e:
            print(f"   ❌ Failed to post comment: {e}")
    else:
        print("   ℹ️ (Dry Run) Comment not posted.")
        
    return state

def save_results(state: PullSharkState):
    print(f"\n💾 [5/5] Saving to Database...")
    
    record = {
        'pr_number': state['pr_number'],
        'repo': state['repo'],
        'author': state['author'],
        'test_plan': state['test_plan'],
        'status': state['status'],
        'risk_score': state['risk_score'],
        'pathway_bugs_found': len(state['similar_bugs']),
        'timestamp': state['timestamp']
    }
    
    db.log_analysis(record)
    print("✅ Workflow Complete!")

# ============================================================================
# FASTAPI APPLICATION
# ============================================================================

app = FastAPI(
    title="PullShark AI API",
    description="AI-powered PR analysis with Gemini & Pathway RAG",
    version="1.0.0"
)

# CORS Configuration
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Update in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ============================================================================
# PYDANTIC MODELS
# ============================================================================

class AnalyzePRRequest(BaseModel):
    pr: str   # compressed PR string you send


class AnalyzePRResponse(BaseModel):
    success: bool
    pr_number: int
    repo: str
    author: str
    risk_score: int
    status: str
    test_plan: Dict
    formatted_comment: str
    similar_bugs: List[Dict]
    timestamp: str

class HealthResponse(BaseModel):
    status: str
    database: str
    github_token_present: bool
    gemini_api_key_present: bool
    pathway_available: bool
    test_mode: bool

class HistoryResponse(BaseModel):
    success: bool
    count: int
    analyses: List[Dict]

# ============================================================================
# API ENDPOINTS
# ============================================================================

@app.get("/", tags=["Root"])
def read_root():
    return {
        "message": "🦈 PullShark AI API",
        "version": "1.0.0",
        "endpoints": {
            "health": "/health",
            "analyze": "/api/analyze",
            "history": "/api/history",
            "bugs": "/api/bugs/search"
        }
    }

@app.get("/health", response_model=HealthResponse, tags=["Health"])
def health_check():
    """Check API health and configuration status"""
    return HealthResponse(
        status="healthy",
        database=DB_TYPE,
        github_token_present=bool(GITHUB_TOKEN),
        gemini_api_key_present=bool(GEMINI_API_KEY),
        test_mode=TEST_MODE
    )

@app.post("/api/analyze", response_model=AnalyzePRResponse)
async def analyze_pr(request: AnalyzePRRequest, background_tasks: BackgroundTasks):

    try:
        print("🦈 PR RECEIVED FROM NODE BACKEND")

        # No GitHub fetching
        decoded = base64.b64decode(request.pr).decode("utf-8")
        state = extract_pr_data_from_string(decoded)

        # RAG + LLM
        state = augment_context(state)
        state = generate_test_plan(state)

        # Build comment for Node backend to post
        plan = state["test_plan"]
        if "error" not in plan:
            priority_emoji = "🔴" if plan.get('priority') == 'High' else "🟡"
            state["formatted_comment"] = f"""
## 🦈 PullShark AI Analysis

**Risk Level**: {priority_emoji} {plan.get('priority')}

### 🧪 Recommended Tests
{chr(10).join(f"- [ ] {t}" for t in plan.get('recommended_tests', []))}

### ⚠️ Edge Cases & Security
{chr(10).join(f"- {t}" for t in plan.get('edge_cases', []) + plan.get('security_risks', []))}

---
*Generated by PullShark AI*
            """

        return AnalyzePRResponse(
            success=True,
            pr_number=-1,
            repo="unknown",
            author=state["author"],
            risk_score=state["risk_score"],
            status=state["status"],
            test_plan=state["test_plan"],
            formatted_comment=state["formatted_comment"],
            similar_bugs=state["similar_bugs"],
            timestamp=state["timestamp"]
        )

    except Exception as e:
        raise HTTPException(500, str(e))

@app.get("/api/history", response_model=HistoryResponse, tags=["History"])
def get_analysis_history(limit: int = 50):
    """Get recent PR analysis history"""
    try:
        analyses = db.get_all_analyses(limit)
        return HistoryResponse(
            success=True,
            count=len(analyses),
            analyses=analyses
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/bugs/search", tags=["RAG"])
def search_bugs(query: str, k: int = 3):
    """
    Search bug knowledge base using semantic search
    
    - query: Search query text
    - k: Number of results to return (default: 3)
    """
    try:
        results = semantic_search_bugs(query, k)
        return {
            "success": True,
            "query": query,
            "count": len(results),
            "results": results
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/historical/{repo:path}", tags=["History"])
def get_repo_history(repo: str):
    """Get historical PR data for a specific repository"""
    try:
        history = db.get_historical_data(repo)
        return {
            "success": True,
            "repo": repo,
            "count": len(history),
            "data": history
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ============================================================================
# MAIN ENTRY POINT
# ============================================================================

if __name__ == "__main__":
    import uvicorn
    print(f"🦈 PullShark AI Backend Starting...")
    print(f"   Database: {DB_TYPE}")
    print(f"   Test Mode: {TEST_MODE}")
    uvicorn.run(app, host="0.0.0.0", port=8000)
