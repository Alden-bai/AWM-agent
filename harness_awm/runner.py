import asyncio
import copy
import time
from pathlib import Path
from .backend import BudgetExceeded
from .contracts import EpisodeResult
from .environment import AWMEnvAdapter
from .evaluation import apply_evaluation, evaluate_episode
from .io import Recorder, canonical, digest, seal, write_json
from .protocol import SYSTEM_PROMPT, Observations, ProtocolError, estimate_tokens, parse_action, validate_call


class EpisodeRunner:
    def __init__(self, config, budget, judge_semaphore, env_factory=AWMEnvAdapter):
        self.config, self.budget, self.judge_semaphore = config, budget, judge_semaphore
        self.env_factory = env_factory

    async def run(self, task, folder, rollout_id, backend):
        folder = Path(folder).resolve()
        folder.mkdir(parents=True, exist_ok=False)
        config = self.config
        recorder = Recorder(folder, rollout_id)
        env = self.env_factory(task, config, folder)
        observations = Observations(recorder, config.observation_chars, config.max_read_chars)
        write_json(folder / "task.json", task.model_dump())
        messages = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": task.instruction}]
        trace, known_tools, final_answer = [], {}, ""
        reason, origin, integrity, terminal = "environment_error", "runtime", "valid", "unavailable"
        turns, calls, cumulative_tokens = 0, 0, 0
        finished, error, quiet = False, None, False
        started = time.monotonic()
        recorder.emit("lifecycle", state="CREATED")
        try:
            recorder.emit("lifecycle", state="PREPARING")
            # The MCP session must stay in this coroutine: no wait_for/create_task around prepare.
            await env.prepare()
            recorder.emit("environment_ready", tools_hash=digest(env.tools), isolation=config.isolation,
                          network_boundary="shared_loopback", clock_policy=config.clock_policy, timezone=config.timezone)
            recorder.emit("lifecycle", state="RUNNING")
            for turn in range(1, config.max_turns + 1):
                remaining = config.episode_timeout - (time.monotonic() - started)
                if remaining <= 0:
                    reason = "wall_clock_limit"
                    break
                count = estimate_tokens(messages)
                if count + config.max_output_tokens + config.safety_margin > config.context_limit:
                    reason = "context_limit"
                    break
                if cumulative_tokens + count + config.max_output_tokens > config.episode_token_limit:
                    reason = "token_limit"
                    break
                turns = turn
                turn_dir = folder / "model_turns" / f"{turn:04d}"
                actual_messages = copy.deepcopy(messages)
                write_json(turn_dir / "input.json", {"messages": actual_messages, "input_token_estimate": count,
                           "estimator": "utf8_serialized_bytes_plus_128", "reserved_output": config.max_output_tokens})
                recorder.emit("model_request", "model_visible", turn, input_ref=recorder.blob(actual_messages, "model_visible"))
                async with asyncio.timeout(remaining):
                    response = await backend.generate(actual_messages, turn_dir, turn)
                write_json(turn_dir / "output.json", response)
                recorder.emit("model_response", "model_visible", turn,
                              raw_model_output_ref=recorder.blob(response, "model_visible"))
                raw = response["content"]
                cumulative_tokens += (response.get("usage") or {}).get("total_tokens", count + len(raw.encode()))
                messages.append({"role": "assistant", "content": raw})
                item = {"iteration": turn, "role": "assistant", "content": raw, "tool_calls": []}
                trace.append(item)
                try:
                    if response["finish_reason"] != "stop":
                        raise ProtocolError("model_output_incomplete_or_unsupported_finish_reason")
                    action = parse_action(raw)
                    recorder.emit("action_parsed", "model_visible", turn, parsed_action=action)
                    kind = action["type"]
                    if kind == "finish":
                        final_answer = action["answer"]
                        item.update(content=final_answer, is_final=True, raw_action=raw)
                        reason, origin, finished = "finish", "model", True
                        break
                    if kind == "list_tools":
                        known_tools = {t["name"]: t for t in env.tools}
                        returned = {"tools": env.tools}
                    elif kind == "read_observation":
                        returned = observations.read(action)
                    else:
                        validate_call(action, known_tools)
                        if calls >= config.max_tool_calls:
                            reason = "tool_call_limit"
                            break
                        calls += 1
                        tc_id = f"{rollout_id}_{turn}"
                        item["tool_calls"] = [{"id": tc_id, "name": "call_tool", "arguments": {
                            "tool_name": action["name"], "arguments": action["arguments"]}}]
                        before = env.audit(f"{turn:04d}_before")
                        call_id = recorder.emit("tool_started", turn_id=turn, tool_name=action["name"],
                            arguments_ref=recorder.blob(action["arguments"], "model_visible"),
                            before_state_ref=before, commit_status="outcome_unknown")
                        # All tools conservatively treated as potentially mutating.
                        try:
                            remaining = config.episode_timeout - (time.monotonic() - started)
                            async with asyncio.timeout(max(.001, remaining)):
                                returned = await env.call(action["name"], action["arguments"])
                        except BaseException as exc:
                            integrity = "outcome_unknown"
                            item["tool_response"] = {"tool_call_id": tc_id,
                                "content": canonical({"source": "harness", "transport_error": type(exc).__name__,
                                                       "commit_status": "outcome_unknown"})}
                            recorder.emit("tool_outcome_unknown", turn_id=turn, call_event_id=call_id)
                            raise
                        after = env.audit(f"{turn:04d}_after")
                        recorder.emit("tool_completed", "runtime_returned", turn, call_event_id=call_id,
                            raw_result_ref=recorder.blob(returned, "runtime_returned"),
                            transport_status="response", mcp_is_error=returned.get("isError", False),
                            commit_status="response_received", before_state_ref=before, after_state_ref=after)
                    visible, raw_ref = observations.present(returned)
                    rendered = canonical({"source": "harness" if kind == "read_observation" else "awm", **visible})
                    # Actor JSON protocol uses user observations; official trajectory uses native call wrappers.
                    tc_id = f"{rollout_id}_{turn}"
                    tc = {"id": tc_id, "name": kind, "arguments":
                          ({"tool_name": action["name"], "arguments": action["arguments"]} if kind == "call_tool" else
                           {k: v for k, v in action.items() if k != "type"})}
                    item.update(tool_calls=[tc], tool_response={"tool_call_id": tc_id, "content": rendered})
                    recorder.emit("observation_delivered", "model_visible", turn, raw_result_ref=raw_ref,
                                  visible_result_ref=recorder.blob(visible, "model_visible"), truncated=visible["truncated"])
                except ProtocolError as exc:
                    rendered = canonical({"source": "harness", "protocol_error": str(exc)})
                    item["protocol_error"] = str(exc)
                    recorder.emit("protocol_error", "model_visible", turn, error=str(exc))
                messages.append({"role": "user", "content": rendered})
            else:
                reason = "max_turns"
        except BudgetExceeded as exc:
            reason, error = "global_budget_limit", str(exc)
        except asyncio.CancelledError:
            reason, origin, error = "cancelled", "user", "cancelled"
        except TimeoutError:
            reason = "tool_timeout" if integrity == "outcome_unknown" else "wall_clock_limit"
            error = reason
        except Exception as exc:
            reason, error = "runtime_error", f"{type(exc).__name__}: {exc}"
            if integrity == "valid":
                integrity = "runtime_error"
        finally:
            recorder.emit("lifecycle", state="STOPPING", termination_reason=reason)
            try:
                quiet = await env.close()
            except Exception as exc:
                quiet, error = False, f"quiescence_failed: {exc}"
            recorder.emit("environment_stopped", quiescent=quiet)
            if quiet and env.started:
                try:
                    recorder.emit("lifecycle", state="SNAPSHOTTING")
                    final_hash = env.freeze()
                    terminal = "trusted"
                    recorder.emit("snapshot_created", final_hash=final_hash)
                except Exception as exc:
                    terminal, error = "untrusted", f"snapshot_failed: {exc}"
            elif not quiet:
                terminal, integrity = "untrusted", "outcome_unknown"
        trajectory = {"scenario": task.scenario, "task_id": task.native_task_id, "task": task.instruction,
                      "trajectory": trace, "messages": messages, "final_answer": final_answer,
                      "finished": finished, "protocol_id": config.protocol_id}
        write_json(folder / "trajectory.json", trajectory)
        result = EpisodeResult(rollout_id=rollout_id, task_instance_id=task.task_instance_id,
            termination_reason=reason, termination_origin=origin, execution_integrity=integrity,
            terminal_state_status=terminal, turns=turns, tool_calls=calls,
            test_only=config.actor == "scripted" or config.judge == "mock", error=error).model_dump()
        recorder.emit("execution_closed", termination_reason=reason, execution_integrity=integrity,
                      elapsed_seconds=time.monotonic() - started, token_count_or_estimate=cumulative_tokens)
        write_json(folder / "episode_result.json", result)
        seal(folder)
        if terminal == "trusted":
            evaluation = await evaluate_episode(folder, task, config, self.budget, self.judge_semaphore)
            result = apply_evaluation(folder, evaluation)
        return result
