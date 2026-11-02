"""Offline single-task prompt packs. No model calls or changes to frozen runs."""
import argparse
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VIEW_KEYS = {
    'task_id', 'task', 'candidates', 'public_contracts', 'schemas',
    'tool_inventory', 'quality_policy', 'evidence_context',
}
EXTRA_KEYS = {
    'generate': set(),
    'judge': {'candidate_rubric'},
    'revise': {'previous_output', 'revision_feedback'},
}


def read(path):
    return json.loads(Path(path).read_text())


def build_pack(role, data, output_path):
    # An explicit allowlist prevents historical judge feedback leaking into review.
    if not isinstance(data, dict) or set(data) != VIEW_KEYS | EXTRA_KEYS[role]:
        raise ValueError('unexpected_or_missing_payload_keys')
    if role == 'judge' and data['candidate_rubric'].get('task_id') != data['task_id']:
        raise ValueError('rubric_task_id_mismatch')
    if role == 'revise' and data['previous_output'].get('task_id') != data['task_id']:
        raise ValueError('revision_task_id_mismatch')
    protocol = ROOT / 'configs/rubric' / (
        'judge_qwen_v13.txt' if role == 'judge' else 'select_compile_v13.txt')
    return dict(
        role=role,
        execution=(ROOT / 'configs/rubric/agent_execution_lean_v1.txt').read_text(),
        protocol=protocol.read_text(),
        data=data,
        output_path=str(Path(output_path).resolve()),
    )


def write_pack(role, source, destination):
    source, destination = Path(source).resolve(), Path(destination).resolve()
    pack_path = destination / 'input.json'
    launch_path = destination / 'launch.txt'
    output_path = destination / 'result.json'
    if any(p.exists() for p in (pack_path, launch_path, output_path)):
        raise FileExistsError(f'job_already_exists:{destination}')
    data = read(source)
    pack = build_pack(role, data, output_path)
    # Whitespace only: preserve every source string, reference ID and schema value.
    compact = json.dumps(pack, ensure_ascii=False, separators=(',', ':'))
    assert json.loads(compact)['data'] == data
    destination.mkdir(parents=True, exist_ok=True)
    pack_path.write_text(compact)
    launch_path.write_text(
        f'独立单题工作单元，模型gpt-5.6-luna，reasoning_effort=high。'
        f'读取输入包一次：{pack_path}。按包内execution和protocol完成，'
        '仅写output_path，结束后返回task_id和路径。调度要求fork_turns=none；'
        '审查不得继承生成或上轮审查上下文。')
    return dict(task_id=data['task_id'], role=role, input_path=str(pack_path),
                launch_path=str(launch_path), output_path=str(output_path),
                source_ref=str(source), source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
                pack_sha256=hashlib.sha256(pack_path.read_bytes()).hexdigest(),
                payload_chars_before=len(source.read_text()),
                payload_chars_after=len(json.dumps(data, ensure_ascii=False, separators=(',', ':'))))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--destination', required=True, type=Path)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--manifest', type=Path, help='Prepare initial generation jobs only')
    group.add_argument('--input', type=Path, help='One task payload for the specified role')
    parser.add_argument('--role', choices=EXTRA_KEYS, default='generate')
    args = parser.parse_args()
    if args.destination.exists():
        raise FileExistsError('destination_must_be_new')
    if args.manifest:
        if args.role != 'generate':
            parser.error('--manifest only supports initial generation')
        tasks = read(args.manifest)['tasks']
        names = [t['task_id'].replace(':', '__') for t in tasks]
        if len(set(names)) != len(names) or any(Path(n).name != n or n in ('.', '..') for n in names):
            raise ValueError('unsafe_or_duplicate_task_names')
        # Check all frozen sources before writing any job.
        for t in tasks:
            if hashlib.sha256(Path(t['view_ref']).read_bytes()).hexdigest() != t['view_sha256']:
                raise ValueError('frozen_view_hash_mismatch')
            if read(t['view_ref'])['task_id'] != t['task_id']:
                raise ValueError('source_task_id_mismatch')
            build_pack('generate', read(t['view_ref']), args.destination / 'unused.json')
        jobs = [write_pack('generate', t['view_ref'], args.destination / name)
                for t, name in zip(tasks, names)]
    else:
        jobs = [write_pack(args.role, args.input, args.destination / 'job')]
    metadata = dict(status='prepared_not_run', model='gpt-5.6-luna', reasoning_effort='high',
                    fork_turns='none', one_task_per_context=True, max_external_revisions=1,
                    semantic_protocols_unchanged=True, token_savings_measured=False,
                    context_target=128000, hard_context_limit_enforced=False, jobs=jobs)
    (args.destination / 'manifest.json').write_text(json.dumps(metadata, ensure_ascii=False, indent=2))
    print(json.dumps(dict(status=metadata['status'], jobs=len(jobs), destination=str(args.destination.resolve()))))


if __name__ == '__main__':
    main()
