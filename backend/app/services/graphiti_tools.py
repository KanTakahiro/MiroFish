"""
Graphiti 检索工具服务
对应 services/zep_tools.py，使用 Graphiti OSS + Neo4j 作为后端。

直接复用 zep_tools 中的所有数据类（SearchResult、InsightForgeResult 等），
确保 report_agent.py 等调用端无需修改数据处理逻辑。
"""

import csv
import json
import os
import re
import time
from typing import Any, Dict, List, Optional

from ..config import Config
from ..utils.graphiti_paging import fetch_all_nodes, fetch_all_edges
from ..utils.llm_client import LLMClient
from ..utils.logger import get_logger
from .zep_tools import (
    AgentInterview,
    EdgeInfo,
    InsightForgeResult,
    InterviewResult,
    NodeInfo,
    PanoramaResult,
    SearchResult,
)

logger = get_logger('mirofish.graphiti_tools')

# Neo4j：按 UUID 查找单个节点
_NODE_BY_UUID_QUERY = """
MATCH (n:Entity {uuid: $uuid})
RETURN n, labels(n) AS node_labels
LIMIT 1
"""


class GraphitiToolsService:
    """
    Graphiti 检索工具服务。
    公开接口与 ZepToolsService 完全相同，
    后端改为 Graphiti 语义搜索 + 同步 Neo4j Cypher 查询。
    """

    MAX_RETRIES = 3
    RETRY_DELAY = 2.0

    def __init__(self, llm_client: Optional[LLMClient] = None) -> None:
        self._llm_client = llm_client
        logger.info("GraphitiToolsService 初始化完成")

    @property
    def llm(self) -> LLMClient:
        if self._llm_client is None:
            self._llm_client = LLMClient()
        return self._llm_client

    def _get_driver(self):
        from .graphiti_client import get_neo4j_driver
        return get_neo4j_driver()

    # ──────────────────────────────────────────────
    # 基础工具
    # ──────────────────────────────────────────────

    def search_graph(
        self,
        graph_id: str,
        query: str,
        limit: int = 10,
        scope: str = "edges",
    ) -> SearchResult:
        """图谱语义搜索（使用 Graphiti 向量搜索）"""
        logger.info(f"Graphiti 图谱搜索: graph_id={graph_id}, query={query[:50]}...")

        try:
            from .graphiti_client import _close_graphiti, create_graphiti, run_async

            async def _search():
                g = await create_graphiti()
                try:
                    edges = await g.search(
                        query=query,
                        group_ids=[graph_id],
                        num_results=limit,
                    )
                    return edges
                finally:
                    await _close_graphiti(g)

            edge_results = run_async(_search())

            facts = []
            edges_data = []
            seen_facts: set = set()

            for edge in edge_results or []:
                fact = getattr(edge, 'fact', '') or ''
                if fact and fact not in seen_facts:
                    facts.append(fact)
                    seen_facts.add(fact)
                edges_data.append({
                    "uuid": getattr(edge, 'uuid', '') or '',
                    "name": getattr(edge, 'name', '') or '',
                    "fact": fact,
                    "source_node_uuid": getattr(edge, 'source_node_uuid', '') or '',
                    "target_node_uuid": getattr(edge, 'target_node_uuid', '') or '',
                })

            logger.info(f"Graphiti 搜索完成: 找到 {len(facts)} 条相关事实")
            return SearchResult(
                facts=facts,
                edges=edges_data,
                nodes=[],
                query=query,
                total_count=len(facts),
            )

        except Exception as e:
            logger.warning(f"Graphiti 语义搜索失败，降级为本地关键词匹配: {str(e)}")
            return self._local_search(graph_id, query, limit)

    def _local_search(
        self, graph_id: str, query: str, limit: int = 10
    ) -> SearchResult:
        """本地关键词匹配搜索（Graphiti 搜索失败时的降级方案）"""
        query_lower = query.lower()
        keywords = [
            w.strip()
            for w in query_lower.replace(',', ' ').replace('，', ' ').split()
            if len(w.strip()) > 1
        ]

        def match_score(text: str) -> int:
            if not text:
                return 0
            text_lower = text.lower()
            if query_lower in text_lower:
                return 100
            return sum(10 for kw in keywords if kw in text_lower)

        try:
            all_edges = self.get_all_edges(graph_id)
            scored = sorted(
                [(match_score(e.fact) + match_score(e.name), e) for e in all_edges if match_score(e.fact) + match_score(e.name) > 0],
                reverse=True,
            )
            facts = [e.fact for _, e in scored[:limit] if e.fact]
            edges_data = [
                {
                    "uuid": e.uuid,
                    "name": e.name,
                    "fact": e.fact,
                    "source_node_uuid": e.source_node_uuid,
                    "target_node_uuid": e.target_node_uuid,
                }
                for _, e in scored[:limit]
            ]
        except Exception as ex:
            logger.error(f"本地搜索失败: {ex}")
            facts, edges_data = [], []

        return SearchResult(
            facts=facts, edges=edges_data, nodes=[], query=query, total_count=len(facts)
        )

    def get_all_nodes(self, graph_id: str) -> List[NodeInfo]:
        """取得图谱所有节点"""
        logger.info(f"取得图谱 {graph_id} 所有节点...")
        driver = self._get_driver()
        try:
            raw_nodes = fetch_all_nodes(driver, graph_id)
            result = [
                NodeInfo(
                    uuid=n.uuid_ or '',
                    name=n.name or '',
                    labels=n.labels or [],
                    summary=n.summary or '',
                    attributes={},
                )
                for n in raw_nodes
            ]
            logger.info(f"取得 {len(result)} 个节点")
            return result
        finally:
            driver.close()

    def get_all_edges(self, graph_id: str, include_temporal: bool = True) -> List[EdgeInfo]:
        """取得图谱所有边（含时间信息）"""
        logger.info(f"取得图谱 {graph_id} 所有边...")
        driver = self._get_driver()
        try:
            raw_edges = fetch_all_edges(driver, graph_id)
            result = []
            for e in raw_edges:
                info = EdgeInfo(
                    uuid=e.uuid_ or '',
                    name=e.name or '',
                    fact=e.fact or '',
                    source_node_uuid=e.source_node_uuid or '',
                    target_node_uuid=e.target_node_uuid or '',
                )
                if include_temporal:
                    info.created_at = e.created_at
                    info.valid_at = e.valid_at
                    info.invalid_at = e.invalid_at
                    info.expired_at = e.expired_at
                result.append(info)
            logger.info(f"取得 {len(result)} 条边")
            return result
        finally:
            driver.close()

    def get_node_detail(self, node_uuid: str) -> Optional[NodeInfo]:
        """取得单个节点详细信息"""
        driver = self._get_driver()
        try:
            with driver.session() as session:
                result = session.run(_NODE_BY_UUID_QUERY, uuid=node_uuid)
                record = result.single()
                if not record:
                    return None
                data = record.data()
                n = data.get('n') or {}
                labels = data.get('node_labels') or []
                return NodeInfo(
                    uuid=n.get('uuid', '') or '',
                    name=n.get('name', '') or '',
                    labels=list(labels),
                    summary=n.get('summary', '') or '',
                    attributes={},
                )
        except Exception as e:
            logger.error(f"取得节点详情失败 {node_uuid[:8]}...: {e}")
            return None
        finally:
            driver.close()

    def get_node_edges(self, graph_id: str, node_uuid: str) -> List[EdgeInfo]:
        """取得节点相关的所有边"""
        try:
            all_edges = self.get_all_edges(graph_id)
            return [
                e for e in all_edges
                if e.source_node_uuid == node_uuid or e.target_node_uuid == node_uuid
            ]
        except Exception as e:
            logger.warning(f"取得节点边失败: {e}")
            return []

    def get_entities_by_type(self, graph_id: str, entity_type: str) -> List[NodeInfo]:
        """按类型取得实体"""
        return [n for n in self.get_all_nodes(graph_id) if entity_type in n.labels]

    def get_graph_statistics(self, graph_id: str) -> Dict[str, Any]:
        """取得图谱统计信息"""
        nodes = self.get_all_nodes(graph_id)
        edges = self.get_all_edges(graph_id)
        entity_types: Dict[str, int] = {}
        for n in nodes:
            for label in n.labels:
                if label not in ("Entity", "Node", "Episodic"):
                    entity_types[label] = entity_types.get(label, 0) + 1
        relation_types: Dict[str, int] = {}
        for e in edges:
            relation_types[e.name] = relation_types.get(e.name, 0) + 1
        return {
            "graph_id": graph_id,
            "total_nodes": len(nodes),
            "total_edges": len(edges),
            "entity_types": entity_types,
            "relation_types": relation_types,
        }

    def get_entity_summary(self, graph_id: str, entity_name: str) -> Dict[str, Any]:
        """取得指定实体的关系摘要"""
        search_result = self.search_graph(graph_id=graph_id, query=entity_name, limit=20)
        all_nodes = self.get_all_nodes(graph_id)
        entity_node = next(
            (n for n in all_nodes if n.name.lower() == entity_name.lower()), None
        )
        related_edges = []
        if entity_node:
            related_edges = self.get_node_edges(graph_id, entity_node.uuid)
        return {
            "entity_name": entity_name,
            "entity_info": entity_node.to_dict() if entity_node else None,
            "related_facts": search_result.facts,
            "related_edges": [e.to_dict() for e in related_edges],
            "total_relations": len(related_edges),
        }

    def get_simulation_context(
        self, graph_id: str, simulation_requirement: str, limit: int = 30
    ) -> Dict[str, Any]:
        """取得模拟相关的上下文信息"""
        search_result = self.search_graph(graph_id=graph_id, query=simulation_requirement, limit=limit)
        stats = self.get_graph_statistics(graph_id)
        all_nodes = self.get_all_nodes(graph_id)
        entities = [
            {"name": n.name, "type": next((l for l in n.labels if l not in ("Entity", "Node", "Episodic")), "实体"), "summary": n.summary}
            for n in all_nodes
            if any(l not in ("Entity", "Node", "Episodic") for l in n.labels)
        ]
        return {
            "simulation_requirement": simulation_requirement,
            "related_facts": search_result.facts,
            "graph_statistics": stats,
            "entities": entities[:limit],
            "total_entities": len(entities),
        }

    # ──────────────────────────────────────────────
    # 核心检索工具
    # ──────────────────────────────────────────────

    def insight_forge(
        self,
        graph_id: str,
        query: str,
        simulation_requirement: str,
        report_context: str = "",
        max_sub_queries: int = 5,
    ) -> InsightForgeResult:
        """深度洞察检索（与 ZepToolsService.insight_forge 完全相同的逻辑，只是搜索后端不同）"""
        logger.info(f"InsightForge 深度洞察检索: {query[:50]}...")

        result = InsightForgeResult(
            query=query,
            simulation_requirement=simulation_requirement,
            sub_queries=[],
        )

        sub_queries = self._generate_sub_queries(
            query=query,
            simulation_requirement=simulation_requirement,
            report_context=report_context,
            max_queries=max_sub_queries,
        )
        result.sub_queries = sub_queries
        logger.info(f"生成 {len(sub_queries)} 个子问题")

        all_facts: List[str] = []
        all_edges_data: List[Dict] = []
        seen_facts: set = set()

        for sub_query in sub_queries:
            sr = self.search_graph(graph_id=graph_id, query=sub_query, limit=15, scope="edges")
            for fact in sr.facts:
                if fact not in seen_facts:
                    all_facts.append(fact)
                    seen_facts.add(fact)
            all_edges_data.extend(sr.edges)

        main_sr = self.search_graph(graph_id=graph_id, query=query, limit=20, scope="edges")
        for fact in main_sr.facts:
            if fact not in seen_facts:
                all_facts.append(fact)
                seen_facts.add(fact)

        result.semantic_facts = all_facts
        result.total_facts = len(all_facts)

        # 从边中提取相关实体
        entity_uuids = set()
        for edge_data in all_edges_data:
            if isinstance(edge_data, dict):
                if edge_data.get('source_node_uuid'):
                    entity_uuids.add(edge_data['source_node_uuid'])
                if edge_data.get('target_node_uuid'):
                    entity_uuids.add(edge_data['target_node_uuid'])

        entity_insights: List[Dict] = []
        node_map: Dict[str, NodeInfo] = {}

        for uuid_val in list(entity_uuids):
            if not uuid_val:
                continue
            try:
                node = self.get_node_detail(uuid_val)
                if node:
                    node_map[uuid_val] = node
                    entity_type = next(
                        (l for l in node.labels if l not in ("Entity", "Node", "Episodic")),
                        "实体",
                    )
                    entity_insights.append(
                        {
                            "name": node.name,
                            "type": entity_type,
                            "summary": node.summary,
                            "uuid": node.uuid,
                        }
                    )
            except Exception:
                pass

        result.entity_insights = entity_insights
        result.total_entities = len(entity_insights)

        # 关系链
        relationship_chains: List[str] = []
        for edge_data in all_edges_data:
            if not isinstance(edge_data, dict) or not edge_data.get('fact'):
                continue
            source_name = node_map.get(edge_data.get('source_node_uuid', ''))
            target_name = node_map.get(edge_data.get('target_node_uuid', ''))
            s = source_name.name if source_name else edge_data.get('source_node_uuid', '')[:8]
            t = target_name.name if target_name else edge_data.get('target_node_uuid', '')[:8]
            chain = f"{s} → {t}: {edge_data['fact']}"
            if chain not in relationship_chains:
                relationship_chains.append(chain)

        result.relationship_chains = relationship_chains
        result.total_relationships = len(relationship_chains)

        logger.info(
            f"InsightForge完成: {result.total_facts}条事实, "
            f"{result.total_entities}个实体, {result.total_relationships}条关系链"
        )
        return result

    def panorama_search(
        self,
        graph_id: str,
        query: str,
        include_expired: bool = True,
        limit: int = 50,
    ) -> PanoramaResult:
        """广度搜索（包含全部历史/过期信息）"""
        logger.info(f"PanoramaSearch 广度搜索: {query[:50]}...")

        result = PanoramaResult(query=query)

        all_nodes = self.get_all_nodes(graph_id)
        node_map = {n.uuid: n for n in all_nodes}
        result.all_nodes = all_nodes
        result.total_nodes = len(all_nodes)

        all_edges = self.get_all_edges(graph_id, include_temporal=True)
        result.all_edges = all_edges
        result.total_edges = len(all_edges)

        active_facts: List[str] = []
        historical_facts: List[str] = []

        for edge in all_edges:
            if not edge.fact:
                continue
            is_historical = edge.is_expired or edge.is_invalid
            if is_historical:
                valid_at = edge.valid_at or "未知"
                invalid_at = edge.invalid_at or edge.expired_at or "未知"
                historical_facts.append(f"[{valid_at} - {invalid_at}] {edge.fact}")
            else:
                active_facts.append(edge.fact)

        # 按查询相关性排序
        query_lower = query.lower()
        keywords = [
            w.strip()
            for w in query_lower.replace(',', ' ').replace('，', ' ').split()
            if len(w.strip()) > 1
        ]

        def relevance_score(fact: str) -> int:
            fl = fact.lower()
            score = 100 if query_lower in fl else 0
            score += sum(10 for kw in keywords if kw in fl)
            return score

        active_facts.sort(key=relevance_score, reverse=True)
        historical_facts.sort(key=relevance_score, reverse=True)

        result.active_facts = active_facts[:limit]
        result.historical_facts = historical_facts[:limit] if include_expired else []
        result.active_count = len(active_facts)
        result.historical_count = len(historical_facts)

        logger.info(
            f"PanoramaSearch完成: {result.active_count}条有效, {result.historical_count}条历史"
        )
        return result

    def quick_search(self, graph_id: str, query: str, limit: int = 10) -> SearchResult:
        """快速搜索（直接调用 search_graph）"""
        logger.info(f"QuickSearch 简单搜索: {query[:50]}...")
        result = self.search_graph(graph_id=graph_id, query=query, limit=limit, scope="edges")
        logger.info(f"QuickSearch完成: {result.total_count}条结果")
        return result

    def interview_agents(
        self,
        simulation_id: str,
        interview_requirement: str,
        simulation_requirement: str = "",
        max_agents: int = 5,
        custom_questions: List[str] = None,
    ) -> InterviewResult:
        """
        深度采访（采访模拟中运行的 Agent）。
        与 ZepToolsService.interview_agents 逻辑完全相同，不依赖图谱后端。
        """
        from .simulation_runner import SimulationRunner

        logger.info(f"InterviewAgents 深度采访（真实API）: {interview_requirement[:50]}...")

        result = InterviewResult(
            interview_topic=interview_requirement,
            interview_questions=custom_questions or [],
        )

        profiles = self._load_agent_profiles(simulation_id)
        if not profiles:
            logger.warning(f"未找到模拟 {simulation_id} 的人设文件")
            result.summary = "未找到可采访的Agent人设文件"
            return result

        result.total_agents = len(profiles)

        selected_agents, selected_indices, selection_reasoning = self._select_agents_for_interview(
            profiles=profiles,
            interview_requirement=interview_requirement,
            simulation_requirement=simulation_requirement,
            max_agents=max_agents,
        )
        result.selected_agents = selected_agents
        result.selection_reasoning = selection_reasoning

        if not result.interview_questions:
            result.interview_questions = self._generate_interview_questions(
                interview_requirement=interview_requirement,
                simulation_requirement=simulation_requirement,
                selected_agents=selected_agents,
            )

        combined_prompt = "\n".join(
            [f"{i + 1}. {q}" for i, q in enumerate(result.interview_questions)]
        )

        INTERVIEW_PROMPT_PREFIX = (
            "你正在接受一次采访。请结合你的人设、所有的过往记忆与行动，"
            "以纯文本方式直接回答以下问题。\n"
            "回复要求：\n"
            "1. 直接用自然语言回答，不要调用任何工具\n"
            "2. 不要返回JSON格式或工具调用格式\n"
            "3. 不要使用Markdown标题（如#、##、###）\n"
            "4. 按问题编号逐一回答，每个回答以「问题X：」开头（X为问题编号）\n"
            "5. 每个问题的回答之间用空行分隔\n"
            "6. 回答要有实质内容，每个问题至少回答2-3句话\n\n"
        )
        optimized_prompt = f"{INTERVIEW_PROMPT_PREFIX}{combined_prompt}"

        try:
            interviews_request = [
                {"agent_id": idx, "prompt": optimized_prompt} for idx in selected_indices
            ]
            api_result = SimulationRunner.interview_agents_batch(
                simulation_id=simulation_id,
                interviews=interviews_request,
                platform=None,
                timeout=180.0,
            )

            if not api_result.get("success", False):
                error_msg = api_result.get("error", "未知错误")
                result.summary = f"采访API调用失败：{error_msg}。请检查OASIS模拟环境状态。"
                return result

            api_data = api_result.get("result", {})
            results_dict = api_data.get("results", {}) if isinstance(api_data, dict) else {}

            for i, agent_idx in enumerate(selected_indices):
                agent = selected_agents[i]
                agent_name = agent.get("realname", agent.get("username", f"Agent_{agent_idx}"))
                agent_role = agent.get("profession", "未知")
                agent_bio = agent.get("bio", "")

                twitter_result = results_dict.get(f"twitter_{agent_idx}", {})
                reddit_result = results_dict.get(f"reddit_{agent_idx}", {})

                twitter_response = self._clean_tool_call_response(
                    twitter_result.get("response", "")
                )
                reddit_response = self._clean_tool_call_response(
                    reddit_result.get("response", "")
                )

                twitter_text = twitter_response if twitter_response else "（该平台未获得回复）"
                reddit_text = reddit_response if reddit_response else "（该平台未获得回复）"
                response_text = (
                    f"【Twitter平台回答】\n{twitter_text}\n\n【Reddit平台回答】\n{reddit_text}"
                )

                combined_responses = f"{twitter_response} {reddit_response}"
                clean_text = re.sub(r'#{1,6}\s+', '', combined_responses)
                clean_text = re.sub(r'\{[^}]*tool_name[^}]*\}', '', clean_text)
                clean_text = re.sub(r'[*_`|>~\-]{2,}', '', clean_text)
                clean_text = re.sub(r'问题\d+[：:]\s*', '', clean_text)
                clean_text = re.sub(r'【[^】]+】', '', clean_text)

                sentences = re.split(r'[。！？]', clean_text)
                meaningful = [
                    s.strip() for s in sentences
                    if 20 <= len(s.strip()) <= 150
                    and not re.match(r'^[\s\W，,；;：:、]+', s.strip())
                    and not s.strip().startswith(('{', '问题'))
                ]
                meaningful.sort(key=len, reverse=True)
                key_quotes = [s + "。" for s in meaningful[:3]]

                if not key_quotes:
                    paired = re.findall(r'\u201c([^\u201c\u201d]{15,100})\u201d', clean_text)
                    paired += re.findall(r'\u300c([^\u300c\u300d]{15,100})\u300d', clean_text)
                    key_quotes = [q for q in paired if not re.match(r'^[，,；;：:、]', q)][:3]

                result.interviews.append(
                    AgentInterview(
                        agent_name=agent_name,
                        agent_role=agent_role,
                        agent_bio=agent_bio[:1000],
                        question=combined_prompt,
                        response=response_text,
                        key_quotes=key_quotes[:5],
                    )
                )

            result.interviewed_count = len(result.interviews)

        except ValueError as e:
            result.summary = f"采访失败：{str(e)}。模拟环境可能已关闭，请确保OASIS环境正在运行。"
            return result
        except Exception as e:
            logger.error(f"采访API调用异常: {e}")
            result.summary = f"采访过程发生错误：{str(e)}"
            return result

        if result.interviews:
            result.summary = self._generate_interview_summary(
                interviews=result.interviews,
                interview_requirement=interview_requirement,
            )

        logger.info(f"InterviewAgents完成: 采访了 {result.interviewed_count} 个Agent（双平台）")
        return result

    # ──────────────────────────────────────────────
    # 辅助方法（与 ZepToolsService 相同的 LLM/文件逻辑）
    # ──────────────────────────────────────────────

    def _generate_sub_queries(
        self,
        query: str,
        simulation_requirement: str,
        report_context: str = "",
        max_queries: int = 5,
    ) -> List[str]:
        """使用 LLM 将复杂问题分解为多个子问题"""
        system_prompt = (
            "你是一个专业的问题分析专家。你的任务是将一个复杂问题分解为多个可以在模拟世界中独立观察的子问题。\n"
            "要求：\n"
            "1. 每个子问题应该足够具体，可以在模拟世界中找到相关的Agent行为或事件\n"
            "2. 子问题应该覆盖原问题的不同维度（如：谁、什么、为什么、怎么样、何时、何地）\n"
            "3. 子问题应该与模拟场景相关\n"
            '4. 返回JSON格式：{"sub_queries": ["子问题1", "子问题2", ...]}'
        )
        user_prompt = (
            f"模拟需求背景：\n{simulation_requirement}\n\n"
            + (f"报告上下文：{report_context[:500]}\n\n" if report_context else "")
            + f"请将以下问题分解为{max_queries}个子问题：\n{query}\n\n返回JSON格式的子问题列表。"
        )
        try:
            response = self.llm.chat_json(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.3,
            )
            return [str(sq) for sq in response.get("sub_queries", [])[:max_queries]]
        except Exception as e:
            logger.warning(f"生成子问题失败: {e}，使用默认子问题")
            return [
                query,
                f"{query} 的主要参与者",
                f"{query} 的原因和影响",
                f"{query} 的发展过程",
            ][:max_queries]

    @staticmethod
    def _clean_tool_call_response(response: str) -> str:
        if not response or not response.strip().startswith('{'):
            return response
        text = response.strip()
        if 'tool_name' not in text[:80]:
            return response
        try:
            data = json.loads(text)
            if isinstance(data, dict) and 'arguments' in data:
                for key in ('content', 'text', 'body', 'message', 'reply'):
                    if key in data['arguments']:
                        return str(data['arguments'][key])
        except (json.JSONDecodeError, KeyError, TypeError):
            match = re.search(r'"content"\s*:\s*"((?:[^"\\]|\\.)*)"', text)
            if match:
                return match.group(1).replace('\\n', '\n').replace('\\"', '"')
        return response

    def _load_agent_profiles(self, simulation_id: str) -> List[Dict[str, Any]]:
        """加载模拟的 Agent 人设文件"""
        sim_dir = os.path.join(
            os.path.dirname(__file__),
            f'../../uploads/simulations/{simulation_id}',
        )
        profiles: List[Dict[str, Any]] = []

        reddit_path = os.path.join(sim_dir, "reddit_profiles.json")
        if os.path.exists(reddit_path):
            try:
                with open(reddit_path, 'r', encoding='utf-8') as f:
                    profiles = json.load(f)
                return profiles
            except Exception as e:
                logger.warning(f"读取 reddit_profiles.json 失败: {e}")

        twitter_path = os.path.join(sim_dir, "twitter_profiles.csv")
        if os.path.exists(twitter_path):
            try:
                with open(twitter_path, 'r', encoding='utf-8') as f:
                    for row in csv.DictReader(f):
                        profiles.append({
                            "realname": row.get("name", ""),
                            "username": row.get("username", ""),
                            "bio": row.get("description", ""),
                            "persona": row.get("user_char", ""),
                            "profession": "未知",
                        })
                return profiles
            except Exception as e:
                logger.warning(f"读取 twitter_profiles.csv 失败: {e}")

        return profiles

    def _select_agents_for_interview(
        self,
        profiles: List[Dict[str, Any]],
        interview_requirement: str,
        simulation_requirement: str,
        max_agents: int,
    ):
        """使用 LLM 选择最相关的 Agent 进行采访"""
        profiles_summary = "\n".join(
            f"[{i}] {p.get('realname', p.get('username', 'Agent'))} - "
            f"{p.get('profession', '未知')} - {p.get('bio', '')[:100]}"
            for i, p in enumerate(profiles[:30])
        )
        system_prompt = (
            "你是一个采访策划专家。根据采访需求和模拟背景，选择最相关的Agent进行采访。\n"
            '返回JSON格式：{"selected_indices": [0, 1, 2], "reasoning": "选择理由"}'
        )
        user_prompt = (
            f"采访需求：{interview_requirement}\n"
            f"模拟背景：{simulation_requirement[:200]}\n\n"
            f"可用Agent列表：\n{profiles_summary}\n\n"
            f"请选择最多{max_agents}个最相关的Agent（返回索引列表）。"
        )
        try:
            response = self.llm.chat_json(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.3,
            )
            indices = [
                int(i) for i in response.get("selected_indices", [])
                if isinstance(i, (int, float)) and 0 <= int(i) < len(profiles)
            ][:max_agents]
            reasoning = response.get("reasoning", "")
        except Exception as e:
            logger.warning(f"LLM选择Agent失败: {e}，使用默认选择")
            indices = list(range(min(max_agents, len(profiles))))
            reasoning = "默认选择前几个Agent"

        selected = [profiles[i] for i in indices]
        return selected, indices, reasoning

    def _generate_interview_questions(
        self,
        interview_requirement: str,
        simulation_requirement: str,
        selected_agents: List[Dict[str, Any]],
    ) -> List[str]:
        """使用 LLM 生成采访问题"""
        agent_desc = "、".join(
            p.get("realname", p.get("username", "Agent")) for p in selected_agents[:5]
        )
        system_prompt = (
            "你是一个专业采访者。根据采访需求生成具体、有深度的采访问题。\n"
            '返回JSON格式：{"questions": ["问题1", "问题2", ...]}'
        )
        user_prompt = (
            f"采访需求：{interview_requirement}\n"
            f"模拟背景：{simulation_requirement[:200]}\n"
            f"受访Agent：{agent_desc}\n\n"
            "请生成3-5个具体的采访问题。"
        )
        try:
            response = self.llm.chat_json(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.5,
            )
            return [str(q) for q in response.get("questions", [])][:5]
        except Exception as e:
            logger.warning(f"生成采访问题失败: {e}")
            return [
                f"关于{interview_requirement}，你有什么看法？",
                "这件事对你产生了什么影响？",
                "你认为未来会怎么发展？",
            ]

    def _generate_interview_summary(
        self, interviews: List[AgentInterview], interview_requirement: str
    ) -> str:
        """生成采访摘要"""
        interview_texts = "\n\n".join(
            f"[{itv.agent_name}（{itv.agent_role}）]\n{itv.response[:500]}"
            for itv in interviews[:5]
        )
        system_prompt = "你是一个专业的访谈分析师。请对以下采访内容进行简要总结。"
        user_prompt = (
            f"采访主题：{interview_requirement}\n\n"
            f"采访内容：\n{interview_texts}\n\n"
            "请用200字以内总结主要观点。"
        )
        try:
            return self.llm.chat(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.3,
            )
        except Exception as e:
            logger.warning(f"生成采访摘要失败: {e}")
            return f"采访了{len(interviews)}位Agent关于{interview_requirement}的看法。"
