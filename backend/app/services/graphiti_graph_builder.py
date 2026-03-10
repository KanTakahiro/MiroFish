"""
Graphiti 圖譜構建服務
對應 services/graph_builder.py，使用 Graphiti OSS + Neo4j 作為後端。

主要差異：
- group_id 本地生成（UUID），無需呼叫外部 API
- set_ontology 為 no-op（Graphiti 由 LLM 自動推斷實體類型）
- add_episode 同步完成，無需輪詢等待
"""

import uuid
import threading
from typing import Dict, Any, List, Optional, Callable
from datetime import datetime, timezone

from ..config import Config
from ..models.task import TaskManager, TaskStatus
from ..utils.graphiti_paging import fetch_all_nodes, fetch_all_edges
from ..utils.logger import get_logger
from .graph_builder import GraphInfo
from .text_processor import TextProcessor

logger = get_logger('mirofish.graphiti_graph_builder')


class GraphitiGraphBuilderService:
    """
    Graphiti 圖譜構建服務。
    公開介面與 GraphBuilderService 完全相同，後端改為 Graphiti OSS + Neo4j。
    """

    def __init__(self) -> None:
        self.task_manager = TaskManager()

    # ──────────────────────────────────────────────
    # 非同步構建入口（供 api/graph.py build_task 使用）
    # ──────────────────────────────────────────────

    def build_graph_async(
        self,
        text: str,
        ontology: Dict[str, Any],
        graph_name: str = "MiroFish Graph",
        chunk_size: int = 500,
        chunk_overlap: int = 50,
        batch_size: int = 3,
    ) -> str:
        """異步構建圖譜，回傳 task_id"""
        task_id = self.task_manager.create_task(
            task_type="graph_build",
            metadata={
                "graph_name": graph_name,
                "chunk_size": chunk_size,
                "text_length": len(text),
            },
        )
        thread = threading.Thread(
            target=self._build_graph_worker,
            args=(task_id, text, ontology, graph_name, chunk_size, chunk_overlap, batch_size),
            daemon=True,
        )
        thread.start()
        return task_id

    def _build_graph_worker(
        self,
        task_id: str,
        text: str,
        ontology: Dict[str, Any],
        graph_name: str,
        chunk_size: int,
        chunk_overlap: int,
        batch_size: int,
    ) -> None:
        """圖譜構建工作執行緒（與 GraphBuilderService._build_graph_worker 同樣流程）"""
        try:
            self.task_manager.update_task(
                task_id, status=TaskStatus.PROCESSING, progress=5, message="開始構建圖譜..."
            )

            graph_id = self.create_graph(graph_name)
            self.task_manager.update_task(task_id, progress=10, message=f"圖譜已創建: {graph_id}")

            self.set_ontology(graph_id, ontology)
            self.task_manager.update_task(task_id, progress=15, message="本體設置完成（Graphiti 自動推斷）")

            chunks = TextProcessor.split_text(text, chunk_size, chunk_overlap)
            total_chunks = len(chunks)
            self.task_manager.update_task(
                task_id, progress=20, message=f"文本已分割為 {total_chunks} 個塊"
            )

            episode_uuids = self.add_text_batches(
                graph_id,
                chunks,
                batch_size,
                progress_callback=lambda msg, prog: self.task_manager.update_task(
                    task_id,
                    progress=20 + int(prog * 0.60),  # 20–80%
                    message=msg,
                ),
            )

            self.task_manager.update_task(task_id, progress=80, message="等待 Graphiti 處理完成...")
            self._wait_for_episodes(
                episode_uuids,
                progress_callback=lambda msg, prog: self.task_manager.update_task(
                    task_id,
                    progress=80 + int(prog * 0.10),  # 80–90%
                    message=msg,
                ),
            )

            self.task_manager.update_task(task_id, progress=90, message="獲取圖譜信息...")
            graph_info = self._get_graph_info(graph_id)

            self.task_manager.complete_task(
                task_id,
                {
                    "graph_id": graph_id,
                    "graph_info": graph_info.to_dict(),
                    "chunks_processed": total_chunks,
                },
            )

        except Exception as e:
            import traceback

            self.task_manager.fail_task(task_id, f"{str(e)}\n{traceback.format_exc()}")

    # ──────────────────────────────────────────────
    # 公開方法（供 api/graph.py 直接呼叫）
    # ──────────────────────────────────────────────

    def create_graph(self, name: str) -> str:
        """本地生成 graph_id（即 Graphiti 的 group_id），並確保 Neo4j 索引已建立"""
        from .graphiti_client import ensure_indices

        ensure_indices()
        return f"mirofish_{uuid.uuid4().hex[:16]}"

    def set_ontology(self, graph_id: str, ontology: Dict[str, Any]) -> None:
        """
        No-op：Graphiti 由 LLM 自動推斷實體類型，不需預設 ontology。
        保留此方法以維持與 GraphBuilderService 相同的呼叫介面。
        """
        pass

    def add_text_batches(
        self,
        graph_id: str,
        chunks: List[str],
        batch_size: int = 3,
        progress_callback: Optional[Callable] = None,
    ) -> List[str]:
        """
        將文本塊逐一發送到 Graphiti 作為 episode。
        add_episode 同步完成，無需輪詢等待。
        """
        from graphiti_core.nodes import EpisodeType
        from .graphiti_client import run_async, create_graphiti, _close_graphiti

        total = len(chunks)
        episode_uuids: List[str] = []

        for i, chunk in enumerate(chunks):
            if progress_callback:
                progress_callback(
                    f"處理第 {i + 1}/{total} 個文本塊（Graphiti 抽取實體中）...",
                    (i + 1) / total,
                )

            chunk_text = chunk
            idx = i

            async def _add_episode(text=chunk_text, ep_idx=idx):
                g = await create_graphiti()
                try:
                    await g.add_episode(
                        name=f"chunk_{ep_idx}",
                        episode_body=text,
                        source=EpisodeType.text,
                        source_description="MiroFish document chunk",
                        group_id=graph_id,
                        reference_time=datetime.now(timezone.utc),
                    )
                finally:
                    await _close_graphiti(g)

            try:
                run_async(_add_episode())
                episode_uuids.append(f"graphiti_ep_{graph_id}_{i}")
            except Exception as e:
                logger.error(f"添加第 {i + 1} 個文本塊失敗: {e}")
                if progress_callback:
                    progress_callback(f"塊 {i + 1} 添加失敗: {str(e)}", (i + 1) / total)
                raise

        return episode_uuids

    def _wait_for_episodes(
        self,
        episode_uuids: List[str],
        progress_callback: Optional[Callable] = None,
        timeout: int = 600,
    ) -> None:
        """Graphiti add_episode 同步完成，此方法為 no-op"""
        if progress_callback:
            progress_callback("Graphiti 處理完成（無需等待）", 1.0)

    def _get_graph_info(self, graph_id: str) -> GraphInfo:
        """查詢 Neo4j 取得圖譜統計資訊"""
        from .graphiti_client import get_neo4j_driver

        driver = get_neo4j_driver()
        try:
            nodes = fetch_all_nodes(driver, graph_id)
            edges = fetch_all_edges(driver, graph_id)

            entity_types = set()
            for node in nodes:
                for label in node.labels:
                    if label not in ("Entity", "Node", "Episodic"):
                        entity_types.add(label)

            return GraphInfo(
                graph_id=graph_id,
                node_count=len(nodes),
                edge_count=len(edges),
                entity_types=list(entity_types),
            )
        finally:
            driver.close()

    def get_graph_data(self, graph_id: str) -> Dict[str, Any]:
        """取得完整圖譜資料（節點與邊），格式與 GraphBuilderService.get_graph_data 相同"""
        from .graphiti_client import get_neo4j_driver

        driver = get_neo4j_driver()
        try:
            nodes = fetch_all_nodes(driver, graph_id)
            edges = fetch_all_edges(driver, graph_id)

            node_map = {node.uuid_: node.name for node in nodes}

            nodes_data = [
                {
                    "uuid": node.uuid_,
                    "name": node.name,
                    "labels": node.labels or [],
                    "summary": node.summary or "",
                    "attributes": {},
                    "created_at": str(node.created_at) if node.created_at else None,
                }
                for node in nodes
            ]

            edges_data = [
                {
                    "uuid": edge.uuid_,
                    "name": edge.name or "",
                    "fact": edge.fact or "",
                    "fact_type": edge.name or "",
                    "source_node_uuid": edge.source_node_uuid,
                    "target_node_uuid": edge.target_node_uuid,
                    "source_node_name": node_map.get(edge.source_node_uuid, ""),
                    "target_node_name": node_map.get(edge.target_node_uuid, ""),
                    "attributes": {},
                    "created_at": str(edge.created_at) if edge.created_at else None,
                    "valid_at": str(edge.valid_at) if edge.valid_at else None,
                    "invalid_at": str(edge.invalid_at) if edge.invalid_at else None,
                    "expired_at": str(edge.expired_at) if edge.expired_at else None,
                    "episodes": [],
                }
                for edge in edges
            ]

            return {
                "graph_id": graph_id,
                "nodes": nodes_data,
                "edges": edges_data,
                "node_count": len(nodes_data),
                "edge_count": len(edges_data),
            }
        finally:
            driver.close()

    def delete_graph(self, graph_id: str) -> None:
        """刪除指定 group_id 的所有節點與關係"""
        from .graphiti_client import get_neo4j_driver

        driver = get_neo4j_driver()
        try:
            with driver.session() as session:
                session.run(
                    "MATCH (n {group_id: $gid}) DETACH DELETE n",
                    gid=graph_id,
                )
            logger.info(f"已刪除圖譜: {graph_id}")
        finally:
            driver.close()
