"""Deterministic and replay policies used by frozen evaluation."""

from __future__ import annotations

from agent.datasets.research_sft import reference_actions


class PolicyError(RuntimeError):
    pass


class ReferencePolicy:
    """Replay the executed deterministic reference path for a case."""

    def __init__(self, case):
        self.case = case
        self.actions = reference_actions(case)
        self.index = 0

    def act(self, messages):
        if self.index >= len(self.actions):
            raise PolicyError("reference policy exhausted its actions before the episode terminated")
        action = self.actions[self.index]
        self.index += 1
        return action


class ReplayPolicy:
    """Replay a recorded list of assistant actions for a case."""

    def __init__(self, case, actions):
        if not isinstance(actions, list) or any(not isinstance(item, str) for item in actions):
            raise PolicyError("replayed actions must be a list of strings")
        self.case = case
        self.actions = list(actions)
        self.index = 0

    def act(self, messages):
        if self.index >= len(self.actions):
            raise PolicyError("replay policy exhausted its actions before the episode terminated")
        action = self.actions[self.index]
        self.index += 1
        return action
