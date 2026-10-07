"""Integration clients for external services (LLM, knowledge base, etc.)."""

from app.integrations.llm_client import LLMClient, LLMClientError, MockLLMClient

__all__ = ["LLMClient", "LLMClientError", "MockLLMClient"]
# integrations 子包:三方系统接入(后续对接外部 LLM/DB/对象存储等扩展点)。
