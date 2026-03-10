"""
Graphiti 圖譜記憶更新服務
對應 services/zep_graph_memory_updater.py，使用 Graphiti OSS + Neo4j 作為後端。

直接複用 AgentActivity 資料類別（從 zep_graph_memory_updater 匯入），
確保 SimulationRunner 的呼叫端無需修改。
"""

import threading
from datetime import datetime
from queue import Empty, Queue
from typing import Any, Dict, List, Optional

from ..utils.logger import get_logger
from .zep_graph_memory_updater import AgentActivity

logger = get_logger('mirofish.graphiti_memory_updater')


class GraphitiGraphMemoryUpdater:
    """
    Graphiti 圖譜記憶更新器。
    公開介面與 ZepGraphMemoryUpdater 完全相同。
    """

    BATCH_SIZE = 5
    PLATFORM_DISPLAY_NAMES = {
        'twitter': '世界1',
        'reddit': '世界2',
    }
    SEND_INTERVAL = 0.5
    MAX_RETRIES = 3
    RETRY_DELAY = 2

    def __init__(self, graph_id: str) -> None:
        self.graph_id = graph_id
        self._activity_queue: Queue = Queue()
        self._platform_buffers: Dict[str, List[AgentActivity]] = {
            'twitter': [],
            'reddit': [],
        }
        self._buffer_lock = threading.Lock()
        self._running = False
        self._worker_thread: Optional[threading.Thread] = None

        self._total_activities = 0
        self._total_sent = 0
        self._total_items_sent = 0
        self._failed_count = 0
        self._skipped_count = 0

        logger.info(f"GraphitiGraphMemoryUpdater 初始化完成: graph_id={graph_id}")

    def start(self) -> None:
        """啟動後台工作執行緒"""
        if self._running:
            return
        self._running = True
        self._worker_thread = threading.Thread(
            target=self._worker_loop,
            daemon=True,
            name=f"GraphitiMemoryUpdater-{self.graph_id[:8]}",
        )
        self._worker_thread.start()
        logger.info(f"GraphitiGraphMemoryUpdater 已啟動: graph_id={self.graph_id}")

    def stop(self) -> None:
        """停止後台工作執行緒並發送剩餘活動"""
        self._running = False
        self._flush_remaining()
        if self._worker_thread and self._worker_thread.is_alive():
            self._worker_thread.join(timeout=10)
        logger.info(
            f"GraphitiGraphMemoryUpdater 已停止: graph_id={self.graph_id}, "
            f"total_activities={self._total_activities}, "
            f"batches_sent={self._total_sent}, "
            f"items_sent={self._total_items_sent}, "
            f"failed={self._failed_count}, "
            f"skipped={self._skipped_count}"
        )

    def add_activity(self, activity: AgentActivity) -> None:
        """添加 Agent 活動到處理隊列（跳過 DO_NOTHING）"""
        if activity.action_type == "DO_NOTHING":
            self._skipped_count += 1
            return
        self._activity_queue.put(activity)
        self._total_activities += 1
        logger.debug(f"添加活動到 Graphiti 隊列: {activity.agent_name} - {activity.action_type}")

    def add_activity_from_dict(self, data: Dict[str, Any], platform: str) -> None:
        """從字典資料建立 AgentActivity 並加入隊列"""
        if "event_type" in data:
            return
        activity = AgentActivity(
            platform=platform,
            agent_id=data.get("agent_id", 0),
            agent_name=data.get("agent_name", ""),
            action_type=data.get("action_type", ""),
            action_args=data.get("action_args", {}),
            round_num=data.get("round", 0),
            timestamp=data.get("timestamp", datetime.now().isoformat()),
        )
        self.add_activity(activity)

    def _worker_loop(self) -> None:
        """後台工作迴圈：按平台累積批次後發送到 Graphiti"""
        import time

        while self._running or not self._activity_queue.empty():
            try:
                try:
                    activity = self._activity_queue.get(timeout=1)
                    platform = activity.platform.lower()
                    with self._buffer_lock:
                        if platform not in self._platform_buffers:
                            self._platform_buffers[platform] = []
                        self._platform_buffers[platform].append(activity)
                        if len(self._platform_buffers[platform]) >= self.BATCH_SIZE:
                            batch = self._platform_buffers[platform][: self.BATCH_SIZE]
                            self._platform_buffers[platform] = self._platform_buffers[platform][
                                self.BATCH_SIZE :
                            ]
                            self._send_batch_activities(batch, platform)
                            time.sleep(self.SEND_INTERVAL)
                except Empty:
                    pass
            except Exception as e:
                import time as _t

                logger.error(f"工作迴圈異常: {e}")
                _t.sleep(1)

    def _send_batch_activities(
        self, activities: List[AgentActivity], platform: str
    ) -> None:
        """批量將活動合并為文本發送到 Graphiti 圖譜"""
        import time
        from datetime import timezone

        from graphiti_core.nodes import EpisodeType

        from .graphiti_client import _close_graphiti, create_graphiti, run_async

        if not activities:
            return

        combined_text = "\n".join(a.to_episode_text() for a in activities)

        for attempt in range(self.MAX_RETRIES):
            try:
                text_snapshot = combined_text
                graph_id_snapshot = self.graph_id

                async def _send(text=text_snapshot, gid=graph_id_snapshot):
                    g = await create_graphiti()
                    try:
                        from datetime import datetime, timezone as tz

                        await g.add_episode(
                            name=f"sim_{platform}_activities",
                            episode_body=text,
                            source=EpisodeType.text,
                            source_description=f"simulation activities from {platform}",
                            group_id=gid,
                            reference_time=datetime.now(tz.utc),
                        )
                    finally:
                        await _close_graphiti(g)

                run_async(_send())
                self._total_sent += 1
                self._total_items_sent += len(activities)
                display = self.PLATFORM_DISPLAY_NAMES.get(platform, platform)
                logger.info(
                    f"成功批量發送 {len(activities)} 條{display}活動到圖譜 {self.graph_id}"
                )
                return
            except Exception as e:
                if attempt < self.MAX_RETRIES - 1:
                    logger.warning(
                        f"批量發送失敗 (嘗試 {attempt + 1}/{self.MAX_RETRIES}): {e}"
                    )
                    time.sleep(self.RETRY_DELAY * (attempt + 1))
                else:
                    logger.error(f"批量發送失敗，已重試 {self.MAX_RETRIES} 次: {e}")
                    self._failed_count += 1

    def _flush_remaining(self) -> None:
        """將隊列和緩衝區中剩餘的活動全部發送"""
        while not self._activity_queue.empty():
            try:
                activity = self._activity_queue.get_nowait()
                platform = activity.platform.lower()
                with self._buffer_lock:
                    if platform not in self._platform_buffers:
                        self._platform_buffers[platform] = []
                    self._platform_buffers[platform].append(activity)
            except Empty:
                break

        with self._buffer_lock:
            for platform, buffer in self._platform_buffers.items():
                if buffer:
                    self._send_batch_activities(buffer, platform)
            for platform in self._platform_buffers:
                self._platform_buffers[platform] = []

    def get_stats(self) -> Dict[str, Any]:
        """取得統計資訊"""
        with self._buffer_lock:
            buffer_sizes = {p: len(b) for p, b in self._platform_buffers.items()}
        return {
            "graph_id": self.graph_id,
            "batch_size": self.BATCH_SIZE,
            "total_activities": self._total_activities,
            "batches_sent": self._total_sent,
            "items_sent": self._total_items_sent,
            "failed_count": self._failed_count,
            "skipped_count": self._skipped_count,
            "queue_size": self._activity_queue.qsize(),
            "buffer_sizes": buffer_sizes,
            "running": self._running,
        }


