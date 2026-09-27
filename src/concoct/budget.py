"""Token and cost accounting with hard limits.

``MeteredProvider`` wraps any provider: it refuses to start a call once the
budget is exhausted and records usage (including usage attached to failed
calls) so the UI can report spend per repository and in total.
"""

from __future__ import annotations

import threading
from collections.abc import Callable

from concoct.models import UsageSummary
from concoct.providers.base import LLMProvider, LLMRequest, LLMResponse, ProviderError, TokenUsage
from concoct.providers.pricing import estimate_cost


class BudgetExceededError(RuntimeError):
    pass


class Budget:
    def __init__(self, max_cost_usd: float | None = None, max_tokens: int | None = None) -> None:
        self.max_cost_usd = max_cost_usd
        self.max_tokens = max_tokens
        self.total = UsageSummary()
        self.cost_known = True
        self._lock = threading.Lock()

    def check(self) -> None:
        with self._lock:
            if self.max_cost_usd is not None and self.total.cost_usd >= self.max_cost_usd:
                raise BudgetExceededError(
                    f"cost budget exhausted: ${self.total.cost_usd:.2f} of "
                    f"${self.max_cost_usd:.2f} spent"
                )
            if self.max_tokens is not None and self.total.total_tokens >= self.max_tokens:
                raise BudgetExceededError(
                    f"token budget exhausted: {self.total.total_tokens:,} of "
                    f"{self.max_tokens:,} tokens used"
                )

    def record(self, model: str, usage: TokenUsage) -> UsageSummary:
        cost = estimate_cost(model, usage)
        entry = UsageSummary(
            calls=1,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_read_tokens=usage.cache_read_tokens,
            cache_write_tokens=usage.cache_write_tokens,
            cost_usd=cost or 0.0,
        )
        with self._lock:
            if cost is None:
                self.cost_known = False
            self.total.add(entry)
        return entry


UsageListener = Callable[[UsageSummary], None]


class MeteredProvider:
    """Provider decorator enforcing a :class:`Budget`."""

    def __init__(self, inner: LLMProvider, budget: Budget) -> None:
        self.inner = inner
        self.budget = budget
        self.name = inner.name
        self.model = inner.model
        self._listeners: list[UsageListener] = []

    def add_listener(self, listener: UsageListener) -> None:
        self._listeners.append(listener)

    def remove_listener(self, listener: UsageListener) -> None:
        if listener in self._listeners:
            self._listeners.remove(listener)

    def _emit(self, usage: TokenUsage, model: str) -> None:
        entry = self.budget.record(model, usage)
        for listener in list(self._listeners):
            listener(entry)

    def complete(self, request: LLMRequest) -> LLMResponse:
        self.budget.check()
        try:
            response = self.inner.complete(request)
        except ProviderError as exc:
            if exc.usage is not None:
                self._emit(exc.usage, self.model)
            raise
        self._emit(response.usage, self.model)
        return response
