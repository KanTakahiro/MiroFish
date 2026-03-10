"""
Graphiti 圖譜分頁讀取工具。

使用同步 Neo4j Driver 透過 Cypher SKIP/LIMIT 分頁查詢，
提供與 zep_paging.py 平行的介面，回傳結構化的節點/邊物件。

注意：Neo4j schema 中節點用 :Entity label，邊用 :RELATES_TO 關係類型。
"""

from __future__ import annotations

from typing import Any, List

from .logger import get_logger

logger = get_logger('mirofish.graphiti_paging')

_DEFAULT_PAGE_SIZE = 100
_MAX_NODES = 2000

# Cypher 查詢
_NODES_QUERY = """
MATCH (n:Entity {group_id: $group_id})
RETURN n, labels(n) AS node_labels
ORDER BY n.uuid
SKIP $skip LIMIT $limit
"""

_EDGES_QUERY = """
MATCH (s:Entity {group_id: $group_id})-[r:RELATES_TO {group_id: $group_id}]->(t:Entity {group_id: $group_id})
RETURN DISTINCT r,
       coalesce(r.source_node_uuid, s.uuid) AS source_uuid,
       coalesce(r.target_node_uuid, t.uuid) AS target_uuid
ORDER BY r.uuid
SKIP $skip LIMIT $limit
"""


class _NodeRecord:
    """模擬 Zep Node 屬性結構，供 Graphiti 服務層使用"""

    def __init__(self, data: dict) -> None:
        n: dict = data.get('n') or {}
        node_labels: list = data.get('node_labels') or []

        self.uuid_ = n.get('uuid') or ''
        self.uuid = self.uuid_
        self.name = n.get('name') or ''
        self.labels = list(node_labels)
        self.summary = n.get('summary') or ''
        self.attributes: dict = {}
        self.created_at = n.get('created_at')


class _EdgeRecord:
    """模擬 Zep Edge 屬性結構，供 Graphiti 服務層使用"""

    def __init__(self, data: dict) -> None:
        r: dict = data.get('r') or {}

        self.uuid_ = r.get('uuid') or ''
        self.uuid = self.uuid_
        self.name = r.get('name') or ''
        self.fact = r.get('fact') or ''
        self.source_node_uuid = data.get('source_uuid') or r.get('source_node_uuid') or ''
        self.target_node_uuid = data.get('target_uuid') or r.get('target_node_uuid') or ''
        self.attributes: dict = {}
        self.created_at = r.get('created_at')
        self.valid_at = r.get('valid_at')
        self.invalid_at = r.get('invalid_at')
        self.expired_at = r.get('expired_at')


def fetch_all_nodes(
    driver,
    group_id: str,
    page_size: int = _DEFAULT_PAGE_SIZE,
    max_items: int = _MAX_NODES,
) -> List[_NodeRecord]:
    """
    分頁取得圖譜節點，最多回傳 max_items 筆。
    driver 為同步 neo4j.GraphDatabase.driver() 實例。
    """
    all_nodes: List[_NodeRecord] = []
    skip = 0

    with driver.session() as session:
        while True:
            result = session.run(
                _NODES_QUERY,
                group_id=group_id,
                skip=skip,
                limit=page_size,
            )
            records = [rec.data() for rec in result]

            if not records:
                break

            for rec in records:
                all_nodes.append(_NodeRecord(rec))

            if len(all_nodes) >= max_items:
                all_nodes = all_nodes[:max_items]
                logger.warning(
                    f"節點數量達到上限 ({max_items})，停止分頁 (group={group_id})"
                )
                break

            if len(records) < page_size:
                break

            skip += page_size

    return all_nodes


def fetch_all_edges(
    driver,
    group_id: str,
    page_size: int = _DEFAULT_PAGE_SIZE,
) -> List[_EdgeRecord]:
    """
    分頁取得圖譜所有邊。
    driver 為同步 neo4j.GraphDatabase.driver() 實例。
    """
    all_edges: List[_EdgeRecord] = []
    skip = 0

    with driver.session() as session:
        while True:
            result = session.run(
                _EDGES_QUERY,
                group_id=group_id,
                skip=skip,
                limit=page_size,
            )
            records = [rec.data() for rec in result]

            if not records:
                break

            for rec in records:
                all_edges.append(_EdgeRecord(rec))

            if len(records) < page_size:
                break

            skip += page_size

    return all_edges
