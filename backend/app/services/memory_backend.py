"""
記憶後端工廠模組

根據 Config.MEMORY_BACKEND 環境變數選擇實際的服務實現：
- 'zep'      (預設) → 使用 Zep Cloud
- 'graphiti'        → 使用 Graphiti OSS + Neo4j
"""

from ..config import Config


def get_graph_builder():
    """取得圖譜構建服務實例"""
    if Config.MEMORY_BACKEND == 'graphiti':
        from .graphiti_graph_builder import GraphitiGraphBuilderService
        return GraphitiGraphBuilderService()
    from .graph_builder import GraphBuilderService
    return GraphBuilderService(api_key=Config.ZEP_API_KEY)


def get_entity_reader():
    """取得實體讀取服務實例"""
    if Config.MEMORY_BACKEND == 'graphiti':
        from .graphiti_entity_reader import GraphitiEntityReader
        return GraphitiEntityReader()
    from .zep_entity_reader import ZepEntityReader
    return ZepEntityReader()


def get_memory_manager():
    """
    取得記憶管理器類別（注意：回傳的是 class，非實例）

    用法：
        mgr = get_memory_manager()
        mgr.create_updater(simulation_id, graph_id)
        mgr.get_updater(simulation_id)
        mgr.stop_updater(simulation_id)
        mgr.stop_all()
    """
    if Config.MEMORY_BACKEND == 'graphiti':
        from .graphiti_memory_updater import GraphitiMemoryManager
        return GraphitiMemoryManager
    from .zep_graph_memory_updater import ZepGraphMemoryManager
    return ZepGraphMemoryManager


def get_tools_service(api_key=None, llm_client=None):
    """取得工具服務實例"""
    if Config.MEMORY_BACKEND == 'graphiti':
        from .graphiti_tools import GraphitiToolsService
        return GraphitiToolsService(llm_client=llm_client)
    from .zep_tools import ZepToolsService
    return ZepToolsService(api_key=api_key, llm_client=llm_client)


def check_backend_config() -> list:
    """
    檢查當前後端配置是否完整，回傳錯誤訊息列表（空列表表示配置正確）
    """
    return Config.validate()
