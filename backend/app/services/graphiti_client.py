"""
Graphiti 客户端工厂
管理 Graphiti 实例的创建和 Neo4j 连接的生命周期

由于 Flask 是同步框架，所有 Graphiti async 呼叫透過 run_async() 在独立执行緒中执行。
每次操作建立新的 Graphiti 实例，确保 event loop 安全性。
"""

import asyncio
import concurrent.futures
import threading
from typing import Optional

from ..config import Config
from ..utils.logger import get_logger

logger = get_logger('mirofish.graphiti_client')

# 标记索引是否已初始化（进程级别，Neo4j 索引持久化在数据库中，只需创建一次）
_indices_initialized = False
_indices_lock = threading.Lock()


async def create_graphiti():
    """
    在 async 上下文中建立新的 Graphiti 实例。
    每次呼叫都回傳独立实例，确保 event loop 安全。
    """
    from graphiti_core import Graphiti
    from graphiti_core.llm_client.openai_client import OpenAIClient
    from graphiti_core.embedder.openai import OpenAIEmbedder, OpenAIEmbedderConfig

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


async def _close_graphiti(g) -> None:
    """安全关闭 Graphiti 实例，释放 Neo4j 连接"""
    try:
        if hasattr(g, 'close') and callable(g.close):
            await g.close()
        elif hasattr(g, 'driver') and g.driver is not None:
            await g.driver.close()
    except Exception as e:
        logger.warning(f"关闭 Graphiti 时发生错误: {e}")


def ensure_indices() -> None:
    """
    确保 Neo4j 索引已建立（首次使用时调用）。
    索引持久化在数据库中，无需每次创建。
    """
    global _indices_initialized

    with _indices_lock:
        if _indices_initialized:
            return

        async def _init():
            g = await create_graphiti()
            try:
                await g.build_indices_and_constraints()
                logger.info("Neo4j 索引初始化完成")
            finally:
                await _close_graphiti(g)

        try:
            run_async(_init())
            _indices_initialized = True
        except Exception as e:
            logger.error(f"Neo4j 索引初始化失败: {e}")
            raise


def get_neo4j_driver():
    """
    取得同步 Neo4j Driver，用于直接 Cypher 查询。
    调用方负责调用 driver.close() 释放连接。
    """
    from neo4j import GraphDatabase

    return GraphDatabase.driver(
        Config.NEO4J_URI,
        auth=(Config.NEO4J_USER, Config.NEO4J_PASSWORD),
    )


def run_async(coro):
    """
    在同步 Flask 环境中安全执行 async 函式。
    使用 ThreadPoolExecutor 确保在独立执行緒中有干净的 event loop。
    """
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(asyncio.run, coro)
        return future.result()
