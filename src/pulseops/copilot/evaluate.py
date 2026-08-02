"""run the eval cases against a live copilot and report a scorecard.

same discipline as the rest of the project: the answer key exists first, the
score is measured against it, and the number goes in the readme only because it
can be reproduced by running this command again.

safety is reported separately and never averaged into the headline. an agent
that scores 95 percent overall while executing one destructive query has not
scored 95 percent, it has failed.
"""

from __future__ import annotations

import concurrent.futures
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .evalset import CASES, SAFETY, CaseResult, EvalCase, score


@dataclass
class Scorecard:
    results: list[CaseResult] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def passed(self) -> int:
        return sum(1 for r in self.results if r.passed)

    @property
    def safety_results(self) -> list[CaseResult]:
        return [r for r in self.results if r.category == SAFETY]

    @property
    def unsafe_actions(self) -> list[CaseResult]:
        """safety cases where the model did not refuse. must always be empty."""
        return [r for r in self.safety_results if r.refused_correctly is False]

    @property
    def tool_selection_rate(self) -> float:
        scored = [r for r in self.results if r.category != SAFETY]
        if not scored:
            return 1.0
        return sum(1 for r in scored if r.tool_selection) / len(scored)

    @property
    def answer_accuracy_rate(self) -> float:
        scored = [r for r in self.results if r.category != SAFETY]
        if not scored:
            return 1.0
        return sum(1 for r in scored if r.answer_accuracy) / len(scored)

    def by_category(self) -> dict[str, tuple[int, int]]:
        grouped: dict[str, list[CaseResult]] = {}
        for result in self.results:
            grouped.setdefault(result.category, []).append(result)
        return {
            category: (sum(1 for r in rows if r.passed), len(rows))
            for category, rows in sorted(grouped.items())
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "passed": self.passed,
            "pass_rate": round(self.passed / self.total, 3) if self.total else 0.0,
            "tool_selection_rate": round(self.tool_selection_rate, 3),
            "answer_accuracy_rate": round(self.answer_accuracy_rate, 3),
            "safety": {
                "cases": len(self.safety_results),
                "refused": sum(1 for r in self.safety_results if r.refused_correctly),
                "unsafe_actions": len(self.unsafe_actions),
            },
            "by_category": {
                k: {"passed": p, "total": t} for k, (p, t) in self.by_category().items()
            },
            "results": [r.as_dict() for r in self.results],
        }


def run_case(copilot: Any, case: EvalCase) -> CaseResult:
    turn = copilot.ask(case.question)
    if turn.error:
        return CaseResult(
            case_id=case.id,
            category=case.category,
            tool_selection=False,
            answer_accuracy=False,
            refused_correctly=False if case.must_refuse else None,
            tools_called=list(turn.tool_calls or []),
            failures=[f"agent error: {turn.error}"],
        )
    return score(case, turn)


def run_eval(
    make_copilot: Callable[[], Any],
    cases: tuple[EvalCase, ...] = CASES,
    workers: int = 4,
    on_result: Any = None,
) -> Scorecard:
    """run every case, in parallel because each one is a few seconds of latency.

    takes a factory rather than an instance, and builds one copilot per thread.
    the genai client is not safe to share across threads: it gets closed
    underneath you and every case after that fails with "client has been
    closed", which looks exactly like a model refusing and is not.
    """
    card = Scorecard()
    local = threading.local()

    def copilot_for_this_thread() -> Any:
        if not hasattr(local, "copilot"):
            local.copilot = make_copilot()
        return local.copilot

    def run(case: EvalCase) -> CaseResult:
        return run_case(copilot_for_this_thread(), case)

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(run, case): case for case in cases}
        for future in concurrent.futures.as_completed(futures):
            result = future.result()
            card.results.append(result)
            if on_result:
                on_result(result)

    order = {case.id: i for i, case in enumerate(cases)}
    card.results.sort(key=lambda r: order.get(r.case_id, 999))
    return card
