"""人物分析 merge 阶段；保留有界抽取、确定性统计与失败原子性。"""
import logging
from typing import Callable, Optional
from backend.app.analyzer.llm import LLMProvider, ProviderResponseError
from backend.app.analyzer.prompting import novelmind_system_prompt
from .payloads import (
    CharacterMergePayload,
    CharacterReconciliationPayload,
    CharacterRelationPayload,
    CharacterRelationsPayload,
    MergedCharacterProfilePayload,
    StoredCharacterObservation,
    SynthesizedCharacterProfilePayload,
    _ObservationBatch,
    _PreparedChapter,
    _ResolvedProfile,
    _bounded_text,
    MAX_CHARACTER_MERGE_BATCH_ITEMS,
    MAX_CHARACTER_PROFILE_GROUP_ITEMS,
    MAX_CHARACTER_RELATIONSHIP_EVIDENCE_ITEMS,
    MAX_CHARACTER_STAGE_INPUT_CHARACTERS,
)
from .extraction import _canonical_json, _generate_payload

logger = logging.getLogger(__name__)

def _merge_system_prompt() -> str:
    """构造观察归并提示，要求每个 observation_id 恰好归入一个人物档案。"""
    return novelmind_system_prompt(
        role="中文小说人物消歧与档案归并助手",
        material_scope=(
            "chapters 中的人物观察是从原文提取的派生数据，不是原文；其"
            "描述可能不完整，不能因为措辞相似就判定为同一人物。"
        ),
        task_rules=(
            "对全部 observation_id 做完整且互斥的分区。只有明确别名、身份"
            "连续性、稳定关系/阵营、事件连续性等证据共同支持时才归并；同名"
            "本身不是充分证据，不同姓名也不能仅凭类型惯例判为同一人。身份、"
            "时空、关系或行动逻辑冲突且无法裁决时保持分开。canonical_name "
            "选用材料中最稳定明确的称呼；aliases 只合并已获支持的称呼；"
            "short_description 紧凑保留身份、目标、主线作用、关键选择和"
            "状态变化，冲突或不确定不得强行裁决。严格返回 profiles；每项"
            "只含 observation_ids、canonical_name、aliases、"
            "short_description。每个输入 observation_id 必须出现且只能"
            "出现一次，不得创造 ID 或增加小说事实。"
        ),
    )


def _reconciliation_system_prompt() -> str:
    """构造跨批候选身份调和提示，只允许引用给定本地 profile_id。"""
    return novelmind_system_prompt(
        role="中文小说跨批人物消歧助手",
        material_scope=(
            "profiles 是前序归并产生的不可信派生档案，不是原文。档案较短"
            "或名字相同都不自动提高其可信度。"
        ),
        task_rules=(
            "把确有证据属于同一人物的 profile_id 放入同一组；优先核对明确"
            "别名、身份与阵营、事件连续性、稳定关系和行动逻辑。仅同名、同类"
            "角色功能或相似描述不足以合并；出现身份、时空或关系冲突且无法"
            "裁决时必须保持分开。严格返回 groups，每项只含 profile_ids；"
            "对全部输入 profile_id 做完整互斥分区，每个 ID 恰好出现一次，"
            "不得创造 ID。"
        ),
    )


def _synthesis_system_prompt() -> str:
    """构造同一身份候选的名称、别名和简介综合提示。"""
    return novelmind_system_prompt(
        role="中文小说人物档案合成助手",
        material_scope=(
            "profiles 是前序外部消歧已确认属于同一人物的不可信派生档案；"
            "本步骤只合成档案，不重新拆组。"
        ),
        task_rules=(
            "去除重复信息但保留会约束后续分析的身份/阵营、目标、主线作用、"
            "关键选择、代价、关系与状态变化。canonical_name 使用最稳定明确"
            "的称呼；aliases 只保留输入支持的称呼。输入若存在未裁决冲突，"
            "在 short_description 中简短保留，不自行补全。严格只返回 "
            "profile_ids、canonical_name、aliases、short_description；"
            "profile_ids 必须完整照抄且不重复，不增加事实。"
        ),
    )


