import sys, sqlite3, json
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts/two_layer'))
from core import resolve, query_check, make_cases


def test_public_result_reference_rejects_ambiguous_object():
    import pytest
    with pytest.raises(ValueError):resolve({'$ref':0,'path':['items'],'where':{'name':'A'},'take':'one'},results=[{'items':[{'name':'A','id':1},{'name':'A','id':2}]}])
    assert resolve({'$ref':0,'path':['items'],'where':{'name':'A'},'take':'one','field':'id'},results=[{'items':[{'name':'A','id':1}]}])==1
    with pytest.raises(ValueError):resolve({'$sql':'SELECT hidden_id'})


def test_negative_omission_cannot_fabricate_missing_reference():
    import pytest
    s={'positive':{'calls':[{'tool':'create','arguments':{}},{'tool':'link','arguments':{'id':{'$ref':0,'path':['id']}}}],'answer':'done'},'negatives':[{'id':'n','target':'r','kind':'omission','step':0}]}
    with pytest.raises(ValueError):make_cases(s)


def test_read_only_oracle_and_actual_answer(tmp_path):
    a=tmp_path/'a.db';b=tmp_path/'b.db'
    for p in (a,b):
        with sqlite3.connect(p) as db:db.execute('create table items(id integer, price integer)');db.execute('insert into items values (1,80)')
    trace={'final_answer':'{"price":81}','trajectory':[]}
    assert query_check("SELECT json_extract(value,'$.price')=(SELECT price FROM items WHERE id=1) FROM evidence.answers",a,b,trace)['passed'] is False
    assert query_check('DELETE FROM items',a,b,trace)['passed'] is False
    assert query_check('SELECT price FROM missing_table',a,b,trace)['valid'] is False
    assert query_check('SELECT 1 WHERE 0',a,b,trace)['passed'] is False
    assert query_check('SELECT count(*)=1 FROM items',a,b,trace)['passed'] is True
    with sqlite3.connect(b) as db:assert db.execute('select count(*) from items').fetchone()[0]==1


def test_mutation_changes_only_requested_leaf():
    s={'positive':{'calls':[{'tool':'update','arguments':{'price':80,'stock':20}}],'answer':'done'},'negatives':[{'id':'n','target':'price','kind':'mutation','step':0,'path':['price'],'value':81}]}
    cases=make_cases(s)
    assert cases[0]['calls'][0]['arguments']=={'price':80,'stock':20}
    assert cases[1]['calls'][0]['arguments']=={'price':81,'stock':20}
    assert s['positive']['calls'][0]['arguments']['price']==80


def test_budget_is_cumulative_and_prevents_duplicate_slot(tmp_path):
    import pytest
    from core import Budget, save_json
    save_json(tmp_path/'manifest.json',{'budget':{'max_physical_requests':2,'max_total_tokens':10000,'stage_requests':{'judge':2}}})
    budget=Budget(tmp_path)
    one=budget.reserve('case/slot0','judge',100,[{'role':'user','content':'test'}])
    budget.finish(one,{'total_tokens':50})
    with pytest.raises(RuntimeError,match='already reserved'):budget.reserve('case/slot0','judge',100,[])
    two=budget.reserve('case/slot1','judge',100,[])
    budget.finish(two,{'total_tokens':50})
    with pytest.raises(RuntimeError,match='budget_exhausted'):budget.reserve('case/slot2','judge',100,[])
