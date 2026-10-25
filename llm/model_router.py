"""Roteamento de modelos por papel.

Não disparamos vários modelos "só para ver". O papel define o modelo; o
gateway faz o failover entre provedores. A escolha é auditável e configurável.
"""

from __future__ import annotations

from dataclasses import dataclass

from aurora.config.models import ModelProfile, default_profiles


@dataclass
class ModelRouter:
    profiles: dict[str, ModelProfile]

    @classmethod
    def from_settings(cls, llm) -> "ModelRouter":
        profiles = default_profiles(
            model=llm.model,
            utility=llm.utility_model,
            planner=llm.planner_model,
            coder=llm.coder_model,
            context=llm.context_window,
            vision=getattr(llm, "vision_model", "auto"),
        )
        return cls(profiles)

    def for_role(self, role: str) -> ModelProfile:
        profile = self.profiles.get(role)
        if profile is None:
            profile = self.profiles["chat"]
        return profile

    def model_for(self, role: str) -> str:
        return self.for_role(role).model

    def context_for(self, role: str = "chat") -> int:
        return self.for_role(role).context_window

    def describe(self) -> str:
        return ", ".join(f"{role}={profile.model}" for role, profile in self.profiles.items())
