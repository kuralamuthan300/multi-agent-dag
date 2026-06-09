# Multi-Agent DAG — Growing-Graph Orchestrator

A **multi-agent orchestration system** where each user query spawns a dynamic, growing DAG (Directed Acyclic Graph) of specialised AI skills. The graph is not pre-defined — it expands at runtime through five distinct mechanisms, enabling parallel research, automatic compression, code execution, critic evaluation, and error recovery without hard-coded workflows.

---

## Table of Contents

- [Architecture Overview](#architecture-overview)
- [Skill Catalog](#skill-catalog)
- [How the DAG Grows at Runtime](#how-the-dag-grows-at-runtime)
- [Token Miser — Automatic Compression](#token-miser--automatic-compression)
- [Memory System](#memory-system)
- [Error Recovery](#error-recovery)
- [Project Structure](#project-structure)
- [Gateway Integration](#gateway-integration)
- [Getting Started](#getting-started)
- [Batch Run Results](#batch-run-results)

---

## Architecture Overview

The agent's execution loop is a **NetworkX DiGraph** (directed graph). Each node represents one skill invocation; edges carry typed `AgentResult` payloads between skills. The orchestrator (`flow.py`) runs the graph in topological order, dispatching ready nodes concurrently via `asyncio.gather`.

### Key Design Principles

- **No hard-coded workflows.** The Planner decomposes each query into a seed DAG, then the graph grows organically as skills emit dynamic successors.
- **Tool-blindness contract.** Skills name other skills in their successors — never tools. The Planner names skills, not MCP endpoints.
- **Per-skill configuration.** `agent_config.yaml` defines every skill's prompt, temperature, `max_tokens`, allowed tools, and behavioural flags (critic, internal_successors).
- **Separation of concerns.** The orchestrator handles graph topology, concurrency, and persistence. Skills handle cognition. The gateway handles LLM routing and provider failover.
- **Session persistence.** Every graph state is serialised to disk after each node completes, enabling crash recovery and session replay (`--resume` flag).

```
                         ┌──────────────────┐
                         │   USER QUERY      │
                         └────────┬─────────┘
                                  │
                         ┌────────▼─────────┐
                         │    Planner (n:1)  │  ← Decomposes query into initial DAG
                         └────────┬─────────┘
                                  │
                    ┌─────────────┼─────────────┐
                    │             │              │
              ┌─────▼────┐  ┌────▼─────┐  ┌─────▼────┐
              │Researcher │  │Researcher │  │ Researcher│  ← Parallel workers
              └─────┬────┘  └────┬──────┘  └─────┬────┘
                    │            │                │
              ┌─────▼────┐  ┌────▼─────┐  ┌─────▼────┐
              │TokenMiser│  │TokenMiser│  │TokenMiser│  ← Auto-inserted compression
              └─────┬────┘  └────┬──────┘  └─────┬────┘
                    │            │                │
                    └────────────┼────────────────┘
                                 │
                          ┌──────▼──────┐
                          │  Formatter   │  ← Produces final answer
                          └─────────────┘
```

---

## Skill Catalog

All skills are defined declaratively in [`code/agent_config.yaml`](code/agent_config.yaml). There is no Python class per skill — the orchestrator loads prompt templates and configuration from the YAML at startup.

| Skill | Temperature | Max Tokens | Tools Allowed | Description |
|---|---|---|---|---|
| **Planner** | 0.4 | 1,500 | — | Decomposes the user query into the initial DAG; synthesises recovery subgraphs on node failure |
| **Researcher** | 0.7 | 2,500 | `web_search`, `fetch_url` | Multi-step web research with normalised text outputs |
| **Retriever** | 0.2 | 1,200 | `search_knowledge` | Vector search over FAISS-indexed knowledge base |
| **Distiller** | 0.1 | 1,200 | — | Extracts structured fields from raw text or page content. Has `critic: true` — every outgoing edge gets a Critic inserted |
| **Summariser** | 0.3 | 1,200 | — | Condenses long content into a short form |
| **Critic** | 0.0 | 500 | — | Evaluates upstream node output; emits pass or fail with rationale. Deterministic (temperature=0) |
| **Coder** | 0.2 | 1,500 | — | Generates modular, correct code. Auto-spawns `sandbox_executor` via `internal_successors` |
| **Sandbox Executor** | 0.0 | 400 | — | Runs code from Coder in an isolated sandbox; returns stdout/stderr/exit code. Bypasses the LLM gateway entirely |
| **Formatter** | 0.3 | 1,500 | — | Renders the final answer to the user. Conventional terminal node — its `output.final_answer` is returned by the executor |
| **Token Miser** | 0.0 | 2,048 | — | Lossy-compression filter that strips noise from retrieval outputs before they reach downstream nodes |
| **Browser** | 0.3 | 1,500 | — | *(Stub — reserved for future use)* |

### Provider Pinning (via Gateway)

The gateway's [`agent_routing.yaml`](gateway/agent_routing.yaml) maps each skill to a preferred LLM provider. Pins are preferences, not hard bindings — if a provider is in cooldown, the gateway falls back through the normal failover ladder.

| Skill | Pinned Provider |
|---|---|
| Planner | Gemini |
| Researcher | Gemini |
| Distiller | Gemini |
| Summariser | Gemini |
| Critic | Groq |
| Formatter | Gemini |
| Retriever | GitHub Models |
| Coder | Gemini |
| Sandbox Executor | GitHub Models |
| Browser | Gemini |

---

## How the DAG Grows at Runtime

The graph starts with a single Planner node and expands through **five independent mechanisms**:

### 1. Planner Seed Plan
The Planner receives the user query and emits an initial set of successor nodes. For example, `"Find populations of London, Paris, Berlin"` produces three parallel Researcher nodes plus a Formatter.

### 2. Dynamic Successors
Any skill can emit successors in its JSON output via the `successors` field. This enables a Researcher to spawn additional sub-researchers, or the Planner to re-insert itself for recovery.

### 3. Internal Successors (Static)
Defined in `agent_config.yaml`. When a Coder node completes, `internal_successors: [sandbox_executor]` automatically adds a Sandbox Executor child — no code change needed.

### 4. Critic Auto-Insertion
Skills with `critic: true` (currently Distiller) automatically get a Critic node inserted on **every outgoing edge**. The child only runs after the Critic passes. If the Critic fails, the orchestrator triggers recovery (re-plan via Planner, capped at one retry per branch).

### 5. Token Miser Auto-Insertion
When a Researcher or Retriever node completes with a large output, the orchestrator inserts a Token Miser node between it and its downstream readers. This preserves the parent's raw output for any other consumers while giving the selected child a compressed version.

---

## Token Miser — Automatic Compression

The Token Miser is a lossy-compression skill that automatically activates when a Researcher or Retriever produces output exceeding **600 characters**. It reduces token consumption by 40–65% while preserving factual content.

**How it works:**
1. The orchestrator detects a large output from Researcher/Retriever
2. It creates a Token Miser node and rewires the edge: `Researcher → Miser → Downstream`
3. The Miser calls a cheap LLM to compress the text
4. Downstream nodes receive compressed input; the original is preserved on the graph node for any other consumers

**Session-level statistics** are printed at the end of each run showing total characters saved, compression ratio, and estimated token savings.

```
────────────────────────────────────────────────────────────
TOKEN MISER — SESSION SUMMARY
────────────────────────────────────────────────────────────
  Nodes compressed: 3
  Total input chars: 2,777
  Total output chars: 1,380
  Overall compression: 50.3%
  Gross chars saved (× downstream readers): 1,397
  Est. token savings: ~349
  Miser LLM cost (est. tokens): −0
  Net estimated token savings: ~349
────────────────────────────────────────────────────────────
```

---

## Memory System

Memory is read **once at session start** and the same ranked hits flow into every skill's prompt. This carries forward the Session 7 contract: every cognitive role can see what the agent already knows.

- **FAISS vector index** over previously indexed documents and interactions
- **Hits ranked** by relevance to the user query
- **Up to 8 hits** surfaced per session (capped to keep prompts bounded)
- Each hit includes: kind, descriptor, source, and a 2000-char preview of the chunk

The system uses the gateway's `POST /v1/embed` endpoint (768-dim vectors via `nomic-embed-text` or `gemini-embedding-001`) for indexing and retrieval.

---

## Error Recovery

When a node fails, the orchestrator consults the recovery module (`recovery.py`):

1. **Critic fail** → if this is the first failure for this branch, the Planner is re-invoked with a failure report to produce a corrected subgraph. On the second failure, the branch is skipped and a warning is logged.
2. **Skill failure** (e.g. tool error, parse error) → the Planner synthesises a recovery subgraph that re-tries with adjusted parameters.
3. **Hard cap** — maximum 60 nodes per session prevents runaway Planner loops.

---

## Project Structure

```
multi-agent-dag/
├── README.md                  ← This file
├── .env.example               ← Environment variable template
│
├── code/                      ← Core orchestrator & skills
│   ├── flow.py                ← Growing-graph executor (main loop)
│   ├── skills.py              ← Skill registry, prompt rendering, tool dispatch
│   ├── agent_config.yaml      ← Declarative skill definitions
│   ├── perception.py          ← Agent perception layer (S7 orchestration)
│   ├── decision.py            ← Decision-making layer
│   ├── memory.py              ← Memory read/write for FAISS index
│   ├── vector_index.py        ← FAISS vector index operations
│   ├── schemas.py             ← Pydantic models (AgentResult, NodeState, etc.)
│   ├── persistence.py         ← Session graph serialisation + replay
│   ├── recovery.py            ← Failure handling (critic verdicts, re-planning)
│   ├── artifacts.py           ← Artifact storage & retrieval
│   ├── sandbox.py             ← Isolated Python sandbox for code execution
│   ├── mcp_server.py          ← MCP server for tool execution
│   ├── mcp_runner.py          ← Multi-turn tool-use loop
│   ├── gateway.py             ← LLM gateway client
│   ├── replay.py              ← Session replay visualisation
│   ├── visualizer.py          ← Graph visualisation utilities
│   ├── run_all.py             ← Batch runner (9 queries → README + logs)
│   ├── pyproject.toml         ← Python project config
│   ├── requirements.txt       ← Dependencies
│   ├── usage.json             ← Usage tracking
│   │
│   ├── prompts/               ← System prompts per skill
│   │   ├── planner.md
│   │   ├── researcher.md
│   │   ├── retriever.md
│   │   ├── distiller.md
│   │   ├── summariser.md
│   │   ├── critic.md
│   │   ├── coder.md
│   │   ├── sandbox_executor.md
│   │   ├── formatter.md
│   │   ├── token_miser.md
│   │   ├── browser.md
│   │   └── ... (other prompts)
│   │
│   ├── sandbox/papers/        ← Reference papers for sandbox testing
│   │   ├── attention.md
│   │   ├── cot.md
│   │   ├── dpo.md
│   │   ├── lora.md
│   │   └── react.md
│   │
│   └── tests/
│       └── test_recovery.py   ← Recovery module tests
│
├── gateway/                   ← LLM Gateway V7 + V8 agent routing
│   ├── main.py                ← FastAPI app with /v1/chat, /v1/embed
│   ├── providers.py           ← Provider adapters (Gemini, Groq, etc.)
│   ├── router.py              ← Router pool + worker pool with rate limiting
│   ├── client.py              ← Python SDK (LLM class)
│   ├── agent_routing.yaml     ← Skill → provider pinning
│   ├── cache.py               ← Gemini caching support
│   ├── db.py                  ← SQLite call logging
│   ├── embedders.py           ← Embedding providers (Ollama, Gemini)
│   ├── schemas.py             ← Pydantic models
│   ├── README.md              ← Gateway documentation
│   ├── requirements.txt
│   ├── run.sh                 ← Startup script
│   └── static/
│       ├── dashboard.html     ← Live dashboard
│       └── help.html          ← Help page
│
└── run_logs/                  ← Timestamped batch run logs
    └── run_20260610_011246.log
```

---

## Gateway Integration

The system connects to the **LLM Gateway V7** (port 8107) for all LLM calls and the **V3 router pool** (port 8101) for cognitive-layer routing. The gateway provides:

- **Seven worker providers:** Ollama, Gemini, NVIDIA NIM, Groq, Cerebras, OpenRouter, GitHub Models
- **Four-router pool** for classification (Cerebras, Groq, NVIDIA, GitHub) that routes requests to the appropriate worker tier (TINY / LARGE)
- **Agent routing** — per-skill provider pinning via `agent_routing.yaml`
- **Embedding service** — `POST /v1/embed` with 768-dim vectors (Ollama `nomic-embed-text` primary, Gemini `gemini-embedding-001` fallback)
- **Session-aware caching** via Gemini SHA-256 explicit cache
- **Live dashboard** at `http://localhost:8101`

Skills call the gateway through two paths:
- **Text-only skills** (Planner, Formatter, Critic, Distiller) → direct `LLM().chat()`
- **Tool-using skills** (Researcher, Retriever) → `mcp_runner.run_with_tools()` multi-turn loop
- **Sandbox Executor** → bypasses the gateway entirely, calls `sandbox.run_python()` directly

---

## Getting Started

### Prerequisites

- Python 3.11+
- [uv](https://docs.astral.sh/uv/) (fast Python package installer)
- LLM Gateway running on port 8107 (with V3 routing on 8101)

### Setup

```bash
# Clone the repository
git clone https://github.com/kuralamuthan300/multi-agent-dag.git
cd multi-agent-dag

# Set up environment
cp .env.example .env
# Edit .env with your API keys

# Install dependencies
cd code && uv sync
cd ../gateway && uv sync
```

### Running a Single Query

```bash
cd code
uv run python3 flow.py "Say hello."

# Resume a previous session
uv run python3 flow.py --resume <session_id>

# Run all 9 benchmark queries (generates README + logs)
uv run python3 run_all.py
```

The first run executes a single Planner → Formatter flow. More complex queries automatically build larger graphs with parallel researchers, token compression, code execution, and critic evaluation.

---

## Batch Run Results

Batch executed on **2026-06-10 01:18:15**

| # | Label | Purpose | Status | Time |
|---|-------|---------|--------|------|
| 1 | `arg1-hello` | Basic hello-world test | ✅ | 10.1s |
| 2 | `arg2-shannon` | Fetch Wikipedia article (researcher web fetch) | ✅ | 24.5s |
| 3 | `arg3-populations` | Multi-city research (parallel researchers) | ✅ | 67.3s |
| 4 | `arg4-nonexistent` | Error handling (read non-existent file) | ✅ | 11.8s |
| 5 | `arg5-africa` | Multi-city Africa research (parallel researchers) | ✅ | 50.2s |
| 6 | `skill-parallel` | Parallel processing — multiple researchers spawned by Planner | ✅ | 74.6s |
| 7 | `skill-critic` | Critic skill — Researcher → Distiller (critic: true) → Critic auto-inserts | ✅ | 26.4s |
| 8 | `skill-coder` | Coder skill + SandboxExecutor — Kadane's algorithm | ✅ | 17.0s |
| 9 | `skill-token-miser` | Token Miser — large research output triggers auto-compression | ✅ | 47.1s |

**9/9 queries succeeded.**

### Full Logs

Full raw logs saved to: [`/Users/kural/Documents/EAGv3/WEEK8/multi-agent-dag/run_logs/run_20260610_011246.log`](/Users/kural/Documents/EAGv3/WEEK8/multi-agent-dag/run_logs/run_20260610_011246.log)

---

### Query #1: arg1-hello ✅

**Purpose:** Basic hello-world test

**Query:** `Say hello.`

**Elapsed:** 10.1s  |  **Exit code:** 0

```
══════════════════════════════════════════════════════════════════════════════
session s8-399dcb1a  ─  query: Say hello.
══════════════════════════════════════════════════════════════════════════════
[memory.read] 8 hit(s) visible to every skill this run
[n:1] planner            complete (3.9s)
[n:2] formatter          complete (4.1s)

══════════════════════════════════════════════════════════════════════════════
FINAL: Hello! How can I help you today?
══════════════════════════════════════════════════════════════════════════════
```

---

### Query #2: arg2-shannon ✅

**Purpose:** Fetch Wikipedia article (researcher web fetch)

**Query:** `Fetch https://en.wikipedia.org/wiki/Claude_Shannon and tell me his birth date, death date, and three key contributions to information theory.`

**Elapsed:** 24.5s  |  **Exit code:** 0

```
══════════════════════════════════════════════════════════════════════════════
session s8-748541fe  ─  query: Fetch https://en.wikipedia.org/wiki/Claude_Shannon and tell me his birth date, death date, and three key contributions to information theory.
══════════════════════════════════════════════════════════════════════════════
[memory.read] 8 hit(s) visible to every skill this run
[n:1] planner            complete (4.3s)
[n:2] researcher         complete (12.4s)
  └─ inserted token_miser (n:4) between n:2 → n:3
  └─ Token Miser: 763→423 chars (45% reduction)
[n:4] token_miser        complete (1.4s)
[n:3] formatter          complete (2.0s)

─────────────────────────────────────────────────────────────────────────────
TOKEN MISER — SESSION SUMMARY
─────────────────────────────────────────────────────────────────────────────
  Nodes compressed: 1
  Total input chars: 763
  Total output chars: 423
  Overall compression: 44.6%
  Gross chars saved (× downstream readers): 340
  Est. token savings: ~85
  Miser LLM cost (est. tokens): −0
  Net estimated token savings: ~85
─────────────────────────────────────────────────────────────────────────────

══════════════════════════════════════════════════════════════════════════════
FINAL: Claude Shannon was born on April 30, 1916, and passed away on February 24, 2001. His three key contributions to information theory include: 

1. Formalizing entropy as a measure of information uncertainty.
2. Developing a mathematical framework for data compression.
3. Establishing fundamental limits on communication channel capacity, known as Shannon's channel capacity theorem.

Source: https://en.wikipedia.org/wiki/Claude_Shannon
══════════════════════════════════════════════════════════════════════════════
```

**stderr:**
```
[06/10/26 01:13:08] INFO     Processing request of type            server.py:727
                             CallToolRequest                                    
[INIT].... → Crawl4AI 0.8.6 
[FETCH]... ↓ https://en.wikipedia.org/wiki/Claude_Shannon                       
| ✓ | ⏱: 1.26s 
[SCRAPE].. ◆ https://en.wikipedia.org/wiki/Claude_Shannon                       
| ✓ | ⏱: 0.16s 
[COMPLETE] ● https://en.wikipedia.org/wiki/Claude_Shannon                       
| ✓ | ⏱: 1.43s 
[06/10/26 01:13:11] INFO     Processing request of type            server.py:727
                             ListToolsRequest                                   
[06/10/26 01:13:12] INFO     Processing request of type            server.py:727
                             CallToolRequest                                    
[INIT].... → Crawl4AI 0.8.6 
[FETCH]... ↓ https://en.wikipedia.org/api/rest_v1/page/html/Claude_Shannon      
| ✓ | ⏱: 1.93s 
[SCRAPE].. ◆ https://en.wikipedia.org/api/rest_v1/page/html/Claude_Shannon      
| ✓ | ⏱: 0.13s 
[COMPLETE] ● https://en.wikipedia.org/api/rest_v1/page/html/Claude_Shannon      
| ✓ | ⏱: 2.07s
```

---

### Query #3: arg3-populations ✅

**Purpose:** Multi-city research (parallel researchers)

**Query:** `Find the populations of London, Paris, Berlin and tell me which two are closest in size.`

**Elapsed:** 67.3s  |  **Exit code:** 0

```
══════════════════════════════════════════════════════════════════════════════
session s8-d5d708b9  ─  query: Find the populations of London, Paris, Berlin and tell me which two are closest in size.
══════════════════════════════════════════════════════════════════════════════
[memory.read] 8 hit(s) visible to every skill this run
[n:1] planner            complete (4.3s)
[n:2] researcher         complete (36.7s)
  └─ inserted token_miser (n:6) between n:2 → n:5
[n:3] researcher         complete (41.0s)
  └─ inserted token_miser (n:7) between n:3 → n:5
[n:4] researcher         complete (32.9s)
  └─ inserted token_miser (n:8) between n:4 → n:5
  └─ Token Miser: 778→430 chars (45% reduction)
[n:6] token_miser        complete (16.5s)
  └─ Token Miser: 1,047→401 chars (62% reduction)
[n:7] token_miser        complete (15.1s)
  └─ Token Miser: 952→549 chars (42% reduction)
[n:8] token_miser        complete (15.8s)
[n:5] formatter          complete (1.1s)

─────────────────────────────────────────────────────────────────────────────
TOKEN MISER — SESSION SUMMARY
─────────────────────────────────────────────────────────────────────────────
  Nodes compressed: 3
  Total input chars: 2,777
  Total output chars: 1,380
  Overall compression: 50.3%
  Gross chars saved (× downstream readers): 1,397
  Est. token savings: ~349
  Miser LLM cost (est. tokens): −0
  Net estimated token savings: ~349
─────────────────────────────────────────────────────────────────────────────

══════════════════════════════════════════════════════════════════════════════
FINAL: Based on the provided data, the populations for the city proper of each city are as follows: London has approximately 9.1 million residents, Berlin has approximately 3.9 million, and Paris has approximately 2.05 million. Comparing these figures, Berlin and Paris are the two cities closest in size, with a difference of approximately 1.85 million, compared to the much larger gaps between London and the other two cities.
══════════════════════════════════════════════════════════════════════════════
```

**stderr:**
```
[06/10/26 01:13:32] INFO     Processing request of type            server.py:727
                             CallToolRequest                                    
[06/10/26 01:13:33] INFO     response:                                lib.rs:444
                             https://en.wikipedia.org/w/api.php?actio           
                             n=opensearch&profile=fuzzy&limit=1&searc           
                             h=current%20population%20of%20Berlin%202           
                             024%202025 200                                     
[06/10/26 01:13:36] INFO     response:                                lib.rs:444
                             https://grokipedia.com/api/typeahead?que           
                             ry=current+population+of+Berlin+2024+202           
                             5&limit=1 200                                      
[06/10/26 01:13:36] INFO     Processing request of type            server.py:727
                             CallToolRequest                                    
[06/10/26 01:13:37] INFO     response:                                lib.rs:444
                             https://search.brave.com/search?q=curren           
                             t+population+of+Berlin+2024+2025&source=           
                             web 429                                            
[06/10/26 01:13:37] INFO     response:                                lib.rs:444
                             https://en.wikipedia.org/w/api.php?actio           
                             n=opensearch&profile=fuzzy&limit=1&searc           
                             h=current%20population%20of%20London%202           
                             024%202025 200                                     
[06/10/26 01:13:38] INFO     HTTP Request: POST                  _client.py:1025
                             https://html.duckduckgo.com/html/                  
                             "HTTP/2 200 OK"                                    
                    INFO     Processing request of type            server.py:727
                             ListToolsRequest                                   
[06/10/26 01:13:38] INFO     response:                                lib.rs:444
                             https://grokipedia.com/api/typeahead?que           
                             ry=current+population+of+London+2024+202           
                             5&limit=1 200                                      
[06/10/26 01:13:39] INFO     response:                                lib.rs:444
                             https://www.mojeek.com/search?q=current+           
                             population+of+London+2024+2025 200                 
                    INFO     Processing request of type            server.py:727
                             ListToolsRequest                                   
[06/10/26 01:13:40] INFO     Processing request of type            server.py:727
                             CallToolRequest                                    
[INIT].... → Crawl4AI 0.8.6 
[FETCH]... ↓ https://www.varbes.com/population/london-population                
| ✓ | ⏱: 1.37s 
[SCRAPE].. ◆ https://www.varbes.com/population/london-population                
| ✓ | ⏱: 0.00s 
[COMPLETE] ● https://www.varbes.com/population/london-population                
| ✗ | ⏱: 1.37s 
[06/10/26 01:13:45] INFO     Processing request of type            server.py:727
                             CallToolRequest                                    
                    INFO     response:                                lib.rs:444
                             https://en.wikipedia.org/w/api.php?actio           
                             n=opensearch&profile=fuzzy&limit=1&searc           
                             h=current%20population%20of%20Paris%20Fr           
                             ance%202024%202025 200                             
                    INFO     response:                                lib.rs:444
                             https://grokipedia.com/api/typeahead?que           
                             ry=current+population+of+Paris+France+20           
                             24+2025&limit=1 200                                
[06/10/26 01:13:46] INFO     response:                                lib.rs:444
                             https://www.google.com/search?q=current+           
                             population+of+Paris+France+2024+2025&fil           
                             ter=1&start=0&hl=en-US&lr=lang_en&cr=cou           
                             ntryUS 200                                         
                    INFO     response: https://www.startpage.com/ 200 lib.rs:444
[06/10/26 01:13:47] INFO     response:                                lib.rs:444
                             https://www.startpage.com/sp/search 200            
                    INFO     Processing request of type            server.py:727
                             ListToolsRequest                                   
[06/10/26 01:13:49] INFO     Processing request of type            server.py:727
                             CallToolRequest                                    
[INIT].... → Crawl4AI 0.8.6 
[FETCH]... ↓ 
https://www.businesslocationcenter.de/en/business-location/berlin-at-a-glance/de
mographic-data/      | ✓ | ⏱: 2.77s 
[SCRAPE].. ◆ 
https://www.businesslocationcenter.de/en/business-location/berlin-at-a-glance/de
mographic-data/      | ✓ | ⏱: 0.02s 
[COMPLETE] ● 
https://www.businesslocationcenter.de/en/business-location/berlin-at-a-glance/de
mographic-data/      | ✓ | ⏱: 2.80s 
[06/10/26 01:13:53] INFO     Processing request of type            server.py:727
                             CallToolRequest                                    
[INIT].... → Crawl4AI 0.8.6 
[FETCH]... ↓ https://trustforlondon.org.uk/data/topics/population/              
| ✓ | ⏱: 2.93s 
[SCRAPE].. ◆ https://trustforlondon.org.uk/data/topics/population/              
| ✓ | ⏱: 0.01s 
[COMPLETE] ● https://trustforlondon.org.uk/data/topics/population/              
| ✓ | ⏱: 2.94s 
[06/10/26 01:13:57] INFO     Processing request of type            server.py:727
                             CallToolRequest                                    
[INIT].... → Crawl4AI 0.8.6 
[FETCH]... ↓ https://www.insee.fr/en/statistiques/serie/001760155               
| ✓ | ⏱: 6.03s 
[SCRAPE].. ◆ https://www.insee.fr/en/statistiques/serie/001760155               
| ✓ | ⏱: 0.00s 
[COMPLETE] ● https://www.insee.fr/en/statistiques/serie/001760155               
| ✓ | ⏱: 6.03s
```

---

### Query #4: arg4-nonexistent ✅

**Purpose:** Error handling (read non-existent file)

**Query:** `Read /nonexistent/path.txt and tell me what's in it.`

**Elapsed:** 11.8s  |  **Exit code:** 0

```
══════════════════════════════════════════════════════════════════════════════
session s8-6c310449  ─  query: Read /nonexistent/path.txt and tell me what's in it.
══════════════════════════════════════════════════════════════════════════════
[memory.read] 8 hit(s) visible to every skill this run
[n:1] planner            complete (3.9s)
[n:2] formatter          complete (3.9s)

══════════════════════════════════════════════════════════════════════════════
FINAL: I am unable to read the file at /nonexistent/path.txt because it does not exist.
══════════════════════════════════════════════════════════════════════════════
```

---

### Query #5: arg5-africa ✅

**Purpose:** Multi-city Africa research (parallel researchers)

**Query:** `For Lagos, Cairo, and Kinshasa, find current populations and growth rates and tell me which is growing fastest.`

**Elapsed:** 50.2s  |  **Exit code:** 0

```
══════════════════════════════════════════════════════════════════════════════
session s8-55daf4f6  ─  query: For Lagos, Cairo, and Kinshasa, find current populations and growth rates and tell me which is growing fastest.
══════════════════════════════════════════════════════════════════════════════
[memory.read] 8 hit(s) visible to every skill this run
[n:1] planner            complete (4.6s)
[n:2] researcher         complete (24.7s)
  └─ inserted token_miser (n:6) between n:2 → n:5
[n:3] researcher         complete (36.4s)
  └─ inserted token_miser (n:7) between n:3 → n:5
[n:4] researcher         complete (32.3s)
  └─ inserted token_miser (n:8) between n:4 → n:5
  └─ Token Miser: 735→371 chars (50% reduction)
[n:6] token_miser        complete (2.1s)
  └─ Token Miser: 608→343 chars (44% reduction)
[n:7] token_miser        complete (3.5s)
  └─ Token Miser: 652→315 chars (52% reduction)
[n:8] token_miser        complete (1.5s)
[n:5] formatter          complete (1.3s)

─────────────────────────────────────────────────────────────────────────────
TOKEN MISER — SESSION SUMMARY
─────────────────────────────────────────────────────────────────────────────
  Nodes compressed: 3
  Total input chars: 1,995
  Total output chars: 1,029
  Overall compression: 48.4%
  Gross chars saved (× downstream readers): 966
  Est. token savings: ~241
  Miser LLM cost (est. tokens): −0
  Net estimated token savings: ~241
─────────────────────────────────────────────────────────────────────────────

══════════════════════════════════════════════════════════════════════════════
FINAL: Based on 2026 estimates, here is the population and growth rate data for the three cities:

1. Lagos: 17,804,000 population with a ~3.78% annual growth rate.
2. Cairo: 23,535,000 population with a ~2.0% annual growth rate.
3. Kinshasa: 18,550,000 population with a 4.36% annual growth rate.

Kinshasa is currently growing the fastest among the three cities. Sources: Macrotrends (Lagos, Cairo, Kinshasa).
══════════════════════════════════════════════════════════════════════════════
```

**stderr:**
```
[06/10/26 01:14:52] INFO     Processing request of type            server.py:727
                             CallToolRequest                                    
                    INFO     response:                                lib.rs:444
                             https://en.wikipedia.org/w/api.php?actio           
                             n=opensearch&profile=fuzzy&limit=1&searc           
                             h=current%20population%20and%20annual%20           
                             growth%20rate%20of%20Lagos%202024%202025           
                              200                                               
[06/10/26 01:14:53] INFO     response:                                lib.rs:444
                             https://grokipedia.com/api/typeahead?que           
                             ry=current+population+and+annual+growth+           
                             rate+of+Lagos+2024+2025&limit=1 200                
                    INFO     response:                                lib.rs:444
                             https://search.yahoo.com/search;_ylt=Az2           
                             fo0Ayg3nj8a643jUEJHju;_ylu=fOPnwieZ8VkKH           
                             HPYK4WiFy4Zt85cIr5lamvl_0eWdVi9GKQ?p=cur           
                             rent+population+and+annual+growth+rate+o           
                             f+Lagos+2024+2025 200                              
[06/10/26 01:14:54] INFO     Processing request of type            server.py:727
                             ListToolsRequest                                   
[06/10/26 01:14:56] INFO     Processing request of type            server.py:727
                             CallToolRequest                                    
                    INFO     response:                                lib.rs:444
                             https://en.wikipedia.org/w/api.php?actio           
                             n=opensearch&profile=fuzzy&limit=1&searc           
                             h=current%20population%20and%20annual%20           
                             growth%20rate%20of%20Cairo%202024%202025           
                              200                                               
                    INFO     response:                                lib.rs:444
                             https://grokipedia.com/api/typeahead?que           
                             ry=current+population+and+annual+growth+           
                             rate+of+Cairo+2024+2025&limit=1 200                
                    INFO     response:                                lib.rs:444
                             https://www.mojeek.com/search?q=current+           
                             population+and+annual+growth+rate+of+Cai           
                             ro+2024+2025 403                                   
[06/10/26 01:14:58] INFO     response: https://www.startpage.com/ 200 lib.rs:444
[06/10/26 01:14:59] INFO     response:                                lib.rs:444
                             https://www.startpage.com/sp/search 200            
                    INFO     Processing request of type            server.py:727
                             ListToolsRequest                                   
[06/10/26 01:15:00] INFO     Processing request of type            server.py:727
                             CallToolRequest                                    
[06/10/26 01:15:01] INFO     response:                                lib.rs:444
                             https://grokipedia.com/api/typeahead?que           
                             ry=current+population+and+annual+growth+           
                             rate+of+Kinshasa+2024+2025&limit=1 200             
                    INFO     response:                                lib.rs:444
                             https://en.wikipedia.org/w/api.php?actio           
                             n=opensearch&profile=fuzzy&limit=1&searc           
                             h=current%20population%20and%20annual%20           
                             growth%20rate%20of%20Kinshasa%202024%202           
                             025 200                                            
                    INFO     response:                                lib.rs:444
                             https://www.mojeek.com/search?q=current+           
                             population+and+annual+growth+rate+of+Kin           
                             shasa+2024+2025 403                                
                    INFO     HTTP Request: POST                  _client.py:1025
                             https://html.duckduckgo.com/html/                  
                             "HTTP/2 202 Accepted"                              
[06/10/26 01:15:02] INFO     response:                                lib.rs:444
                             https://www.google.com/search?q=current+           
                             population+and+annual+growth+rate+of+Kin           
                             shasa+2024+2025&filter=1&start=0&hl=en-U           
                             S&lr=lang_en&cr=countryUS 200                      
                    INFO     response:                                lib.rs:444
                             https://search.yahoo.com/search;_ylt=iEx           
                             uAk5o7TF7mlv7usiIAZyL;_ylu=Lfk9lmAqk9Q1P           
                             zkUh8Q1sFzXIaYVLolnqRzK0KkQsNFzQB0?p=cur           
                             rent+population+and+annual+growth+rate+o           
                             f+Kinshasa+2024+2025 200                           
[06/10/26 01:15:04] INFO     response: https://www.startpage.com/ 200 lib.rs:444
[06/10/26 01:15:04] INFO     Processing request of type            server.py:727
                             CallToolRequest                                    
[06/10/26 01:15:05] INFO     response:                                lib.rs:444
                             https://www.startpage.com/sp/search 200            
                    INFO     Processing request of type            server.py:727
                             ListToolsRequest                                   
[INIT].... → Crawl4AI 0.8.6 
[FETCH]... ↓ 
https://www.macrotrends.net/global-metrics/cities/22812/cairo/population        
| ✓ | ⏱: 1.59s 
[SCRAPE].. ◆ 
https://www.macrotrends.net/global-metrics/cities/22812/cairo/population        
| ✓ | ⏱: 0.00s 
[COMPLETE] ● 
https://www.macrotrends.net/global-metrics/cities/22812/cairo/population        
| ✓ | ⏱: 1.60s 
[06/10/26 01:15:08] INFO     Processing request of type            server.py:727
                             CallToolRequest                                    
[INIT].... → Crawl4AI 0.8.6 
[FETCH]... ↓ 
https://www.macrotrends.net/global-metrics/cities/22007/lagos/population        
| ✓ | ⏱: 1.15s 
[SCRAPE].. ◆ 
https://www.macrotrends.net/global-metrics/cities/22007/lagos/population        
| ✓ | ⏱: 0.01s 
[COMPLETE] ● 
https://www.macrotrends.net/global-metrics/cities/22007/lagos/population        
| ✓ | ⏱: 1.16s 
[06/10/26 01:15:16] INFO     Processing request of type            server.py:727
                             CallToolRequest                                    
[INIT].... → Crawl4AI 0.8.6 
[FETCH]... ↓ 
https://www.macrotrends.net/global-metrics/cities/20853/kinshasa/population     
| ✓ | ⏱: 1.17s 
[SCRAPE].. ◆ 
https://www.macrotrends.net/global-metrics/cities/20853/kinshasa/population     
| ✓ | ⏱: 0.00s 
[COMPLETE] ● 
https://www.macrotrends.net/global-metrics/cities/20853/kinshasa/population     
| ✓ | ⏱: 1.18s
```

---

### Query #6: skill-parallel ✅

**Purpose:** Parallel processing — multiple researchers spawned by Planner

**Query:** `Find the populations of London, Paris, Berlin and tell me which two are closest in size.`

**Elapsed:** 74.6s  |  **Exit code:** 0

```
══════════════════════════════════════════════════════════════════════════════
session s8-70568b94  ─  query: Find the populations of London, Paris, Berlin and tell me which two are closest in size.
══════════════════════════════════════════════════════════════════════════════
[memory.read] 8 hit(s) visible to every skill this run
[n:1] planner            complete (4.4s)
[n:2] researcher         complete (44.5s)
  └─ inserted token_miser (n:6) between n:2 → n:5
[n:3] researcher         complete (36.6s)
  └─ inserted token_miser (n:7) between n:3 → n:5
[n:4] researcher         complete (23.9s)
  └─ inserted token_miser (n:8) between n:4 → n:5
  └─ Token Miser: 686→371 chars (46% reduction)
[n:6] token_miser        complete (1.3s)
  └─ Token Miser: 916→391 chars (57% reduction)
[n:7] token_miser        complete (1.7s)
  └─ Token Miser: 364→156 chars (57% reduction)
[n:8] token_miser        complete (20.6s)
[n:5] formatter          complete (1.2s)

─────────────────────────────────────────────────────────────────────────────
TOKEN MISER — SESSION SUMMARY
─────────────────────────────────────────────────────────────────────────────
  Nodes compressed: 3
  Total input chars: 1,966
  Total output chars: 918
  Overall compression: 53.3%
  Gross chars saved (× downstream readers): 1,048
  Est. token savings: ~262
  Miser LLM cost (est. tokens): −0
  Net estimated token savings: ~262
─────────────────────────────────────────────────────────────────────────────

══════════════════════════════════════════════════════════════════════════════
FINAL: The current populations for the three cities are approximately as follows: London (Greater London) has about 9.8 million residents, Paris (city proper) has about 2.1 million residents, and Berlin has about 3.87 million residents. Comparing these figures, Berlin and Paris are the two cities closest in size, with a difference of approximately 1.77 million, whereas London is significantly larger than both.
══════════════════════════════════════════════════════════════════════════════
```

**stderr:**
```
[06/10/26 01:15:41] INFO     Processing request of type            server.py:727
                             CallToolRequest                                    
[06/10/26 01:15:42] INFO     response:                                lib.rs:444
                             https://en.wikipedia.org/w/api.php?actio           
                             n=opensearch&profile=fuzzy&limit=1&searc           
                             h=current%20population%20of%20Berlin%202           
                             024%202025 200                                     
                    INFO     response:                                lib.rs:444
                             https://grokipedia.com/api/typeahead?que           
                             ry=current+population+of+Berlin+2024+202           
                             5&limit=1 200                                      
[06/10/26 01:15:43] INFO     response:                                lib.rs:444
                             https://search.brave.com/search?q=curren           
                             t+population+of+Berlin+2024+2025&source=           
                             web 429                                            
                    INFO     response:                                lib.rs:444
                             https://search.yahoo.com/search;_ylt=883           
                             8gkIYpWRU67COWKtD3S4a;_ylu=BJaojveW2tojL           
                             S65aGAOdEuEXR0YBUKYCJc1L2xOk6Wq6hQ?p=cur           
                             rent+population+of+Berlin+2024+2025 200            
[06/10/26 01:15:45] INFO     response:                                lib.rs:444
                             https://www.mojeek.com/search?q=current+           
                             population+of+Berlin+2024+2025 200                 
                    INFO     Processing request of type            server.py:727
                             ListToolsRequest                                   
[06/10/26 01:15:45] INFO     Processing request of type            server.py:727
                             CallToolRequest                                    
[06/10/26 01:15:46] INFO     response:                                lib.rs:444
                             https://grokipedia.com/api/typeahead?que           
                             ry=current+population+of+Paris+France+20           
                             24+2025&limit=1 200                                
                    INFO     response:                                lib.rs:444
                             https://en.wikipedia.org/w/api.php?actio           
                             n=opensearch&profile=fuzzy&limit=1&searc           
                             h=current%20population%20of%20Paris%20Fr           
                             ance%202024%202025 200                             
[06/10/26 01:15:48] INFO     response:                                lib.rs:444
                             https://www.mojeek.com/search?q=current+           
                             population+of+Paris+France+2024+2025 200           
                    INFO     Processing request of type            server.py:727
                             ListToolsRequest                                   
[06/10/26 01:15:50] INFO     Processing request of type            server.py:727
                             CallToolRequest                                    
[INIT].... → Crawl4AI 0.8.6 
[06/10/26 01:15:54] INFO     Processing request of type            server.py:727
                             CallToolRequest                                    
[FETCH]... ↓ https://volsung.com/blog/berlin-growth-by-2045                     
| ✓ | ⏱: 3.16s 
[SCRAPE].. ◆ https://volsung.com/blog/berlin-growth-by-2045                     
| ✓ | ⏱: 0.01s 
[COMPLETE] ● https://volsung.com/blog/berlin-growth-by-2045                     
| ✓ | ⏱: 3.18s 
[INIT].... → Crawl4AI 0.8.6 
[06/10/26 01:15:58] INFO     Processing request of type            server.py:727
                             CallToolRequest                                    
                    INFO     response:                                lib.rs:444
                             https://en.wikipedia.org/w/api.php?actio           
                             n=opensearch&profile=fuzzy&limit=1&searc           
                             h=current%20population%20of%20London%202           
                             024%202025 200                                     
                    INFO     response:                                lib.rs:444
                             https://grokipedia.com/api/typeahead?que           
                             ry=current+population+of+London+2024+202           
                             5&limit=1 200                                      
[FETCH]... ↓ https://www.insee.fr/en/statistics/7746150?sommaire=7746165        
| ✓ | ⏱: 4.46s 
[SCRAPE].. ◆ https://www.insee.fr/en/statistics/7746150?sommaire=7746165        
| ✓ | ⏱: 0.01s 
[COMPLETE] ● https://www.insee.fr/en/statistics/7746150?sommaire=7746165        
| ✓ | ⏱: 4.48s 
[06/10/26 01:15:59] INFO     response: https://www.startpage.com/ 200 lib.rs:444
[06/10/26 01:16:00] INFO     response:                                lib.rs:444
                             https://www.startpage.com/sp/search 200            
                    INFO     Processing request of type            server.py:727
                             ListToolsRequest                                   
[06/10/26 01:16:06] INFO     Processing request of type            server.py:727
                             CallToolRequest                                    
[06/10/26 01:16:07] INFO     response:                                lib.rs:444
                             https://en.wikipedia.org/w/api.php?actio           
                             n=opensearch&profile=fuzzy&limit=1&searc           
                             h=official%20population%20of%20Paris%20c           
                             ity%20limits%202024%20INSEE 200                    
                    INFO     response:                                lib.rs:444
                             https://grokipedia.com/api/typeahead?que           
                             ry=official+population+of+Paris+city+lim           
                             its+2024+INSEE&limit=1 200                         
[06/10/26 01:16:08] INFO     response: https://www.startpage.com/ 200 lib.rs:444
[06/10/26 01:16:09] INFO     response:                                lib.rs:444
                             https://www.startpage.com/sp/search 200            
[06/10/26 01:16:10] INFO     Processing request of type            server.py:727
                             CallToolRequest                                    
[INIT].... → Crawl4AI 0.8.6 
[FETCH]... ↓ https://en.wikipedia.org/wiki/London                               
| ✓ | ⏱: 1.35s 
[SCRAPE].. ◆ https://en.wikipedia.org/wiki/London                               
| ✓ | ⏱: 0.42s 
[COMPLETE] ● https://en.wikipedia.org/wiki/London                               
| ✓ | ⏱: 1.78s 
[06/10/26 01:16:18] INFO     Processing request of type            server.py:727
                             CallToolRequest                                    
[INIT].... → Crawl4AI 0.8.6 
[FETCH]... ↓ https://en.wikipedia.org/wiki/Demographics_of_London               
| ✓ | ⏱: 1.27s 
[SCRAPE].. ◆ https://en.wikipedia.org/wiki/Demographics_of_London               
| ✓ | ⏱: 0.30s 
[COMPLETE] ● https://en.wikipedia.org/wiki/Demographics_of_London               
| ✓ | ⏱: 1.58s
```

---

### Query #7: skill-critic ✅

**Purpose:** Critic skill — Researcher → Distiller (critic: true) → Critic auto-inserts

**Query:** `Fetch the Wikipedia article on Claude Shannon and extract his birth date, death date, and three key contributions to information theory as structured fields. Present them in a clean format.`

**Elapsed:** 26.4s  |  **Exit code:** 0

```
══════════════════════════════════════════════════════════════════════════════
session s8-a8b09898  ─  query: Fetch the Wikipedia article on Claude Shannon and extract his birth date, death date, and three key contributions to information theory as structured fields. Present them in a clean format.
══════════════════════════════════════════════════════════════════════════════
[memory.read] 8 hit(s) visible to every skill this run
[n:1] planner            complete (4.6s)
[n:2] retriever          complete (9.8s)
  └─ inserted token_miser (n:5) between n:2 → n:3
  └─ Token Miser: 361→151 chars (58% reduction)
[n:5] token_miser        complete (2.9s)
[n:3] distiller          complete (0.7s)
[n:4] formatter          complete (4.2s)

─────────────────────────────────────────────────────────────────────────────
TOKEN MISER — SESSION SUMMARY
─────────────────────────────────────────────────────────────────────────────
  Nodes compressed: 1
  Total input chars: 361
  Total output chars: 151
  Overall compression: 58.2%
  Gross chars saved (× downstream readers): 210
  Est. token savings: ~52
  Miser LLM cost (est. tokens): −0
  Net estimated token savings: ~52
─────────────────────────────────────────────────────────────────────────────

══════════════════════════════════════════════════════════════════════════════
FINAL: I am sorry, but the requested information regarding Claude Shannon's birth date, death date, and contributions to information theory could not be found in the available knowledge base.
══════════════════════════════════════════════════════════════════════════════
```

**stderr:**
```
[06/10/26 01:16:56] INFO     Processing request of type            server.py:727
                             CallToolRequest                                    
                    INFO     HTTP Request: GET                   _client.py:1025
                             http://localhost:8108/v1/routers                   
                             "HTTP/1.1 200 OK"                                  
                    INFO     HTTP Request: POST                  _client.py:1025
                             http://localhost:8108/v1/embed                     
                             "HTTP/1.1 200 OK"                                  
                    INFO     Processing request of type            server.py:727
                             ListToolsRequest
```

---

### Query #8: skill-coder ✅

**Purpose:** Coder skill + SandboxExecutor — Kadane's algorithm

**Query:** `Write a Python function implementing Kadane's algorithm to find the maximum sum subarray in an array that may contain negative numbers. Then test it on the input [-2, 1, -3, 4, -1, 2, 1, -5, 4] and run it in the sandbox to show the result.`

**Elapsed:** 17.0s  |  **Exit code:** 0

```
══════════════════════════════════════════════════════════════════════════════
session s8-f26b52ab  ─  query: Write a Python function implementing Kadane's algorithm to find the maximum sum subarray in an array that may contain negative numbers. Then test it on the input [-2, 1, -3, 4, -1, 2, 1, -5, 4] and run it in the sandbox to show the result.
══════════════════════════════════════════════════════════════════════════════
[memory.read] 8 hit(s) visible to every skill this run
[n:1] planner            complete (4.3s)
[n:2] coder              complete (3.4s)
[n:3] sandbox_executor   complete (0.0s)
[n:5] sandbox_executor   complete (0.0s)
[n:4] formatter          complete (4.8s)

══════════════════════════════════════════════════════════════════════════════
FINAL: To find the maximum sum subarray, we can use Kadane's algorithm, which iterates through the array while maintaining the maximum sum ending at the current position. Here is the Python implementation:

```python
def kadane(arr):
    max_so_far = arr[0]
    current_max = arr[0]
    for i in range(1, len(arr)):
        current_max = max(arr[i], current_max + arr[i])
        max_so_far = max(max_so_far, current_max)
    return max_so_far

nums = [-2, 1, -3, 4, -1, 2, 1, -5, 4]
print(kadane(nums))
```

When executed with the input [-2, 1, -3, 4, -1, 2, 1, -5, 4], the function returns 6, which corres
══════════════════════════════════════════════════════════════════════════════
```

---

### Query #9: skill-token-miser ✅

**Purpose:** Token Miser — large research output triggers auto-compression

**Query:** `For Lagos, Cairo, and Kinshasa, find current populations and growth rates and tell me which is growing fastest. Be thorough in your research.`

**Elapsed:** 47.1s  |  **Exit code:** 0

```
══════════════════════════════════════════════════════════════════════════════
session s8-5c73e499  ─  query: For Lagos, Cairo, and Kinshasa, find current populations and growth rates and tell me which is growing fastest. Be thorough in your research.
══════════════════════════════════════════════════════════════════════════════
[memory.read] 8 hit(s) visible to every skill this run
[n:1] planner            complete (4.9s)
[n:2] researcher         complete (20.1s)
  └─ inserted token_miser (n:6) between n:2 → n:5
[n:3] researcher         complete (32.6s)
  └─ inserted token_miser (n:7) between n:3 → n:5
[n:4] researcher         complete (28.0s)
  └─ inserted token_miser (n:8) between n:4 → n:5
  └─ Token Miser: 848→549 chars (35% reduction)
[n:6] token_miser        complete (4.7s)
  └─ Token Miser: 732→259 chars (65% reduction)
[n:7] token_miser        complete (5.2s)
  └─ Token Miser: 737→306 chars (58% reduction)
[n:8] token_miser        complete (3.5s)
[n:5] formatter          complete (1.0s)

─────────────────────────────────────────────────────────────────────────────
TOKEN MISER — SESSION SUMMARY
─────────────────────────────────────────────────────────────────────────────
  Nodes compressed: 3
  Total input chars: 2,317
  Total output chars: 1,114
  Overall compression: 51.9%
  Gross chars saved (× downstream readers): 1,203
  Est. token savings: ~300
  Miser LLM cost (est. tokens): −0
  Net estimated token savings: ~300
─────────────────────────────────────────────────────────────────────────────

══════════════════════════════════════════════════════════════════════════════
FINAL: Based on current estimates for 2025-2026, here is the population and growth data for the three cities:

| City | Population (Est.) | Annual Growth Rate |
| :--- | :--- | :--- |
| Kinshasa | ~18.55 Million | 4.36% |
| Lagos | ~17.16 Million | 3.75% |
| Cairo | ~10.12 Million (City) | 1.07% |

Kinshasa is growing the fastest among the three, with an annual growth rate of 4.36%. 

Sources:
- Lagos: Macrotrends, NaijaDetails
- Cairo: World Population Review
- Kinshasa: Macrotrends
══════════════════════════════════════════════════════════════════════════════
```

**stderr:**
```
[06/10/26 01:17:39] INFO     Processing request of type            server.py:727
                             CallToolRequest                                    
[06/10/26 01:17:40] INFO     response:                                lib.rs:444
                             https://en.wikipedia.org/w/api.php?actio           
                             n=opensearch&profile=fuzzy&limit=1&searc           
                             h=current%20population%20and%20annual%20           
                             growth%20rate%20of%20Lagos%202024%202025           
                              200                                               
                    INFO     response:                                lib.rs:444
                             https://grokipedia.com/api/typeahead?que           
                             ry=current+population+and+annual+growth+           
                             rate+of+Lagos+2024+2025&limit=1 200                
                    INFO     response:                                lib.rs:444
                             https://search.brave.com/search?q=curren           
                             t+population+and+annual+growth+rate+of+L           
                             agos+2024+2025&source=web 429                      
[06/10/26 01:17:41] INFO     response:                                lib.rs:444
                             https://search.yahoo.com/search;_ylt=O-x           
                             Iai24mEizb2kZe3SZoVrg;_ylu=cX8hVNZuwKO4M           
                             RfX2PDwp6hqLQjNl3Kxl8iG6Pfu9caWPDo?p=cur           
                             rent+population+and+annual+growth+rate+o           
                             f+Lagos+2024+2025 200                              
                    INFO     Processing request of type            server.py:727
                             ListToolsRequest                                   
[06/10/26 01:17:43] INFO     Processing request of type            server.py:727
                             CallToolRequest                                    
[06/10/26 01:17:44] INFO     response:                                lib.rs:444
                             https://grokipedia.com/api/typeahead?que           
                             ry=current+population+and+annual+growth+           
                             rate+of+Cairo+2024+2025&limit=1 200                
                    INFO     response:                                lib.rs:444
                             https://en.wikipedia.org/w/api.php?actio           
                             n=opensearch&profile=fuzzy&limit=1&searc           
                             h=current%20population%20and%20annual%20           
                             growth%20rate%20of%20Cairo%202024%202025           
                              200                                               
[06/10/26 01:17:45] INFO     response:                                lib.rs:444
                             https://yandex.com/search/site/?text=cur           
                             rent+population+and+annual+growth+rate+o           
                             f+Cairo+2024+2025&web=1&searchid=4705590           
                              200                                               
                    INFO     Processing request of type            server.py:727
                             ListToolsRequest                                   
[06/10/26 01:17:47] INFO     Processing request of type            server.py:727
                             CallToolRequest                                    
[06/10/26 01:17:48] INFO     response:                                lib.rs:444
                             https://en.wikipedia.org/w/api.php?actio           
                             n=opensearch&profile=fuzzy&limit=1&searc           
                             h=current%20population%20and%20annual%20           
                             growth%20rate%20of%20Kinshasa%202024%202           
                             025 200                                            
                    INFO     response:                                lib.rs:444
                             https://grokipedia.com/api/typeahead?que           
                             ry=current+population+and+annual+growth+           
                             rate+of+Kinshasa+2024+2025&limit=1 200             
                    INFO     response:                                lib.rs:444
                             https://www.google.com/search?q=current+           
                             population+and+annual+growth+rate+of+Kin           
                             shasa+2024+2025&filter=1&start=0&hl=en-U           
                             S&lr=lang_en&cr=countryUS 200                      
[06/10/26 01:17:49] INFO     response: https://www.startpage.com/ 200 lib.rs:444
[06/10/26 01:17:50] INFO     response:                                lib.rs:444
                             https://www.startpage.com/sp/search 200            
                    INFO     Processing request of type            server.py:727
                             ListToolsRequest                                   
[06/10/26 01:17:51] INFO     Processing request of type            server.py:727
                             CallToolRequest                                    
[INIT].... → Crawl4AI 0.8.6 
[FETCH]... ↓ 
https://www.macrotrends.net/global-metrics/cities/20853/kinshasa/population     
| ✓ | ⏱: 1.23s 
[SCRAPE].. ◆ 
https://www.macrotrends.net/global-metrics/cities/20853/kinshasa/population     
| ✓ | ⏱: 0.01s 
[COMPLETE] ● 
https://www.macrotrends.net/global-metrics/cities/20853/kinshasa/population     
| ✓ | ⏱: 1.24s 
[06/10/26 01:17:59] INFO     Processing request of type            server.py:727
                             CallToolRequest                                    
[INIT].... → Crawl4AI 0.8.6 
[FETCH]... ↓ https://worldpopulationreview.com/cities/egypt/cairo               
| ✓ | ⏱: 1.15s 
[SCRAPE].. ◆ https://worldpopulationreview.com/cities/egypt/cairo               
| ✓ | ⏱: 0.02s 
[COMPLETE] ● https://worldpopulationreview.com/cities/egypt/cairo               
| ✓ | ⏱: 1.18s