"""Assemble mandatory and optional context without provider dependencies."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from itertools import zip_longest

from agent_runtime.context.budget import ContextBudgetExceededError, estimate_tokens
from agent_runtime.context.types import AssembledContext, ContextBlock, ContextBlockKind, ContextBudget


class ContextAssembler:
    def assemble(
        self,
        *,
        system_policy: str,
        active_task: str,
        required_source_evidence: Sequence[str],
        budget: ContextBudget,
        memory_blocks: Sequence[ContextBlock] = (),
        optional_blocks: Sequence[ContextBlock] = (),
    ) -> AssembledContext:
        mandatory = [
            self._block("system-policy", ContextBlockKind.SYSTEM_POLICY, system_policy, True),
            self._block("active-task", ContextBlockKind.ACTIVE_TASK, active_task, True),
        ]
        mandatory.extend(
            self._block(f"required-evidence:{index}", ContextBlockKind.REQUIRED_SOURCE_EVIDENCE,
                        content, True)
            for index, content in enumerate(required_source_evidence, start=1)
        )
        available = budget.available_input_tokens
        mandatory_cost = sum(block.estimated_tokens for block in mandatory)
        if mandatory_cost > available:
            raise ContextBudgetExceededError(
                f"Mandatory context requires {mandatory_cost} tokens; budget allows {available}."
            )
        normalized_memory = [self._normalized(block, ContextBlockKind.MEMORY) for block in memory_blocks]
        normalized_optional = [self._normalized(block, ContextBlockKind.OPTIONAL) for block in optional_blocks]
        all_ids = [block.block_id for block in [*mandatory, *normalized_memory, *normalized_optional]]
        if len(all_ids) != len(set(all_ids)):
            raise ValueError("Context block IDs must be unique.")
        normalized_memory.sort(key=lambda block: (
            -block.priority, -self._timestamp(block.created_at), block.block_id
        ))
        normalized_optional.sort(key=lambda block: (
            -self._timestamp(block.created_at), -block.priority, block.block_id
        ))
        retained = [*normalized_memory, *normalized_optional]
        used = mandatory_cost + sum(block.estimated_tokens for block in retained)
        memory_evictions = sorted(normalized_memory, key=lambda block: (
            block.priority, self._timestamp(block.created_at), block.block_id
        ))
        optional_evictions = sorted(normalized_optional, key=lambda block: (
            self._timestamp(block.created_at), block.priority, block.block_id
        ))
        eviction_order = [
            block
            for pair in zip_longest(memory_evictions, optional_evictions)
            for block in pair
            if block is not None
        ]
        dropped: list[str] = []
        retained_ids = {block.block_id for block in retained}
        for block in eviction_order:
            if used <= available:
                break
            retained_ids.remove(block.block_id)
            dropped.append(block.block_id)
            used -= block.estimated_tokens
        selected = [
            *mandatory,
            *(block for block in normalized_memory if block.block_id in retained_ids),
            *(block for block in normalized_optional if block.block_id in retained_ids),
        ]
        return AssembledContext(blocks=selected, dropped_block_ids=dropped,
                                estimated_tokens=used, budget=budget)

    @staticmethod
    def _block(block_id: str, kind: ContextBlockKind, content: str, mandatory: bool) -> ContextBlock:
        cleaned = content.strip()
        return ContextBlock(block_id=block_id, kind=kind, content=cleaned,
                            mandatory=mandatory, estimated_tokens=estimate_tokens(cleaned))

    @staticmethod
    def _normalized(block: ContextBlock, expected_kind: ContextBlockKind) -> ContextBlock:
        values = block.model_dump(mode="python")
        values["kind"] = expected_kind
        values["mandatory"] = False
        values["estimated_tokens"] = estimate_tokens(block.content)
        return ContextBlock.model_validate(values)

    @staticmethod
    def _timestamp(value: datetime | None) -> float:
        if value is None:
            return 0.0
        aware = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
        return aware.timestamp()