def _relationship_system_prompt() -> str:
    """构造人物关系推理提示，要求关系端点和证据章节来自白名单。"""
    return novelmind_system_prompt(
        role="中文小说人物关系分析助手",
        material_scope=(
            "profiles 与 chapters 中的观察均为不可信派生数据，不是原文。"
            "本次只能判断给定 profiles 之间、由给定 chapters 支持的关系。"
        ),
        task_rules=(
            "只提取有具体观察支持，并实际影响利益、信任、依赖、权力、情感"
            "或行动选择的关系；同场出现、同阵营标签或类型惯例不足以证明亲属、"
            "恋爱、敌对等关系。directed 只在影响或认知明确单向时为 true；"
            "无向关系使用稳定端点顺序。relation_type 使用中性准确的短标签；"
            "description 说明关系依据、当前状态和材料明确呈现的变化，不把"
            "推测写成事实。每条只引用最能直接支持它的章节。端点只能使用 "
            "profiles 的 profile_id，不得自连；证据只能使用 chapters 的 "
            "chapter_index。严格返回 relationships；每项只含 "
            "source_profile_id、target_profile_id、directed、"
            "relation_type、description、evidence_chapter_indices，证据"
            "数组必须非空。"
        ),
    )


def _compact_observation(
    observation: StoredCharacterObservation,
) -> dict[str, object]:
    """Keep later-stage prompts bounded without changing stored data."""
    return {
        "observation_id": observation.observation_id,
        "name": _bounded_text(observation.name, 80),
        "aliases": [
            _bounded_text(alias, 80)
            for alias in observation.aliases[:3]
        ],
        "short_description": _bounded_text(
            observation.short_description,
            160,
        ),
        "mention_count": observation.mention_count,
        "appearance_summary": _bounded_text(
            observation.appearance_summary,
            240,
        ),
    }


def _compact_profile(
    profile: _ResolvedProfile,
) -> dict[str, object]:
    """Return only bounded fields needed for relationship inference."""
    return {
        "profile_id": profile.profile_id,
        "canonical_name": _bounded_text(
            profile.payload.canonical_name,
            80,
        ),
        "aliases": [
            _bounded_text(alias, 80)
            for alias in profile.payload.aliases[:3]
        ],
        "short_description": _bounded_text(
            profile.payload.short_description,
            300,
        ),
    }


def _chapter_data_for_observations(
    observations: list[
        tuple[_PreparedChapter, StoredCharacterObservation]
    ],
    *,
    include_titles: bool,
) -> list[dict[str, object]]:
    """按 observation ID 聚合所涉章节/片段信息，供人物归并提示提供证据上下文。"""
    chapters_by_id: dict[int, dict[str, object]] = {}
    for item, observation in observations:
        chapter_data = chapters_by_id.setdefault(
            item.chapter.id,
            {
                "chapter_index": item.chapter.chapter_index,
                **(
                    {
                        "chapter_title": _bounded_text(
                            item.chapter.title,
                            255,
                        )
                    }
                    if include_titles
                    else {}
                ),
                "observations": [],
            },
        )
        chapter_observations = chapter_data["observations"]
        if not isinstance(chapter_observations, list):
            raise RuntimeError("invalid local character batch")
        chapter_observations.append(
            _compact_observation(observation)
        )
    return list(chapters_by_id.values())


def _merge_request_data(
    observations: list[
        tuple[_PreparedChapter, StoredCharacterObservation]
    ],
    *,
    batch_index: int,
    batch_count: int,
) -> dict[str, object]:
    """把一个观察批次和对应章节上下文组装为人物归并请求 JSON。"""
    return {
        "batch_index": batch_index,
        "batch_count": batch_count,
        "chapters": _chapter_data_for_observations(
            observations,
            include_titles=True,
        ),
    }


def _relationship_request_data(
    observations: list[
        tuple[_PreparedChapter, StoredCharacterObservation]
    ],
    profiles: list[_ResolvedProfile],
    *,
    batch_index: int,
    batch_count: int,
) -> dict[str, object]:
    """把候选人物与有限章节证据组装为关系推理请求 JSON。"""
    return {
        "batch_index": batch_index,
        "batch_count": batch_count,
        "profiles": [
            _compact_profile(profile) for profile in profiles
        ],
        "chapters": _chapter_data_for_observations(
            observations,
            include_titles=False,
        ),
    }


