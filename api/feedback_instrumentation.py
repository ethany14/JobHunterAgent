"""Record explicit product actions without making feedback learning a precondition."""
import logging

from agent_runtime.feedback.types import FeedbackSourceType
from api.session_dependencies import SessionRuntime

logger = logging.getLogger(__name__)


def record_action(runtime: SessionRuntime, *, source_type: FeedbackSourceType,
                  source_action_id: str, content: str | None = None,
                  before: str | None = None, after: str | None = None,
                  application_id: str | None = None,
                  artifact_id: str | None = None,
                  context_metadata_json: dict | None = None) -> None:
    if getattr(runtime, "feedback", None) is None or getattr(runtime, "owner_resolver", None) is None:
        return
    try:
        runtime.feedback.record(owner_id=runtime.owner_resolver.resolve().owner_id,
            source_type=source_type, source_action_id=source_action_id,
            original_content=content, before_content=before, after_content=after,
            application_id=application_id, artifact_id=artifact_id,
            context_metadata_json=context_metadata_json)
    except Exception:
        # The primary action already committed. Do not echo candidate data or source text.
        logger.warning("Feedback capture failed for a completed user action")
