"""
Graphiti 實體讀取與過濾服務
對應 services/zep_entity_reader.py，使用 Graphiti OSS + Neo4j 作為後端。

回傳的資料結構（EntityNode、FilteredEntities）與 ZepEntityReader 完全相同，
直接從 zep_entity_reader 模組匯入，確保呼叫端無縫切換。
"""

from typing import Dict, Any, List, Optional, Set

from ..utils.graphiti_paging import fetch_all_nodes, fetch_all_edges
from ..utils.logger import get_logger
from .zep_entity_reader import EntityNode, FilteredEntities

logger = get_logger('mirofish.graphiti_entity_reader')

# Graphiti / Neo4j 內建標籤，不視為自定義實體類型
_BUILTIN_LABELS = frozenset({"Entity", "Node", "Episodic"})


class GraphitiEntityReader:
    """
    Graphiti 實體讀取與過濾服務。
    公開介面與 ZepEntityReader 完全相同。
    """

    def __init__(self) -> None:
        pass

    # ──────────────────────────────────────────────
    # 內部輔助方法
    # ──────────────────────────────────────────────

    def _get_driver(self):
        from .graphiti_client import get_neo4j_driver
        return get_neo4j_driver()

    def get_all_nodes(self, graph_id: str) -> List[Dict[str, Any]]:
        """取得圖譜所有節點（分頁）"""
        driver = self._get_driver()
        try:
            nodes = fetch_all_nodes(driver, graph_id)
            result = [
                {
                    "uuid": node.uuid_ or "",
                    "name": node.name or "",
                    "labels": node.labels or [],
                    "summary": node.summary or "",
                    "attributes": {},
                }
                for node in nodes
            ]
            logger.info(f"共取得 {len(result)} 個節點 (group={graph_id})")
            return result
        finally:
            driver.close()

    def get_all_edges(self, graph_id: str) -> List[Dict[str, Any]]:
        """取得圖譜所有邊（分頁）"""
        driver = self._get_driver()
        try:
            edges = fetch_all_edges(driver, graph_id)
            result = [
                {
                    "uuid": edge.uuid_ or "",
                    "name": edge.name or "",
                    "fact": edge.fact or "",
                    "source_node_uuid": edge.source_node_uuid or "",
                    "target_node_uuid": edge.target_node_uuid or "",
                    "attributes": {},
                }
                for edge in edges
            ]
            logger.info(f"共取得 {len(result)} 條邊 (group={graph_id})")
            return result
        finally:
            driver.close()

    # ──────────────────────────────────────────────
    # 公開方法（與 ZepEntityReader 介面一致）
    # ──────────────────────────────────────────────

    def filter_defined_entities(
        self,
        graph_id: str,
        defined_entity_types: Optional[List[str]] = None,
        enrich_with_edges: bool = True,
    ) -> FilteredEntities:
        """
        篩選符合預定義實體類型的節點。

        篩選邏輯與 ZepEntityReader 相同：
        - Labels 中除 Entity/Node/Episodic 外無其他標籤 → 跳過
        - 有自定義標籤且符合 defined_entity_types（若提供）→ 保留
        """
        logger.info(f"開始篩選圖譜 {graph_id} 的實體...")

        all_nodes = self.get_all_nodes(graph_id)
        total_count = len(all_nodes)
        all_edges = self.get_all_edges(graph_id) if enrich_with_edges else []
        node_map = {n["uuid"]: n for n in all_nodes}

        filtered_entities: List[EntityNode] = []
        entity_types_found: Set[str] = set()

        for node in all_nodes:
            labels = node.get("labels", [])
            custom_labels = [l for l in labels if l not in _BUILTIN_LABELS]

            if not custom_labels:
                continue

            if defined_entity_types:
                matching = [l for l in custom_labels if l in defined_entity_types]
                if not matching:
                    continue
                entity_type = matching[0]
            else:
                entity_type = custom_labels[0]

            entity_types_found.add(entity_type)

            entity = EntityNode(
                uuid=node["uuid"],
                name=node["name"],
                labels=labels,
                summary=node["summary"],
                attributes=node["attributes"],
            )

            if enrich_with_edges:
                related_edges = []
                related_node_uuids: Set[str] = set()

                for edge in all_edges:
                    if edge["source_node_uuid"] == node["uuid"]:
                        related_edges.append(
                            {
                                "direction": "outgoing",
                                "edge_name": edge["name"],
                                "fact": edge["fact"],
                                "target_node_uuid": edge["target_node_uuid"],
                            }
                        )
                        related_node_uuids.add(edge["target_node_uuid"])
                    elif edge["target_node_uuid"] == node["uuid"]:
                        related_edges.append(
                            {
                                "direction": "incoming",
                                "edge_name": edge["name"],
                                "fact": edge["fact"],
                                "source_node_uuid": edge["source_node_uuid"],
                            }
                        )
                        related_node_uuids.add(edge["source_node_uuid"])

                entity.related_edges = related_edges
                entity.related_nodes = [
                    {
                        "uuid": node_map[uid]["uuid"],
                        "name": node_map[uid]["name"],
                        "labels": node_map[uid]["labels"],
                        "summary": node_map[uid].get("summary", ""),
                    }
                    for uid in related_node_uuids
                    if uid in node_map
                ]

            filtered_entities.append(entity)

        logger.info(
            f"篩選完成: 總節點 {total_count}, 符合條件 {len(filtered_entities)}, "
            f"實體類型: {entity_types_found}"
        )

        return FilteredEntities(
            entities=filtered_entities,
            entity_types=entity_types_found,
            total_count=total_count,
            filtered_count=len(filtered_entities),
        )

    def get_entity_with_context(
        self,
        graph_id: str,
        entity_uuid: str,
    ) -> Optional[EntityNode]:
        """取得單個實體及其完整上下文（邊和關聯節點）"""
        try:
            all_nodes = self.get_all_nodes(graph_id)
            node_data = next((n for n in all_nodes if n["uuid"] == entity_uuid), None)
            if not node_data:
                return None

            all_edges = self.get_all_edges(graph_id)
            node_map = {n["uuid"]: n for n in all_nodes}

            related_edges = []
            related_node_uuids: Set[str] = set()

            for edge in all_edges:
                if edge["source_node_uuid"] == entity_uuid:
                    related_edges.append(
                        {
                            "direction": "outgoing",
                            "edge_name": edge["name"],
                            "fact": edge["fact"],
                            "target_node_uuid": edge["target_node_uuid"],
                        }
                    )
                    related_node_uuids.add(edge["target_node_uuid"])
                elif edge["target_node_uuid"] == entity_uuid:
                    related_edges.append(
                        {
                            "direction": "incoming",
                            "edge_name": edge["name"],
                            "fact": edge["fact"],
                            "source_node_uuid": edge["source_node_uuid"],
                        }
                    )
                    related_node_uuids.add(edge["source_node_uuid"])

            related_nodes = [
                {
                    "uuid": node_map[uid]["uuid"],
                    "name": node_map[uid]["name"],
                    "labels": node_map[uid]["labels"],
                    "summary": node_map[uid].get("summary", ""),
                }
                for uid in related_node_uuids
                if uid in node_map
            ]

            return EntityNode(
                uuid=node_data["uuid"],
                name=node_data["name"],
                labels=node_data["labels"],
                summary=node_data["summary"],
                attributes=node_data["attributes"],
                related_edges=related_edges,
                related_nodes=related_nodes,
            )

        except Exception as e:
            logger.error(f"取得實體 {entity_uuid} 失敗: {str(e)}")
            return None

    def get_entities_by_type(
        self,
        graph_id: str,
        entity_type: str,
        enrich_with_edges: bool = True,
    ) -> List[EntityNode]:
        """取得指定類型的所有實體"""
        result = self.filter_defined_entities(
            graph_id=graph_id,
            defined_entity_types=[entity_type],
            enrich_with_edges=enrich_with_edges,
        )
        return result.entities
