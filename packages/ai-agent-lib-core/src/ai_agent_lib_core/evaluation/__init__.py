"""Measuring how well an agent does its job, off the request path.

Unit tests prove the plumbing with a scripted model. An eval runs the real
model over a dataset of cases and scores what comes back::

    cases = load_cases(SERVICE / "evals" / "triage.jsonl")
    report = await run_eval(
        lambda case: triage(services, context, case.input["text"]),
        cases,
        [exact("category"), exact("urgent")],
    )
    report.require({"category": 0.9, "urgent": 0.85})

Most scores are exact matches on a structured output. :func:`judged` asks a
model, through the ``judge`` alias, for what cannot be matched. Nothing here
runs on the request path, and the request path never imports it.
"""

from ai_agent_lib_core.evaluation.baseline import (
    Regression,
    SavedReport,
    load_report,
    regressions,
    require_no_regression,
)
from ai_agent_lib_core.evaluation.calibration import (
    CalibrationReport,
    LabeledCase,
    calibrate,
)
from ai_agent_lib_core.evaluation.cases import EvalCase, dataset_digest, load_cases
from ai_agent_lib_core.evaluation.feedback import (
    FEEDBACK_SCHEMA,
    FeedbackRecord,
    JsonlFeedbackLog,
    load_feedback,
)
from ai_agent_lib_core.evaluation.online import (
    INTERACTION_SCHEMA,
    Interaction,
    as_cases,
    load_interactions,
    sampled,
    score_interactions,
    write_interactions,
)
from ai_agent_lib_core.evaluation.report import REPORT_SCHEMA, CaseResult, EvalReport
from ai_agent_lib_core.evaluation.run import run_eval
from ai_agent_lib_core.evaluation.scorers import Score, Scorer, Verdict, exact, judged, scored_by

__all__ = [
    "FEEDBACK_SCHEMA",
    "INTERACTION_SCHEMA",
    "REPORT_SCHEMA",
    "CalibrationReport",
    "CaseResult",
    "EvalCase",
    "EvalReport",
    "FeedbackRecord",
    "Interaction",
    "JsonlFeedbackLog",
    "LabeledCase",
    "Regression",
    "SavedReport",
    "Score",
    "Scorer",
    "Verdict",
    "as_cases",
    "calibrate",
    "dataset_digest",
    "exact",
    "judged",
    "load_cases",
    "load_feedback",
    "load_interactions",
    "load_report",
    "regressions",
    "require_no_regression",
    "run_eval",
    "sampled",
    "score_interactions",
    "scored_by",
    "write_interactions",
]
