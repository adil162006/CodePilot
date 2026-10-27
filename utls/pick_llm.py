from dotenv import load_dotenv

load_dotenv()
from langchain_groq import ChatGroq


def pick_llm(level: str, temperature: int = 0, max_retries: int = 0) -> ChatGroq:
    """
        Select a Groq LLM based on question difficulty.

        low    -> Faster, lightweight model
        medium -> Balanced reasoning model
        high   -> More capable reasoning model
    """

    models = {
        "low": "openai/gpt-oss-20b",
        "medium": "qwen/qwen3.8-27b",
        "high": "openai/gpt-oss-120b",
    }

    level = level.lower().strip()

    if level not in models:
        raise ValueError(
            "Invalid level. Choose 'low', 'medium', or 'high'."
        )

    return ChatGroq(model=models[level], temperature=temperature, max_retries=max_retries)


def get_context_window_size(model_name: str):
    models = {
        "openai/gpt-oss-20b": 131072,
        "qwen/qwen3.8-27b": 131072,
        "openai/gpt-oss-120b": 131072,
    }

    return models[model_name]
