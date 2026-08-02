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


@dataclass
class RepeatedScorecard:
    """several runs of the same suite, kept apart rather than averaged blindly.

    one run of an llm eval is an anecdote. the first three runs of this suite
    scored 20/20, 18/20 and 17/20 on identical code and identical data, and
    quoting the best of those would have been a lie by selection. what is
    actually stable here is the safety result, and the only way to know that is
    to run it more than once and look at the spread.
    """

    runs: list[Scorecard] = field(default_factory=list)

    @property
    def pass_counts(self) -> list[int]:
        return [card.passed for card in self.runs]

    @property
    def total(self) -> int:
        return self.runs[0].total if self.runs else 0

    def case_pass_rate(self) -> dict[str, float]:
        """how often each case passed. anything below 1.0 is a flaky case."""
        seen: dict[str, list[bool]] = {}
        for card in self.runs:
            for result in card.results:
                seen.setdefault(result.case_id, []).append(result.passed)
        return {case_id: sum(v) / len(v) for case_id, v in seen.items()}

    @property
    def unsafe_actions(self) -> int:
        return sum(len(card.unsafe_actions) for card in self.runs)

    def as_dict(self) -> dict[str, Any]:
        counts = self.pass_counts
        rates = self.case_pass_rate()
        return {
            "runs": len(self.runs),
            "total_per_run": self.total,
            "passed_per_run": counts,
            "best": max(counts) if counts else 0,
            "worst": min(counts) if counts else 0,
            "mean": round(sum(counts) / len(counts), 2) if counts else 0.0,
            "safety_refused_every_run": self.unsafe_actions == 0,
            "unsafe_actions_total": self.unsafe_actions,
            "flaky_cases": {k: v for k, v in sorted(rates.items()) if v < 1.0},
            "always_passed": sorted(k for k, v in rates.items() if v == 1.0),
        }


def run_eval_repeated(
    make_copilot: Callable[[], Any],
    cases: tuple[EvalCase, ...] = CASES,
    runs: int = 3,
    workers: int = 3,
    on_run: Any = None,
) -> RepeatedScorecard:
    repeated = RepeatedScorecard()
    for index in range(runs):
        card = run_eval(make_copilot, cases, workers=workers)
        repeated.runs.append(card)
        if on_run:
            on_run(index + 1, card)
    return repeated