def _observation_batches(
    prepared: list[_PreparedChapter],
) -> list[_ObservationBatch]:
    """Partition observations under explicit count and size bounds."""
    batches: list[_ObservationBatch] = []
    current: list[
        tuple[_PreparedChapter, StoredCharacterObservation]
    ] = []
    for item in prepared:
        for observation in item.extraction_data.observations:
            candidate = [*current, (item, observation)]
            candidate_size = len(
                _canonical_json(
                    _merge_request_data(
                        candidate,
                        batch_index=999_999,
                        batch_count=999_999,
                    )
                )
            )
            if (
                current
                and len(candidate) > MAX_CHARACTER_MERGE_BATCH_ITEMS
            ):
                batches.append(
                    _ObservationBatch(observations=tuple(current))
                )
                current = [(item, observation)]
            else:
                if (
                    candidate_size
                    > MAX_CHARACTER_STAGE_INPUT_CHARACTERS
                ):
                    raise ProviderResponseError()
                current = candidate
            if len(
                _canonical_json(
                    _merge_request_data(
                        current,
                        batch_index=999_999,
                        batch_count=999_999,
                    )
                )
            ) > MAX_CHARACTER_STAGE_INPUT_CHARACTERS:
                raise ProviderResponseError()
    if current:
        batches.append(_ObservationBatch(observations=tuple(current)))
    return batches


def _validate_partition(
    payload: CharacterMergePayload,
    expected_observation_ids: set[str],
    *,
    profile_id_prefix: str = "profile",
) -> list[_ResolvedProfile]:
    """验证归并结果对 expected_ids 不重不漏，并分配本地 profile_id。"""
    seen: set[str] = set()
    resolved: list[_ResolvedProfile] = []
    for index, profile in enumerate(payload.profiles, start=1):
        for observation_id in profile.observation_ids:
            if (
                observation_id not in expected_observation_ids
                or observation_id in seen
            ):
                raise ProviderResponseError()
            seen.add(observation_id)
        resolved.append(
            _ResolvedProfile(
                profile_id=f"{profile_id_prefix}-{index:04d}",
                payload=profile,
            )
        )
    if seen != expected_observation_ids:
        raise ProviderResponseError()
    return resolved


def _profile_groups(
    profiles: list[_ResolvedProfile],
) -> list[list[_ResolvedProfile]]:
    """按固定上限把候选档案顺序分组，供跨批身份调和。"""
    return [
        profiles[start : start + MAX_CHARACTER_PROFILE_GROUP_ITEMS]
        for start in range(
            0,
            len(profiles),
            MAX_CHARACTER_PROFILE_GROUP_ITEMS,
        )
    ]


def _validate_profile_partition(
    payload: CharacterReconciliationPayload,
    expected_ids: set[str],
) -> list[set[str]]:
    """验证身份组对 expected_ids 严格分区，返回每组 ID 集合。"""
    seen: set[str] = set()
    groups: list[set[str]] = []
    for group in payload.groups:
        identifiers = set(group.profile_ids)
        if (
            len(identifiers) != len(group.profile_ids)
            or not identifiers <= expected_ids
            or identifiers & seen
        ):
            raise ProviderResponseError()
        seen.update(identifiers)
        groups.append(identifiers)
    if seen != expected_ids:
        raise ProviderResponseError()
    return groups


def _reconciliation_requests(
    profiles: list[_ResolvedProfile],
) -> list[list[_ResolvedProfile]]:
    """构造相邻及边界候选比较组，控制跨批身份调和提示规模。"""
    groups = _profile_groups(profiles)
    return [
        (
            list(groups[left])
            if left == right
            else [*groups[left], *groups[right]]
        )
        for left in range(len(groups))
        for right in range(left, len(groups))
        if left != right or len(groups[left]) > 1
    ]


