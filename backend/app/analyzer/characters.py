"""人物分析公共接口；阶段内部函数由各实现模块直接引用。"""

from .character_pipeline.extraction import (
    character_analysis_source_hash,
    character_chapter_source_hash,
    estimate_character_extraction_steps,
    maximum_character_generation_steps,
)
from .character_pipeline.payloads import (
    CHARACTER_EXTRACTION_PROMPT_VERSION,
    CHARACTER_MERGE_PROMPT_VERSION,
    CHARACTER_RELATIONSHIP_PROMPT_VERSION,
    ChapterCharacterExtractionPayload,
    CharacterMergePayload,
    CharacterReconciliationPayload,
    CharacterRelationPayload,
    CharacterRelationsPayload,
    MAX_CHARACTER_STAGE_INPUT_CHARACTERS,
    StoredChapterExtractionData,
    SynthesizedCharacterProfilePayload,
)
from .character_pipeline.persistence import (
    analyze_novel_characters,
)
from .character_pipeline.worker import (
    run_character_job,
)

__all__ = ['CHARACTER_EXTRACTION_PROMPT_VERSION', 'CHARACTER_MERGE_PROMPT_VERSION', 'CHARACTER_RELATIONSHIP_PROMPT_VERSION', 'ChapterCharacterExtractionPayload', 'CharacterMergePayload', 'CharacterReconciliationPayload', 'CharacterRelationPayload', 'CharacterRelationsPayload', 'MAX_CHARACTER_STAGE_INPUT_CHARACTERS', 'StoredChapterExtractionData', 'SynthesizedCharacterProfilePayload', 'analyze_novel_characters', 'character_analysis_source_hash', 'character_chapter_source_hash', 'estimate_character_extraction_steps', 'maximum_character_generation_steps', 'run_character_job']
