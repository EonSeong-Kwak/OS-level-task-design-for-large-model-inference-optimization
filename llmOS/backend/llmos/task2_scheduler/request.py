from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ReqState(Enum):
    WAITING = "waiting"
    RUNNING = "running"
    PREEMPTED = "preempted"
    FINISHED = "finished"


@dataclass
class Request:
    rid: int
    arrival_time: float
    prompt_len: int
    max_tokens: int
    priority: int = 0
    tenant: str = "default"
    state: ReqState = ReqState.WAITING
    generated: int = 0
    start_time: float = -1.0
    first_token_time: float = -1.0
    finish_time: float = -1.0
    seq_id: int = -1
    wait_accum: float = 0.0
    last_wait_start: float = -1.0
    remaining_slice: float = 0.0

    @property
    def is_done(self) -> bool:
        return self.generated >= self.max_tokens

    @property
    def remaining(self) -> int:
        return max(0, self.max_tokens - self.generated)

    def step(self, now: float) -> None:
        if self.start_time < 0:
            self.start_time = now
        if self.generated == 0:
            self.first_token_time = now
        self.generated += 1
        self.state = ReqState.RUNNING
        if self.is_done:
            self.finish_time = now
            self.state = ReqState.FINISHED
