# 實作計畫書：新增 Graphiti OSS + Neo4j 本地部署

## Context

目前 MiroFish 使用 Zep Cloud 作為知識圖譜記憶後端，需要外部 API Key、有費用且資料儲存於雲端。
本計畫在**不修改任何現有 Zep 相關程式碼**的前提下，新增 Graphiti OSS + Neo4j 作為可切換的本地替代後端，
透過 `.env` 的 `MEMORY_BACKEND` 旗標選擇使用哪個後端（預設保持 `zep`，向後相容）。

---

## 架構設計

```
MEMORY_BACKEND=zep (預設)      ←→     MEMORY_BACKEND=graphiti
      ↓                                       ↓
GraphBuilderService             GraphitiGraphBuilderService
ZepEntityReader                 GraphitiEntityReader
ZepGraphMemoryManager          GraphitiMemoryManager
ZepToolsService                 GraphitiToolsService
         \                             /
          ↘                           ↙
        memory_backend.py (Factory 工廠)
                   ↓
        API 路由 / 現有 Service 呼叫工廠取得對應實作
```

**切換原則**：
- 僅 `memory_backend.py` 知道要選哪個後端
- API 層只改呼叫工廠函式（每個整合點約 1~3 行改動）
- 現有 Zep 服務檔案**一行都不改**

---

## 待建立/修改檔案清單

### 新增（8 個檔案）
| 檔案路徑 | 對應的 Zep 版本 |
|---------|---------------|
| `backend/app/services/graphiti_client.py` | （新增：Graphiti 客戶端單例工廠）|
| `backend/app/utils/graphiti_paging.py` | `utils/zep_paging.py` |
| `backend/app/services/graphiti_graph_builder.py` | `services/graph_builder.py` |
| `backend/app/services/graphiti_entity_reader.py` | `services/zep_entity_reader.py` |
| `backend/app/services/graphiti_memory_updater.py` | `services/zep_graph_memory_updater.py` |
| `backend/app/services/graphiti_tools.py` | `services/zep_tools.py` |
| `backend/app/services/memory_backend.py` | （新增：後端選擇工廠）|

### 修改（5 個檔案）
| 檔案路徑 | 修改內容摘要 |
|---------|------------|
| `backend/requirements.txt` | 新增 `graphiti-core` |
| `backend/pyproject.toml` | 新增 `graphiti-core` |
| `.env.example` | 新增 `MEMORY_BACKEND`、`NEO4J_*` 設定 |
| `backend/app/config.py` | 新增 Neo4j 設定、`MEMORY_BACKEND`、更新 `validate()` |

### 最小改動（6 個現有 API/Service 檔案）
每個檔案僅改 import 及服務實例化方式（約 1~3 行）：

| 檔案路徑 | 現在 | 改為 |
|---------|------|-----|
| `backend/app/api/graph.py` | `GraphBuilderService(api_key=...)` | `memory_backend.get_graph_builder()` |
| `backend/app/api/simulation.py` | `ZepEntityReader()` | `memory_backend.get_entity_reader()` |
| `backend/app/services/simulation_manager.py` | `ZepEntityReader()` | `memory_backend.get_entity_reader()` |
| `backend/app/services/simulation_runner.py` | `ZepGraphMemoryManager` | `memory_backend.get_memory_manager()` |
| `backend/app/services/report_agent.py` | `ZepToolsService(...)` | `memory_backend.get_tools_service(...)` |
| `backend/app/api/report.py` | `ZepToolsService()` | `memory_backend.get_tools_service()` |

---

## 各步驟實作說明

### Step 1：Infrastructure — 原生 Neo4j 安裝

Neo4j 採用原生安裝方式，不透過 Docker，`docker-compose.yml` 中不包含 Neo4j 服務。

**macOS（Homebrew）：**
```bash
brew install neo4j

# 首次設定密碼（啟動前執行）
neo4j-admin dbms set-initial-password mirofish-neo4j

# 啟動
brew services start neo4j
```

**Linux（Debian/Ubuntu）：**
```bash
# 參考官方文件安裝：https://neo4j.com/docs/operations-manual/current/installation/linux/
neo4j-admin dbms set-initial-password mirofish-neo4j
sudo systemctl start neo4j
```

**Windows：**
從 https://neo4j.com/download/ 下載安裝包，安裝後使用 Neo4j Desktop 或服務管理器啟動。

