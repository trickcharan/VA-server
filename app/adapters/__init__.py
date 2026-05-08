"""STS adapter factory — maps provider names to adapter classes."""

from app.adapters.base import STSAdapter


def create_adapter(provider: str, **kwargs) -> STSAdapter:
    """Create an STS adapter instance for the given provider.

    Args:
        provider: Provider name (e.g. "google_live").
        **kwargs: Passed to the adapter constructor.

    Returns:
        An STSAdapter implementation.
    """
    if provider == "google_live":
        from app.adapters.google_live.adapter import GoogleLiveAdapter
        return GoogleLiveAdapter(**kwargs)
    # elif provider == "openai_realtime":
    #     from app.adapters.openai_realtime.adapter import OpenAIRealtimeAdapter
    #     return OpenAIRealtimeAdapter(**kwargs)
    raise ValueError(f"Unknown STS provider: {provider}")