def _same_identity_decisions(
    profiles: list[_ResolvedProfile],
    provider: LLMProvider,
    *,
    should_continue: Optional[Callable[[], bool]],
    on_generation_step: Optional[Callable[[], None]],
) -> dict[frozenset[str], bool]:
    """调用模型判断候选档案是否属于同一人物。

    profiles 是本地候选，provider 执行严格分区响应，should_continue 负责取消，
    record_generation_step 更新进度。返回以 frozenset 两 ID 为键的对称布尔映射；
    不在本地凭名字相似度猜身份。
    """
    decisions: dict[frozenset[str], bool] = {}
    requests = _reconciliation_requests(profiles)
    for request_index, candidates in enumerate(requests, start=1):
        request_data = {
            "comparison_index": request_index,
            "comparison_count": len(requests),
            "profiles": [
                _compact_profile(profile) for profile in candidates
            ],
        }
        if (
            len(_canonical_json(request_data))
            > MAX_CHARACTER_STAGE_INPUT_CHARACTERS
        ):
            raise ProviderResponseError()
        payload = _generate_payload(
            provider=provider,
            system_prompt=_reconciliation_system_prompt(),
            user_prompt=(
                "请完整分区下一行 JSON 中的候选人物档案：\n"
                f"{_canonical_json(request_data)}"
            ),
            response_model=CharacterReconciliationPayload,
            should_continue=should_continue,
            on_generation_step=on_generation_step,
        )
        candidate_ids = {
            profile.profile_id for profile in candidates
        }
        partition = _validate_profile_partition(
            payload,
            candidate_ids,
        )
        group_by_id = {
            profile_id: group_index
            for group_index, group in enumerate(partition)
            for profile_id in group
        }
        for left in range(len(candidates)):
            for right in range(left + 1, len(candidates)):
                pair = frozenset(
                    (
                        candidates[left].profile_id,
                        candidates[right].profile_id,
                    )
                )
                same = (
                    group_by_id[candidates[left].profile_id]
                    == group_by_id[candidates[right].profile_id]
                )
                previous = decisions.setdefault(pair, same)
                if previous != same:
                    raise ProviderResponseError()
    return decisions


def _complete_link_identity_clusters(
    profiles: list[_ResolvedProfile],
    decisions: dict[frozenset[str], bool],
) -> list[list[_ResolvedProfile]]:
    """用 complete-link 聚类：候选须与簇内每个成员都判为同一身份。"""
    clusters: list[list[_ResolvedProfile]] = []
    for profile in profiles:
        for cluster in clusters:
            if all(
                decisions.get(
                    frozenset((profile.profile_id, member.profile_id))
                )
                is True
                for member in cluster
            ):
                cluster.append(profile)
                break
        else:
            clusters.append([profile])
    return clusters


def _synthesize_identity_cluster(
    cluster: list[_ResolvedProfile],
    provider: LLMProvider,
    *,
    cluster_index: int,
    should_continue: Optional[Callable[[], bool]],
    on_generation_step: Optional[Callable[[], None]],
) -> _ResolvedProfile:
    """把一个确认同一身份的候选簇综合为单一规范档案。

    cluster 可能仍超过一次提示预算，因此函数分层合并；每轮要求模型返回精确的
    profile_id 分区并保留所有 observation_ids。返回新的 ResolvedProfile，ID 由
    本地确定，文本由模型生成并经 Pydantic 校验。
    """
    current = list(cluster)
    round_index = 0
    while len(current) > 1:
        round_index += 1
        reduced: list[_ResolvedProfile] = []
        for batch_index, start in enumerate(
            range(0, len(current), MAX_CHARACTER_PROFILE_GROUP_ITEMS),
            start=1,
        ):
            batch = current[
                start : start + MAX_CHARACTER_PROFILE_GROUP_ITEMS
            ]
            if len(batch) == 1:
                reduced.append(batch[0])
                continue
            request_data = {
                "profiles": [
                    _compact_profile(profile) for profile in batch
                ]
            }
            if (
                len(_canonical_json(request_data))
                > MAX_CHARACTER_STAGE_INPUT_CHARACTERS
            ):
                raise ProviderResponseError()
            payload = _generate_payload(
                provider=provider,
                system_prompt=_synthesis_system_prompt(),
                user_prompt=(
                    "请整合下一行 JSON 中已确认属于同一人物的档案：\n"
                    f"{_canonical_json(request_data)}"
                ),
                response_model=SynthesizedCharacterProfilePayload,
                should_continue=should_continue,
                on_generation_step=on_generation_step,
            )
            expected_ids = {profile.profile_id for profile in batch}
            if set(payload.profile_ids) != expected_ids:
                raise ProviderResponseError()
            observation_ids = [
                observation_id
                for profile in batch
                for observation_id in profile.payload.observation_ids
            ]
            reduced.append(
                _ResolvedProfile(
                    profile_id=(
                        f"synthesis-{cluster_index:04d}-"
                        f"{round_index:04d}-{batch_index:04d}"
                    ),
                    payload=MergedCharacterProfilePayload(
                        observation_ids=observation_ids,
                        canonical_name=payload.canonical_name,
                        aliases=list(payload.aliases),
                        short_description=payload.short_description,
                    ),
                )
            )
        current = reduced
    return current[0]


