import json
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
from zero_llm_checks import resolve, safe_value, delta, business_check, curated_recipe
from zero_llm_guard import allowed_host, clean_env
from zero_llm_screen import classify
from zero_llm_worker import consistency


def test_no_private_ids_or_future_tool_results():
    with pytest.raises(ValueError):
        resolve({'$sql': 'select id from private'})
    with pytest.raises(ValueError):
        resolve({'$ref': 1}, results=[{'id': 3}])
    assert resolve({'$ref': 0, 'path': ['id']}, results=[{'id': 3}]) == 3
    assert not safe_value({'id': 3}, 'update object 30', 0, (), {}, '')
    assert safe_value({'id': 30}, 'update object 30', 0, (), {}, '')
    assert not safe_value({'$ref': 0, 'where': {'id': 999}}, 'object 30', 1, (), {}, '')


def test_contract_quote_must_contain_actual_value():
    sources = {(0, ('id',)): {'kind': 'public_contract', 'quote': 'ID 3'}}
    assert not safe_value({'id': 9}, 'task', 0, (), sources, 'ID 3')
    assert safe_value({'id': 3}, 'task', 0, (), sources, 'ID 3')


def test_network_guard_and_no_credentials(monkeypatch, tmp_path):
    monkeypatch.setenv('OPENAI_API_KEY', 'test-secret')
    assert 'OPENAI_API_KEY' not in clean_env()
    assert allowed_host('127.0.0.1') and allowed_host('::1')
    assert not allowed_host('api.example.com') and not allowed_host('0.0.0.0')
    script = "from zero_llm_guard import install; import socket; install(" + repr(str(tmp_path/'log')) + "); socket.create_connection(('198.51.100.1',443),timeout=.1)"
    result = subprocess.run([sys.executable, '-c', script], cwd=Path(__file__).resolve().parents[1]/'scripts', capture_output=True, text=True)
    assert result.returncode != 0 and 'external networking disabled' in result.stderr
    assert json.loads((tmp_path/'log').read_text())['blocked']


def test_database_diff_preserves_duplicate_rows():
    assert delta({'x': [{'id': 1}]}, {'x': [{'id': 1}, {'id': 1}]})['x']['added'] == [{'id': 1}]


def test_response_database_mismatch_detected():
    before = {'items': [{'id': 1, 'price': 40}]}
    after = {'items': [{'id': 1, 'price': 80}]}
    result = consistency({'table': 'items', 'method': 'patch'}, {'price': 80}, {'id': 1, 'price': 70}, before, after)
    assert result['status'] == 'diagnostic_mismatch'
    assert result['field_checks'][-1]['stored'] == 80


def test_probe_failure_not_task_rejection_and_bad_binding_excluded():
    row = {'decision': 'retain', 'verifier_binding': {'usable': True}}
    assert classify(row, {'exit_code': 'timeout'})[0] == 'not_covered'
    row['verifier_binding'] = {'usable': False, 'reason': 'conflicting_verifiers'}
    assert classify(row, None)[0] == 'excluded'


def test_placeholder_write_is_not_success():
    task = {'scenario': 'education_training_management_1', 'task_idx': 2}
    trace = {'failure': None, 'trajectory': [{'tool_calls': [{'name': 'call_tool'}], 'tool_response': {'content': '{}'}}]}
    before = {'competency_records': []}
    after = {'competency_records': [{'id': 1, 'apprentice_id': 0}]}
    assert business_check(task, before, after, trace)['status'] == 'mismatch'


def test_nested_response_model_mapping():
    from zero_llm_worker import endpoint_info
    code = '''
class Item:
    __tablename__ = "items"
class ItemModel:
    id: int
    price: float
class ListItemsResponse:
    items: List[ItemModel]
@app.get('/items')
def list_items() -> ListItemsResponse: pass
'''
    info = endpoint_info(code)['list_items']
    assert info['response_paths'] == [{'path': ['items', '*'], 'table': 'items', 'model': 'ItemModel'}]
    state = {'items': [{'id': 1, 'price': 80}]}
    checked = consistency(info, {}, {'items': [{'id': 1, 'price': 70}]}, state, state)
    assert checked['status'] == 'diagnostic_mismatch'
    assert len(checked['field_checks']) == 2


def test_official_sql_failure_is_not_a_negative_label(tmp_path):
    from awm.core.verify import run_verifier
    for name in ('initial', 'final'):
        with sqlite3.connect(tmp_path/(name+'.db')) as db:
            db.execute('create table items(id integer)')
    entry = {'verification': {'code': "def verify_task(initial_db_path, final_db_path):\n import sqlite3\n with sqlite3.connect(final_db_path) as db: return db.execute('SELECT absent FROM items').fetchall()"}}
    reward, result = run_verifier(entry, 'sql', str(tmp_path/'initial.db'), str(tmp_path/'final.db'))
    assert reward == 'judge_error'
    assert 'no such column' in result['error_message']
    row = {'decision': 'retain', 'verifier_binding': {'usable': True}}
    runtime = {'exit_code': 0, 'result': {'tool_schema_errors': [], 'sql_checks': [{'repeat': {'same_failure': True}}]}}
    assert classify(row, runtime) == ('excluded', ['reproduced_judge_program_error'])


def test_prompt_captured_without_model_response(monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from awm.core import verify
    class Capture(BaseException):
        pass
    captured = {}
    class Client:
        def __init__(self, **kwargs):
            self.chat = SimpleNamespace(completions=self)
        async def create(self, **kwargs):
            captured.update(kwargs)
            raise Capture()
    monkeypatch.setattr(verify, 'AsyncOpenAI', Client)
    trajectory = [{'role': 'assistant', 'content': 'actual answer', 'is_final': True}]
    with pytest.raises(Capture):
        asyncio.run(verify.run_llm_judge('task', {'result': 1, 'rows': [(2, 3)], 'counts': {4: 5}}, 'http://127.0.0.1:1', 'EMPTY', 'offline', trajectory))
    prompt = captured['messages'][1]['content']
    assert 'actual answer' in prompt and '"result": 1' in prompt
    wire = json.loads(prompt.split('\n\nverification_json:\n')[1])['code_execution_result']
    assert wire == {'result': 1, 'rows': [[2, 3]], 'counts': {'4': 5}}


def test_declared_datetime_fields_are_not_compared_as_raw_text():
    from zero_llm_worker import endpoint_info
    code = '''
class Cycle:
    __tablename__ = "cycles"
class CycleModel:
    id: int
    submission_deadline: datetime
class Result:
    cycles: List[CycleModel]
@app.get('/cycles')
def list_cycles() -> Result: pass
'''
    info = endpoint_info(code)['list_cycles']
    state = {'cycles': [{'id': 1, 'submission_deadline': '2025-01-31 23:59:00'}]}
    checked = consistency(info, {}, {'cycles': [{'id': 1, 'submission_deadline': '2025-01-31T23:59:00'}]}, state, state)
    assert checked['status'] == 'passed'
    assert [c['field'] for c in checked['field_checks']] == ['id']
