"""
recova/pipeline.py — Orchestrates classify → decide → execute for a single event.

Called by:
  - api/main.py (webhook receiver, live events)
  - scripts/batch_runner.py (synthetic events, bulk testing)

Returns a PipelineResult so both callers can log or assert on the outcome.
"""

import logging
from recova import classifier, decision_engine, execution
from recova.models import PipelineResult

logger = logging.getLogger(__name__)


def process_failed_payment(event_id: int) -> PipelineResult:
    """
    Run the full pipeline for one failed payment event.

    Steps:
      1. Classify the decline type
      2. Decide an intervention (rules-based)
      3. Execute the intervention (API call or mock log)

    Any step that raises an exception is caught and recorded in PipelineResult.error
    so the caller can continue processing other events rather than aborting the batch.
    """
    result = PipelineResult(event_id=event_id)

    # Step 1 — Classify
    try:
        classification_id = classifier.classify(event_id)
        if classification_id is None:
            result.error = "classify() returned None — event not found or not a failure"
            logger.warning("[pipeline] event=%d: %s", event_id, result.error)
            return result
        result.classification_id = classification_id
    except Exception as exc:
        result.error = f"classifier raised: {exc}"
        logger.exception("[pipeline] event=%d classify error", event_id)
        return result

    # Step 2 — Decide
    try:
        decision_id = decision_engine.decide(classification_id)
        if decision_id is None:
            result.error = "decide() returned None — classification not found"
            logger.warning("[pipeline] event=%d: %s", event_id, result.error)
            return result
        result.decision_id = decision_id
    except Exception as exc:
        result.error = f"decision_engine raised: {exc}"
        logger.exception("[pipeline] event=%d decide error", event_id)
        return result

    # Step 3 — Execute
    try:
        action_log_id = execution.execute(decision_id)
        if action_log_id is None:
            result.error = "execute() returned None — decision not found"
            logger.warning("[pipeline] event=%d: %s", event_id, result.error)
            return result
        result.action_log_id = action_log_id
    except Exception as exc:
        result.error = f"execution raised: {exc}"
        logger.exception("[pipeline] event=%d execute error", event_id)
        return result

    logger.info(
        "[pipeline] event=%d complete: cls=%d dec=%d act=%d",
        event_id, classification_id, decision_id, action_log_id,
    )
    return result