class GraphitiMemoryManager:
    """
    管理多個模擬的 Graphiti 記憶更新器。
    公開介面（class methods）與 ZepGraphMemoryManager 完全相同。
    """

    _updaters: Dict[str, GraphitiGraphMemoryUpdater] = {}
    _lock = threading.Lock()
    _stop_all_done = False

    @classmethod
    def create_updater(
        cls, simulation_id: str, graph_id: str
    ) -> GraphitiGraphMemoryUpdater:
        with cls._lock:
            if simulation_id in cls._updaters:
                cls._updaters[simulation_id].stop()
            updater = GraphitiGraphMemoryUpdater(graph_id)
            updater.start()
            cls._updaters[simulation_id] = updater
            logger.info(
                f"創建 Graphiti 記憶更新器: simulation_id={simulation_id}, graph_id={graph_id}"
            )
            return updater

    @classmethod
    def get_updater(
        cls, simulation_id: str
    ) -> Optional[GraphitiGraphMemoryUpdater]:
        return cls._updaters.get(simulation_id)

    @classmethod
    def stop_updater(cls, simulation_id: str) -> None:
        with cls._lock:
            if simulation_id in cls._updaters:
                cls._updaters[simulation_id].stop()
                del cls._updaters[simulation_id]
                logger.info(f"已停止 Graphiti 記憶更新器: simulation_id={simulation_id}")

    @classmethod
    def stop_all(cls) -> None:
        if cls._stop_all_done:
            return
        cls._stop_all_done = True
        with cls._lock:
            if cls._updaters:
                for sim_id, updater in list(cls._updaters.items()):
                    try:
                        updater.stop()
                    except Exception as e:
                        logger.error(f"停止更新器失敗: simulation_id={sim_id}, error={e}")
                cls._updaters.clear()
            logger.info("已停止所有 Graphiti 記憶更新器")

    @classmethod
    def get_all_stats(cls) -> Dict[str, Dict[str, Any]]:
        return {
            sim_id: updater.get_stats() for sim_id, updater in cls._updaters.items()
        }
