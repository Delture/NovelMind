"""Novel analysis providers and shared contracts."""

from backend.app.analyzer.characters import (
    CHARACTER_EXTRACTION_PROMPT_VERSION,
    CHARACTER_MERGE_PROMPT_VERSION,
    CHARACTER_RELATIONSHIP_PROMPT_VERSION,
    analyze_novel_characters,
    character_analysis_source_hash,
    character_chapter_source_hash,
    estimate_character_extraction_steps,
    run_character_job,
)
from backend.app.analyzer.deepseek import DeepSeekProvider
from backend.app.analyzer.llm import (
    JsonGenerationResult,
    LLMProvider,
    LLMProviderError,
    ProviderAuthenticationError,
    ProviderBalanceError,
    ProviderNotConfiguredError,
    ProviderRateLimitError,
    ProviderRequestError,
    ProviderResponseError,
    ProviderUnavailableError,
)


__all__ = [
    "CHARACTER_EXTRACTION_PROMPT_VERSION",
    "CHARACTER_MERGE_PROMPT_VERSION",
    "CHARACTER_RELATIONSHIP_PROMPT_VERSION",
    "DeepSeekProvider",
    "JsonGenerationResult",
    "LLMProvider",
    "LLMProviderError",
    "ProviderAuthenticationError",
    "ProviderBalanceError",
    "ProviderNotConfiguredError",
    "ProviderRateLimitError",
    "ProviderRequestError",
    "ProviderResponseError",
    "ProviderUnavailableError",
    "analyze_novel_characters",
    "character_analysis_source_hash",
    "character_chapter_source_hash",
    "estimate_character_extraction_steps",
    "run_character_job",
]