**APOC 插件安裝（必須）：**
```bash
# 1. 從 https://github.com/neo4j/apoc/releases 下載與 Neo4j 版本對應的 apoc-*.jar
# 2. 放入 plugins/ 目錄：
#    macOS Homebrew：$(brew --prefix)/var/neo4j/plugins/
#    Linux：/var/lib/neo4j/plugins/
# 3. 在 neo4j.conf 中新增：
#    dbms.security.procedures.unrestricted=apoc.*
# 4. 重啟 Neo4j
```

**驗證：** 開啟 http://localhost:7474（Neo4j Browser），確認可以登入。

### Step 2：Dependencies

**requirements.txt** 新增：
```
graphiti-core>=0.3.0
neo4j>=5.0.0
```

**pyproject.toml** 同步新增相同依賴。

### Step 3：.env.example

新增區塊：
```bash
# ===== 記憶後端選擇 =====
# 可選值: zep (預設，使用 Zep Cloud) | graphiti (本地 Neo4j)
MEMORY_BACKEND=zep

# ===== Graphiti / Neo4j 本地設定 (MEMORY_BACKEND=graphiti 時必填) =====
NEO4J_URI=bolt://localhost:7687
NEO4J_USER=neo4j
NEO4J_PASSWORD=mirofish-neo4j
```

### Step 4：config.py

新增以下欄位：
```python
# 記憶後端
MEMORY_BACKEND = os.environ.get('MEMORY_BACKEND', 'zep')  # 'zep' | 'graphiti'

# Neo4j 設定（MEMORY_BACKEND=graphiti 時使用）
NEO4J_URI = os.environ.get('NEO4J_URI', 'bolt://localhost:7687')
NEO4J_USER = os.environ.get('NEO4J_USER', 'neo4j')
NEO4J_PASSWORD = os.environ.get('NEO4J_PASSWORD', 'mirofish-neo4j')
```

**修改 `validate()`**：
```python
@classmethod
def validate(cls):
    errors = []
    if not cls.LLM_API_KEY:
        errors.append("LLM_API_KEY 未配置")
    if cls.MEMORY_BACKEND == 'zep' and not cls.ZEP_API_KEY:
        errors.append("ZEP_API_KEY 未配置（MEMORY_BACKEND=zep 時必填）")
    if cls.MEMORY_BACKEND == 'graphiti' and not cls.NEO4J_PASSWORD:
        errors.append("NEO4J_PASSWORD 未配置（MEMORY_BACKEND=graphiti 時必填）")
    return errors
```

---

### Step 5：graphiti_client.py（Graphiti 客戶端工廠）

管理 Graphiti 實例和 Neo4j driver 的生命週期：

```python
# backend/app/services/graphiti_client.py
"""
Graphiti 客戶端工廠
管理 Graphiti 實例和 Neo4j driver 的生命週期
"""
import asyncio
from typing import Optional
from graphiti_core import Graphiti
from graphiti_core.llm_client.openai_client import OpenAIClient
from graphiti_core.embedder.openai import OpenAIEmbedder, OpenAIEmbedderConfig
from ..config import Config

async def create_graphiti() -> Graphiti:
    """建立新的 Graphiti 實例（每次呼叫建立新實例，避免 event loop 綁定問題）"""
    llm_client = OpenAIClient(
        api_key=Config.LLM_API_KEY,
        base_url=Config.LLM_BASE_URL,
        model=Config.LLM_MODEL_NAME,
    )
    embedder = OpenAIEmbedder(
        OpenAIEmbedderConfig(
            api_key=Config.LLM_API_KEY,
            base_url=Config.LLM_BASE_URL,
        )
    )
    g = Graphiti(
        uri=Config.NEO4J_URI,
        user=Config.NEO4J_USER,
        password=Config.NEO4J_PASSWORD,
        llm_client=llm_client,
        embedder=embedder,
    )
    return g

def get_neo4j_driver():
    """取得 Neo4j Driver（用於直接 Cypher 查詢）"""
    from neo4j import GraphDatabase
    return GraphDatabase.driver(
        Config.NEO4J_URI,
        auth=(Config.NEO4J_USER, Config.NEO4J_PASSWORD)
    )

def run_async(coro):
    """在同步 Flask 環境中執行 async 函式"""
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor() as pool:
        future = pool.submit(asyncio.run, coro)
        return future.result()
```

### Step 6：graphiti_paging.py

**對應**：`backend/app/utils/zep_paging.py`

實作相同的 `fetch_all_nodes(graphiti, group_id)` 和 `fetch_all_edges(graphiti, group_id)` 介面，
內部使用 Neo4j Cypher 查詢（SKIP/LIMIT 分頁）替代 Zep 的 UUID cursor：

