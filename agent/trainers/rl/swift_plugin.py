"""ms-swift external plugin: current-policy sampling -> real tools -> private reward.

Only the GPU trainer imports this module. Tool observations are user messages and
the framework retains exact sampled assistant IDs across subsequent turns.
"""

from __future__ import annotations

import json
import os

from swift.callbacks import TrainerCallback, callbacks_map
from swift.rollout.multi_turn import MultiTurnScheduler, multi_turns

from agent.environment.cases import ResearchCase
from agent.environment.research import ResearchEpisode

_trainer = None


class RunContextCallback(TrainerCallback):
    def __init__(self, args, trainer):
        super().__init__(args, trainer)
        global _trainer
        _trainer = trainer


class FinancialResearchScheduler(MultiTurnScheduler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Qwen3.5 is multimodal: Trainer.processing_class may be a processor,
        # while exact response-prefix encoding needs the template's text tokenizer.
        if self._template is not None:
            self._tokenizer = self._template.tokenizer
        self.episodes = {}
        self.pending = {}

    async def on_trajectory_start(self, requests):
        if _trainer is None:
            raise RuntimeError("finflow_rl_context callback is required to bind the behavior policy step")
        for request in requests:
            row = request.data_dict["env_config"]
            case = ResearchCase.load(os.environ["FINFLOW_RL_DATASET"], row["case_path"])
            tokenizer = getattr(_trainer.processing_class, "tokenizer", _trainer.processing_class)
            episode = ResearchEpisode(
                case,
                logs=os.environ["FINFLOW_RL_EPISODES"],
                run_manifest=os.environ["FINFLOW_RL_RUN_MANIFEST"],
                policy={
                    "optimizer_step": _trainer.state.global_step,
                    "phase": "train" if _trainer.model.training else "validation",
                    "tokenizer": getattr(tokenizer, "name_or_path", None),
                    "model_revision": getattr(_trainer.model.config, "_commit_hash", None),
                },
            )
            request.messages = episode.initial_messages()
            self.episodes[request.uuid] = episode

    async def on_turn_end(self, infer_request, response_choice, current_turn):
        episode = self.episodes[infer_request.uuid]
        try:
            # Native engine outputs, never reconstructed from decoded text.
            sampled_ids = list(response_choice.token_ids or [])
            probabilities = (response_choice.logprobs or {}).get("content", [])
            logprobs = [item["logprob"] for item in probabilities]
            if not sampled_ids or len(sampled_ids) != len(logprobs):
                raise RuntimeError("rollout backend did not return aligned native token IDs and logprobs")
            ids, mask = self.get_response_token_data(infer_request, response_choice)
            episode.capture(
                {
                    "turn": current_turn,
                    "source": "ms-swift/vllm-native",
                    "sampled_token_ids": sampled_ids,
                    "rollout_logprobs": logprobs,
                    "response_token_ids": ids,
                    "response_loss_mask": mask,
                    "finish_reason": response_choice.finish_reason,
                }
            )
            text = infer_request.messages[-1]["content"]
            observation = episode.step(text, sampled_tokens=len(sampled_ids), finish_reason=response_choice.finish_reason)
            if self.max_turns and current_turn >= self.max_turns and not episode.done:
                episode.finish("step_limit")
            if episode.done:
                self.episodes.pop(infer_request.uuid)
                self.pending.pop(infer_request.uuid, None)
            else:
                self.pending[infer_request.uuid] = json.dumps(observation, ensure_ascii=False, separators=(",", ":"))
            # use_gym_env consumes total_reward; it is metadata and never enters messages.
            return {
                "done": episode.done,
                "rollout_infos": {
                    "total_reward": episode.reward if episode.done else 0.0,
                    "episode_id": episode.episode_id,
                    "case_id": episode.case.manifest["case_id"],
                    "terminal_status": episode.status,
                },
            }
        except BaseException:
            episode.finish("infrastructure_failure")
            self.episodes.pop(infer_request.uuid, None)
            self.pending.pop(infer_request.uuid, None)
            # Abort this generation group rather than turning host failures into policy failures.
            raise

    def step(self, infer_request, response_choice, current_turn):
        infer_request.messages.append({"role": "user", "content": self.pending.pop(infer_request.uuid)})
        return {"infer_request": infer_request}


multi_turns["finflow_research"] = FinancialResearchScheduler
callbacks_map["finflow_rl_context"] = RunContextCallback
