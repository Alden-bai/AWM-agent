from collections import Counter
from pathlib import Path
from .io import canonical, file_hash, read_json, rows, verify_seal, write_json
from .stages import check_manifest


def tokenize_turn(tokenizer, messages, output):
    prompt = tokenizer.apply_chat_template(messages, tokenize=True, add_generation_prompt=True)
    full = tokenizer.apply_chat_template(messages + [{"role": "assistant", "content": output}],
                                         tokenize=True, add_generation_prompt=False)
    if full[:len(prompt)] != prompt or len(full) <= len(prompt):
        raise ValueError("chat_template_does_not_preserve_prompt_prefix")
    return {"input_ids": full, "labels": [-100] * len(prompt) + full[len(prompt):],
            "response_mask": [0] * len(prompt) + [1] * (len(full) - len(prompt))}


def export_sft(run, output, tokenizer_path=None, chat_template_path=None):
    run, output = Path(run), Path(output)
    manifest = check_manifest(run)
    if bool(tokenizer_path) != bool(chat_template_path):
        raise ValueError("token_export_requires_both_local_tokenizer_and_chat_template")
    tokenizer = None
    if tokenizer_path:
        from transformers import AutoTokenizer
        tokenizer = AutoTokenizer.from_pretrained(tokenizer_path, local_files_only=True, trust_remote_code=False)
        tokenizer.chat_template = Path(chat_template_path).read_text()
    selected, excluded = [], Counter()
    tasks = {t["task_instance_id"]: t for t in manifest["tasks"]}
    for candidate in manifest["candidates"]:
        folder = run / candidate["path"]
        try:
            verify_seal(folder)
            result = read_json(folder / "episode_result.json")
            task = tasks[candidate["task_instance_id"]]
            if manifest["config"]["purpose"] != "formal" or task["split"] != "train":
                raise ValueError("not_formal_training_split")
            if not result["reward_valid"] or result["reward"] != 1 or result["test_only"]:
                raise ValueError("not_valid_real_success")
            if result["termination_reason"] != "finish" or result["execution_integrity"] != "valid" or result.get("terminal_state_status") != "trusted":
                raise ValueError("not_complete_demonstration")
            outstanding, previous = set(), None
            for seq, event in enumerate(rows(folder / "events.jsonl"), 1):
                if event["seq"] != seq or event["parent_event_id"] != previous:
                    raise ValueError("event_chain_broken")
                previous = event["event_id"]
                if event["event_type"] == "tool_started":
                    outstanding.add(event["event_id"])
                elif event["event_type"] == "tool_completed":
                    outstanding.discard(event["call_event_id"])
            if outstanding:
                raise ValueError("unsettled_tool_write")
            evaluation_paths = sorted((folder / "evaluations" / result["evaluation_id"]).glob("attempt_*/result.json"))
            accepted = [read_json(p) for p in evaluation_paths if read_json(p).get("evaluation_valid")]
            if not accepted or not accepted[0]["reward_valid"] or accepted[0]["reward"] != 1 or accepted[0]["test_only"] or accepted[0].get("outcome_classification") != "complete":
                raise ValueError("evaluation_not_verified")
            turns = sorted((folder / "model_turns").iterdir())
            samples = []
            if len(turns) != result["turns"]:
                raise ValueError("incomplete_model_turns")
            for turn in turns:
                inp, out = read_json(turn / "input.json"), read_json(turn / "output.json")
                if out["test_only"] or out["finish_reason"] != "stop":
                    raise ValueError("test_or_truncated_turn")
                sample = {"task_instance_id": task["task_instance_id"], "rollout_id": candidate["rollout_id"],
                          "turn": int(turn.name), "prompt_messages": inp["messages"],
                          "response": out["content"], "loss_policy": "current_assistant_response_only",
                          "process_status": "not_evaluated", "source": str(folder.resolve())}
                if tokenizer:
                    sample.update(tokenize_turn(tokenizer, inp["messages"], out["content"]))
                samples.append(sample)
            selected.extend(samples)
        except (ValueError, KeyError, OSError) as exc:
            excluded[str(exc)] += 1
    output.mkdir(parents=True, exist_ok=False)
    (output / "sft.jsonl").write_text("".join(canonical(s) + "\n" for s in selected))
    report = {"planned_candidates": len(manifest["candidates"]), "exported_turns": len(selected),
              "accepted_episodes": len({s["rollout_id"] for s in selected}), "excluded": dict(excluded),
              "tokenizer": str(tokenizer_path) if tokenizer_path else None,
              "chat_template": str(chat_template_path) if chat_template_path else None,
              "chat_template_hash": file_hash(chat_template_path) if chat_template_path else None,
              "tokenizer_files": {str(p.relative_to(Path(tokenizer_path))): file_hash(p)
                                  for p in Path(tokenizer_path).rglob("*") if p.is_file()} if tokenizer_path else None,
              "process_status": "not_evaluated", "on_policy_grpo_eligible": False}
    write_json(output / "report.json", report)
    return report