```python
# 核心 Cypher 查詢
NODES_QUERY = """
MATCH (n:Entity {group_id: $group_id})
RETURN n
ORDER BY n.uuid
SKIP $skip LIMIT $limit
"""

EDGES_QUERY = """
MATCH ()-[r:RELATES_TO {group_id: $group_id}]-()
RETURN DISTINCT r
ORDER BY r.uuid
SKIP $skip LIMIT $limit
"""
```

回傳格式對齊 `zep_paging.py` 的回傳值，確保 `graphiti_graph_builder.py` 和 `graphiti_entity_reader.py` 能直接使用。

### Step 7：graphiti_graph_builder.py

**對應**：`backend/app/services/graph_builder.py`

保持完全相同的公開介面：
- `build_graph_async(text, ontology, graph_name, chunk_size, chunk_overlap, batch_size)` → 回傳 `task_id`
- `get_graph_info(graph_id)` → 回傳 `GraphInfo`
- `get_graph_data(graph_id)` → 回傳相同結構的 dict
- `delete_graph(graph_id)`

**關鍵實作差異**：
- `create_graph()`: 本地生成 UUID 作為 `group_id`，不需呼叫 Zep
- `set_ontology()`: 暫時略過（Graphiti 由 LLM 自動決定實體類型），後續可擴充
- `add_text_batches()`: 改呼叫 `await graphiti.add_episode(group_id=graph_id, ...)`
- `_wait_for_episodes()`: 不需要（`add_episode` 是 await 同步完成，無需輪詢）
- `delete_graph()`: 執行 Cypher `MATCH (n {group_id: $gid}) DETACH DELETE n`

**Async 處理**：所有 Graphiti 呼叫包在 `run_async()` 輔助函式內，維持現有背景執行緒模式。

### Step 8：graphiti_entity_reader.py

**對應**：`backend/app/services/zep_entity_reader.py`

保持相同公開介面與回傳型別（`EntityNode`、`FilteredEntities`）：
- `filter_defined_entities(graph_id, defined_entity_types, enrich_with_edges)`
- `get_entity_with_context(graph_id, entity_uuid)`
- `get_entities_by_type(graph_id, entity_type)`

**實作方式**：用 `graphiti_paging.fetch_all_nodes()` 取得節點，
節點的 `labels` 從 Neo4j 節點的 labels 取得（去掉 `Entity` 後即為自定義類型）。

### Step 9：graphiti_memory_updater.py

**對應**：`backend/app/services/zep_graph_memory_updater.py`

保持相同的 `GraphitiMemoryManager` class method 介面：
- `create_updater(simulation_id, graph_id)`
- `get_updater(simulation_id)`
- `stop_updater(simulation_id)`
- `stop_all()`

**實作方式**：複用 `AgentActivity` 資料類別（或直接 import）。
內部改呼叫 `await graphiti.add_episode(group_id=graph_id, episode_body=text)` 送出 batch。

### Step 10：graphiti_tools.py

**對應**：`backend/app/services/zep_tools.py`

保持相同公開介面與回傳型別（`SearchResult`、`InsightForgeResult`、`PanoramaResult`、`InterviewResult`）：
- `search_graph(graph_id, query, limit)` → 呼叫 `await graphiti.search(query, group_ids=[graph_id])`
- `insight_forge(graph_id, topic)` → 拆解子查詢後多次呼叫 search，組合結果
- `panorama_search(graph_id, topic)` → 包含 expired 邊的寬範圍查詢（Cypher 直接查）
- `quick_search(graph_id, query)` → 單次 graphiti.search
- `interview_agents(graph_id, agents, questions)` → 沿用現有 LLM interview 邏輯，資料來源改為 Graphiti

**搜尋結果對應**：Graphiti search 回傳 `EntityEdge` list，內含 `fact`、`source_node`、`target_node`、`valid_at`、`invalid_at`。

### Step 11：memory_backend.py（工廠模組）

```python
# backend/app/services/memory_backend.py
"""
記憶後端工廠：根據 Config.MEMORY_BACKEND 回傳正確的服務實作
"""
from ..config import Config

def get_graph_builder():
    if Config.MEMORY_BACKEND == 'graphiti':
        from .graphiti_graph_builder import GraphitiGraphBuilderService
        return GraphitiGraphBuilderService()
    from .graph_builder import GraphBuilderService
    return GraphBuilderService(api_key=Config.ZEP_API_KEY)

def get_entity_reader():
    if Config.MEMORY_BACKEND == 'graphiti':
        from .graphiti_entity_reader import GraphitiEntityReader
        return GraphitiEntityReader()
    from .zep_entity_reader import ZepEntityReader
    return ZepEntityReader()

def get_memory_manager():
    """回傳 Manager class（非實例），呼叫方使用 class method"""
    if Config.MEMORY_BACKEND == 'graphiti':
        from .graphiti_memory_updater import GraphitiMemoryManager
        return GraphitiMemoryManager
    from .zep_graph_memory_updater import ZepGraphMemoryManager
    return ZepGraphMemoryManager

def get_tools_service(api_key=None, llm_client=None):
    if Config.MEMORY_BACKEND == 'graphiti':
        from .graphiti_tools import GraphitiToolsService
        return GraphitiToolsService(llm_client=llm_client)
    from .zep_tools import ZepToolsService
    return ZepToolsService(api_key=api_key, llm_client=llm_client)
```

