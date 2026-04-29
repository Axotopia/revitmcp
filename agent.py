import os
from dotenv import load_dotenv
from langchain_ollama import ChatOllama
from langgraph.prebuilt import create_react_agent
from langgraph.checkpoint.memory import MemorySaver
from tools import ALL_TOOLS

# Load .env file
load_dotenv()

OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen3.6:35b")
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")

llm = ChatOllama(
    model=OLLAMA_MODEL,
    base_url=OLLAMA_BASE_URL,
    temperature=0.1,
)

memory = MemorySaver()

react_agent = create_react_agent(
    model=llm,
    tools=ALL_TOOLS,
    checkpointer=memory,
    name="axoworks_react_agent",
)