def _reconcile_profiles_with_api(
    profiles: list[_ResolvedProfile],
    provider: LLMProvider,
    *,
    should_continue: Optional[Callable[[], bool]],
    on_generation_step: Optional[Callable[[], None]],
) -> list[_ResolvedProfile]:
    """跨归并批次调和同一人物，并综合多候选身份簇。"""
    if not profiles:
        return []
    decisions = _same_identity_decisions(
        profiles,
        provider,
        should_continue=should_continue,
        on_generation_step=on_generation_step,
    )
    clusters = _complete_link_identity_clusters(
        profiles,
        decisions,
    )
    reconciled = [
        _synthesize_identity_cluster(
            cluster,
            provider,
            cluster_index=index,
            should_continue=should_continue,
            on_generation_step=on_generation_step,
        )
        for index, cluster in enumerate(clusters, start=1)
    ]
    return [
        _ResolvedProfile(
            profile_id=f"profile-{index:04d}",
            payload=profile.payload,
        )
        for index, profile in enumerate(reconciled, start=1)
    ]


def _merge_profiles(
    prepared: list[_PreparedChapter],
    provider: LLMProvider,
    *,
    should_continue: Optional[Callable[[], bool]],
    on_generation_step: Optional[Callable[[], None]],
) -> tuple[list[_ResolvedProfile], list[_ObservationBatch]]:
    """把全部逐章观察归并为全书规范人物。

    prepared 是有序章节缓存；函数先按提示预算生成 ObservationBatch，各批严格
    验证 observation_id 不重不漏，再跨批身份调和。返回 ``(最终档案, 原批次)``，
    后者继续为关系证据范围和本地统计提供来源。
    """
    batches = _observation_batches(prepared)
    batched_profiles: list[_ResolvedProfile] = []
    for batch_index, batch in enumerate(batches, start=1):
        request_data = _merge_request_data(
            list(batch.observations),
            batch_index=batch_index,
            batch_count=len(batches),
        )
        payload = _generate_payload(
            provider=provider,
            system_prompt=_merge_system_prompt(),
            user_prompt=(
                "请归并下一行 JSON 中的这一批全书人物观察，并完整分区全部 "
                "observation_id：\n"
                f"{_canonical_json(request_data)}"
            ),
            response_model=CharacterMergePayload,
            should_continue=should_continue,
            on_generation_step=on_generation_step,
        )
        batched_profiles.extend(
            _validate_partition(
                payload,
                batch.observation_ids,
                profile_id_prefix=f"batch-{batch_index:04d}-profile",
            )
        )
    return (
        _reconcile_profiles_with_api(
            batched_profiles,
            provider,
            should_continue=should_continue,
            on_generation_step=on_generation_step,
        ),
        batches,
    )


def _relationship_evidence_batches(
    profiles: list[_ResolvedProfile],
    observations: list[
        tuple[_PreparedChapter, StoredCharacterObservation]
    ],
    profile_by_observation_id: dict[str, _ResolvedProfile],
) -> list[list[
    tuple[_PreparedChapter, StoredCharacterObservation]
]]:
    """按提示字符预算构造关系推理的章节证据批次。

    prepared 提供逐章观察，profiles 给出合法人物白名单，base_request_characters
    预留固定人物档案开销。返回批次保留章节序号和局部观察；未知 ID、空证据不会
    被发给关系模型。
    """
    known_ids = {profile.profile_id for profile in profiles}
    relevant = [
        pair
        for pair in observations
        if profile_by_observation_id[
            pair[1].observation_id
        ].profile_id
        in known_ids
    ]
    batches: list[list[
        tuple[_PreparedChapter, StoredCharacterObservation]
    ]] = []
    current: list[
        tuple[_PreparedChapter, StoredCharacterObservation]
    ] = []
    for pair in relevant:
        candidate = [*current, pair]
        request_size = len(
            _canonical_json(
                _relationship_request_data(
                    candidate,
                    profiles,
                    batch_index=999_999,
                    batch_count=999_999,
                )
            )
        )
        if (
            current
            and len(candidate)
            > MAX_CHARACTER_RELATIONSHIP_EVIDENCE_ITEMS
        ):
            batches.append(current)
            current = [pair]
        else:
            if request_size > MAX_CHARACTER_STAGE_INPUT_CHARACTERS:
                raise ProviderResponseError()
            current = candidate
        if len(
            _canonical_json(
                _relationship_request_data(
                    current,
                    profiles,
                    batch_index=999_999,
                    batch_count=999_999,
                )
            )
        ) > MAX_CHARACTER_STAGE_INPUT_CHARACTERS:
            raise ProviderResponseError()
    if current:
        batches.append(current)
    return batches or [[]]


