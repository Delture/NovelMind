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

def _merge_system_prompt() -> str:
    return novelmind_system_prompt(
        role="中文小说人物消歧与档案归并助手",
        material_scope=(
            "输入的 chapters 包含从各章节提取的人物观察记录（含 observation_id）。"
            "由于单章观察受限于局部视角，同一人物在不同章节可能以本名、化名、职衔或"
            "不同阶段的状态出现；同时不同人物也可能拥有相似头衔或同名，不能仅因措辞"
            "相似就判定为同一人物。"
        ),
        task_rules=(
            "对全部输入的 observation_id 做完整且互斥的实体归并分区："
            "1. 归并判准（Entity Continuity Test）："
            "   - 应当归并：只有明确别名交集、身份连续性、稳定人际关系/阵营、连贯事件"
            "因果链或经后文证实的“马甲/伪装揭晓”共同支持时才归并。人物随剧情发展的职位"
            "升降、目标转移或立场转变属于正常人物弧光演进，应予归并。"
            "   - 保持分开：仅凭大众化泛称（如“黑衣人”“掌柜”）、无上下文支撑的同名或"
            "类型惯例，不足以合并；当同时空下存在无法用伪装或剧情演进解释的硬性身份冲突"
            "时，必须保持分开。"
            "2. 档案合成规范（严格返回 profiles 数组；每项只含 observation_ids、"
            "canonical_name、aliases、short_description）："
            "   - observation_ids：每个输入 observation_id 必须出现且只能出现一次，"
            "不得遗漏或创造 ID。"
            "   - canonical_name：选用材料中最稳定、最具代表性的核心称呼。"
            "   - aliases：只合并已获文本支撑的别名、化名或头衔。"
            "   - short_description：按叙事演进脉络紧凑保留身份/阵营（含“现为 X，前期"
            "以 Y 身份活动”的演进记录）、核心目标、主线作用、关键抉择和状态变化；对未"
            "揭晓的悬念或无法裁决的冲突如实保留。"
        ),
    )


def _reconciliation_system_prompt() -> str:
    return novelmind_system_prompt(
        role="中文小说跨批人物消歧助手",
        material_scope=(
            "输入的 profiles 是前序各批次分别归并产生的候选人物档案。同一人物在"
            "不同章节批次中可能因剧情发展而呈现不同的阶段性称呼、职衔或处境；档案"
            "较短或表面名字相同都不自动构成同一人证据。"
        ),
        task_rules=(
            "把确有证据属于同一人物的 profile_id 放入同一组："
            "1. 跨批核对重点：核对 canonical_name 与 aliases 的交叉印证、身份与"
            "阵营的演进承接、跨批事件连续性、稳定人际关系锚点以及前期化名/伪装在后期"
            "的揭晓。"
            "2. 拆分保护边界：仅凭泛称相同、同类角色功能或缺乏上下文交集的同名，不足"
            "以合并；出现无法由剧情演进或伪装揭晓解释的硬性身份、时空或关系冲突时，"
            "必须保持分开。"
            "3. 分区契约：严格返回 groups 数组，每项只含 profile_ids；对全部输入 "
            "profile_id 做完整且互斥的分区，每个 ID 恰好出现一次，不得遗漏或创造 ID。"
        ),
    )


def _synthesis_system_prompt() -> str:
    return novelmind_system_prompt(
        role="中文小说人物全貌档案合成助手",
        material_scope=(
            "输入的 profiles 是经前序消歧已确认属于同一人物的多阶段分片档案；本步骤"
            "专注于将该人物在不同阶段的轨迹融合为一份连贯、立体的人物总档案，不再重新拆组。"
        ),
        task_rules=(
            "1. 演进式整合（History Evolution）：去除分片间的重复静态描述，按剧情发展"
            "脉络串联保留该人物的【身份/阵营演进（如“现为 X，早期以 Y 身份活动”）→ "
            "核心目标与动机 → 主线作用 → 关键抉择与代价 → 关系与心理状态弧光】。"
            "2. 字段规范：canonical_name 使用最稳定明确的主称呼；aliases 只保留输入"
            "明确支持的别名与化名。输入若存在未揭晓的身世悬念或未裁决冲突，在 "
            "short_description 中简短如实保留，不自行脑补。严格只返回 profile_ids、"
            "canonical_name、aliases、short_description；profile_ids 必须完整照抄"
            "输入且不重复、不遗漏，不增加输入之外的事实。"
        ),
    )