### Step 12：整合點修改（6 個檔案，最小改動）

**`api/graph.py`**（2 行改動）：
```python
# 原：from ..services.graph_builder import GraphBuilderService
# 改：
from ..services.memory_backend import get_graph_builder
# 原：builder = GraphBuilderService(api_key=Config.ZEP_API_KEY)
# 改：
builder = get_graph_builder()
```

**`api/simulation.py`**（2 行改動）：
```python
# 原：from ..services.zep_entity_reader import ZepEntityReader
# 改：
from ..services.memory_backend import get_entity_reader
# 原：reader = ZepEntityReader()
# 改：reader = get_entity_reader()
```

**`services/simulation_manager.py`**（2 行改動）：
```python
# 原：from .zep_entity_reader import ZepEntityReader, FilteredEntities
# 改：
from .memory_backend import get_entity_reader
# 原：reader = ZepEntityReader()
# 改：reader = get_entity_reader()
```

**`services/simulation_runner.py`**（2 行改動）：
```python
# 原：from .zep_graph_memory_updater import ZepGraphMemoryManager
# 改：
from .memory_backend import get_memory_manager
# 原：ZepGraphMemoryManager.create_updater / get_updater / stop_updater / stop_all
# 改：get_memory_manager().create_updater / get_updater / stop_updater / stop_all
```

**`services/report_agent.py`**（依實際呼叫位置調整，約 2~3 行改動）：
```python
# 原：from .zep_tools import ZepToolsService, ...
# 改：
from .memory_backend import get_tools_service
# 並在需要時呼叫 get_tools_service(llm_client=self._llm_client)
```

**`api/report.py`**（2 行改動）：
```python
# 原：from ..services.zep_tools import ZepToolsService
# 改：
from ..services.memory_backend import get_tools_service
# 原：tools = ZepToolsService()
# 改：tools = get_tools_service()
```

---

## 驗證計畫

### 環境啟動
```bash
# 1. 啟動 Neo4j（原生安裝，非 Docker）
brew services start neo4j      # macOS
# 或：sudo systemctl start neo4j  # Linux

# 2. 設定 .env
MEMORY_BACKEND=graphiti
NEO4J_URI=bolt://localhost:7687
NEO4J_PASSWORD=mirofish-neo4j

# 3. 啟動後端
cd backend && python run.py
```

### 功能驗證（逐步）
1. **建圖**：上傳文字檔 → 觸發 graph build → 確認 Neo4j Browser (http://localhost:7474) 中出現節點/邊
2. **實體讀取**：呼叫實體讀取 API，確認回傳 `entities` 非空
3. **模擬記憶更新**：啟動 `enable_graph_memory_update=true` 的模擬，確認 Neo4j 中 episode 節點增加
4. **搜尋**：呼叫 report API，確認 GraphitiToolsService.search_graph 有回傳結果
5. **回退測試**：切換 `MEMORY_BACKEND=zep`，確認 ZEP_API_KEY 驗證恢復運作

### 切換回 Zep 驗證
確認 `MEMORY_BACKEND=zep` 下所有原始功能完全不受影響。

---

## 注意事項與已知限制

1. **Ontology 支援**：Graphiti OSS 實體類型由 LLM 自動推斷，不受原始 ontology 嚴格約束，初期可接受，後續可加強
2. **Async 橋接**：Flask 為同步框架，所有 Graphiti 呼叫透過 `run_async()` 在背景執行緒中執行
3. **Structured Output**：Graphiti 倚賴 LLM structured output 抽取實體；若使用 qwen-plus 等非 OpenAI 模型，需測試相容性
4. **首次建圖較慢**：每個文字 chunk 都會觸發多次 LLM 呼叫（實體抽取、去重、時序更新），比 Zep Cloud 慢
5. **Neo4j schema 確認**：Cypher 查詢中的節點 label 和屬性名稱（如 `group_id`、`RELATES_TO`）需在安裝 graphiti-core 後實際驗證