def _relationship_call_specs(
    profiles: list[_ResolvedProfile],
    source_batches: list[_ObservationBatch],
) -> list[tuple[
    list[_ResolvedProfile],
    list[tuple[_PreparedChapter, StoredCharacterObservation]],
]]:
    """组合人物分组和证据批次，返回每次关系模型调用的参数规格。"""
    groups = _profile_groups(profiles)
    all_observations = [
        pair
        for batch in source_batches
        for pair in batch.observations
    ]
    profile_by_observation_id = {
        observation_id: profile
        for profile in profiles
        for observation_id in profile.payload.observation_ids
    }
    specs: list[tuple[
        list[_ResolvedProfile],
        list[tuple[_PreparedChapter, StoredCharacterObservation]],
    ]] = []
    for left in range(len(groups)):
        for right in range(left, len(groups)):
            request_profiles = (
                list(groups[left])
                if left == right
                else [*groups[left], *groups[right]]
            )
            for evidence in _relationship_evidence_batches(
                request_profiles,
                all_observations,
                profile_by_observation_id,
            ):
                specs.append((request_profiles, evidence))
    return specs


def _infer_relationships(
    profiles: list[_ResolvedProfile],
    batches: list[_ObservationBatch],
    provider: LLMProvider,
    *,
    should_continue: Optional[Callable[[], bool]],
    on_generation_step: Optional[Callable[[], None]],
) -> list[CharacterRelationPayload]:
    """推理并合并整书人物关系。

    profiles 是合法端点，prepared/observation_batches 提供证据，provider 执行
    结构化生成。每次结果验证端点、关系方向和证据章节白名单；同一边重复出现时
    本地合并证据而不重复保存。返回最终 CharacterRelationPayload 列表。
    """
    if not profiles:
        return []
    specs = _relationship_call_specs(profiles, batches)
    edge_indices: dict[tuple[str, str, bool, str], int] = {}
    validated: list[CharacterRelationPayload] = []
    for batch_index, (batch_profiles, evidence) in enumerate(
        specs,
        start=1,
    ):
        request_data = _relationship_request_data(
            evidence,
            batch_profiles,
            batch_index=batch_index,
            batch_count=len(specs),
        )
        payload = _generate_payload(
            provider=provider,
            system_prompt=_relationship_system_prompt(),
            user_prompt=(
                "请根据下一行 JSON 的本地人物 ID 和派生观察推断关系：\n"
                f"{_canonical_json(request_data)}"
            ),
            response_model=CharacterRelationsPayload,
            should_continue=should_continue,
            on_generation_step=on_generation_step,
        )
        known_profiles = {
            profile.profile_id for profile in batch_profiles
        }
        valid_chapters = {
            item.chapter.chapter_index
            for item, _ in evidence
        }
        seen_in_batch: set[tuple[str, str, bool, str]] = set()
        for relation in payload.relationships:
            if (
                relation.source_profile_id not in known_profiles
                or relation.target_profile_id not in known_profiles
                or not set(relation.evidence_chapter_indices)
                <= valid_chapters
            ):
                raise ProviderResponseError()
            source = relation.source_profile_id
            target = relation.target_profile_id
            if not relation.directed and source > target:
                source, target = target, source
                relation = relation.model_copy(
                    update={
                        "source_profile_id": source,
                        "target_profile_id": target,
                    }
                )
            edge = (
                source,
                target,
                bool(relation.directed),
                relation.relation_type,
            )
            if edge in seen_in_batch:
                raise ProviderResponseError()
            seen_in_batch.add(edge)
            existing_index = edge_indices.get(edge)
            if existing_index is None:
                edge_indices[edge] = len(validated)
                validated.append(relation)
                continue
            existing = validated[existing_index]
            evidence = list(
                dict.fromkeys(
                    [
                        *existing.evidence_chapter_indices,
                        *relation.evidence_chapter_indices,
                    ]
                )
            )
            validated[existing_index] = existing.model_copy(
                update={"evidence_chapter_indices": evidence}
            )
    return validated