def _relationship_system_prompt() -> str:
    return novelmind_system_prompt(
        role="中文小说人物关系与张力分析助手",
        material_scope=(
            "输入的 profiles 为候选人物档案列表（含 profile_id），chapters 为各章节的"
            "具体人物观察记录（含 chapter_index）。本次只能判断给定 profiles 之间、且由"
            "给定 chapters 观察明确支撑的人际关系。"
        ),
        task_rules=(
            "1. 关系成立判准（Relational Impact Test）：只提取有具体观察支持，并实际"
            "影响双方利益博弈、信任、依赖、权力从属、情感羁绊或行动抉择的实质关系；仅同场"
            "出现、同阵营标签或外部类型惯例不足以证明亲属、恋爱、敌对等关系。"
            "2. 字段与方向规范（严格返回 relationships 数组；每项只含 source_profile_id、"
            "target_profile_id、directed、relation_type、description、"
            "evidence_chapter_indices）："
            "   - 端点约束：source_profile_id 与 target_profile_id 只能使用输入 profiles "
            "的 profile_id，严禁自连。"
            "   - directed：只在影响、支配或认知明确单向时设为 true（如单向监视、单向暗恋、"
            "上下级统属）；双向互动或对等关系设为 false，并使用稳定端点顺序。"
            "   - relation_type：使用精准、中性的短标签（若关系发生重大反转，可使用“昔日"
            "同门→死敌”等演进标签）。"
            "   - description：紧凑说明关系建立依据、核心张力、当前状态以及材料明确呈现的"
            "动态变化或表里反差（如表面结盟实则互相提防），不把无据推测写成事实。"
            "   - evidence_chapter_indices：只引用输入 chapters 中最能直接支持该关系的 "
            "chapter_index，证据数组必须非空。"
        ),
    )


def _compact_observation(
    observation: StoredCharacterObservation,
) -> dict[str, object]:
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


def _resolved_merge_profiles(
    payload: CharacterMergePayload,
    *,
    profile_id_prefix: str = "profile",
) -> list[_ResolvedProfile]:
    return [
        _ResolvedProfile(
            profile_id=f"{profile_id_prefix}-{index:04d}",
            payload=profile,
        )
        for index, profile in enumerate(payload.profiles, start=1)
    ]


def _profile_groups(
    profiles: list[_ResolvedProfile],
) -> list[list[_ResolvedProfile]]:
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
    """不在本地凭名字相似度猜身份。"""
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
                "请比对并完整互斥地分区下一行 JSON 中的候选人物档案：\n"
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
    """簇可能超出单次提示预算，故分层合并。"""
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
                    "请整合下一行 JSON 中已确认属于同一人物的多阶段分片档案：\n"
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
    batches = _observation_batches(prepared)
    batched_profiles: list[_ResolvedProfile] = []
    for batch_index, batch in enumerate(batches, start=1):
        request_data = _merge_request_data(
            list(batch.observations),
            batch_index=batch_index,
            batch_count=len(batches),
        )
        profile_id_prefix = f"batch-{batch_index:04d}-profile"
        payload = _generate_payload(
            provider=provider,
            system_prompt=_merge_system_prompt(),
            user_prompt=(
                "请归并下一行 JSON 中的这一批全书人物观察，并按人物同一性完整且互斥地"
                "分区全部 observation_id：\n"
                f"{_canonical_json(request_data)}"
            ),
            response_model=CharacterMergePayload,
            should_continue=should_continue,
            on_generation_step=on_generation_step,
            validation_context={
                "expected_observation_ids": set(batch.observation_ids),
                "profile_id_prefix": profile_id_prefix,
            },
        )
        batched_profiles.extend(
            _resolved_merge_profiles(
                payload,
                profile_id_prefix=profile_id_prefix,
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
    """未知 ID 与空证据不会发给关系模型。"""
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
                "请根据下一行 JSON 的候选人物 ID（profiles）和章节观察证据（chapters），"
                "提取有实质交集支撑的人物关系：\n"
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
            combined_evidence = list(
                dict.fromkeys(
                    [
                        *existing.evidence_chapter_indices,
                        *relation.evidence_chapter_indices,
                    ]
                )
            )
            validated[existing_index] = existing.model_copy(
                update={"evidence_chapter_indices": combined_evidence}
            )
    return validated
