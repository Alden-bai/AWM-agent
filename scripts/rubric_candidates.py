"""Deterministic candidate spans, source-bound five-field compilation and scoring.

No model call is made by this module. Lexical candidates are NOT a semantic
oracle: the complete instruction remains available to generator and judge.
"""
import copy
import hashlib
import json
import re

from rubric_pilot import validate_output

DIMENSIONS = ('source_support', 'coverage', 'requirement_classification', 'evidence_design', 'decision_logic')
DECISIONS = {'satisfied_when':'本条G成立且本条P成立',
             'violated_when':'本条G成立且本条P不成立', 'unknown_when':'本条G不成立'}
SIGNALS = re.compile(r"\b(?:all|only|most recent|highest|top-rated|next|within|before|after|if|unless|then|immediately|same|exact|new|preferably|optional|if possible)\b|\b\d[\d,]*(?:\.\d+)?\b", re.I)


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def extract_candidates(task):
    """Partition all non-whitespace text, respecting quotations and brackets.

    Offsets are Python Unicode code-point offsets, not bytes. Repeated quotes
    remain distinct occurrences. Punctuation stays attached to its clause.
    """
    if not isinstance(task,str) or not task.strip():
        raise ValueError('empty_task')
    boundaries={0,len(task)}
    quote=None; stack=[]; i=0
    while i<len(task):
        c=task[i]
        if quote:
            if c=='\\': i+=2; continue
            if c==quote: quote=None
            i+=1; continue
        if c in ('"', "'") and not (c=="'" and i and task[i-1].isalnum()):
            quote=c
        elif c in '([{': stack.append(c)
        elif c in ')]}':
            if stack: stack.pop()
        elif not stack:
            if c in ',;\n' or (c in '.!?' and (i+1==len(task) or task[i+1].isspace())):
                # Thousands separators in numeric values are not clause breaks.
                if not (c==',' and i and i+1<len(task) and task[i-1].isdigit() and task[i+1].isdigit()):
                    boundaries.add(i+1)
            if i and task[i-1].isspace() and re.match(r'(?:and|then|if|unless|but)\b',task[i:],re.I):
                boundaries.add(i)
        i+=1
    points=sorted(boundaries); candidates=[]
    for a,b in zip(points,points[1:]):
        while a<b and task[a].isspace(): a+=1
        while b>a and task[b-1].isspace(): b-=1
        if a==b: continue
        text=task[a:b]
        # Isolated conjunctions/punctuation belong to the next/previous clause.
        if candidates and not re.search(r'\w',text):
            candidates[-1]['end']=b
            candidates[-1]['quote']=task[candidates[-1]['start']:b]
            continue
        candidates.append(dict(candidate_id=f'C{len(candidates)+1:02d}', source_id='task',
            start=a,end=b,quote=text,signals=[dict(text=m.group(),start=a+m.start(),end=a+m.end()) for m in SIGNALS.finditer(text)]))
    covered=set()
    for c in candidates:
        assert task[c['start']:c['end']]==c['quote']
        covered.update(range(c['start'],c['end']))
    if any(not ch.isspace() and n not in covered for n,ch in enumerate(task)):
        raise ValueError('uncovered_task_characters')
    return dict(version='candidate-spans-v1',task_sha256=sha(task),offset_unit='unicode_codepoint',
                complete_nonwhitespace_coverage=True,candidates=candidates)


def prepare_input(inp):
    """Compact contract view with inline reference IDs; examples stay offline.

    All non-example keys/values and all DDL are preserved. Exact original
    serialized quote spans are kept in a local registry and filled by code.
    """
    pack=extract_candidates(inp['task'])
    registry={c['candidate_id']:dict(source_id='task',quote=c['quote'],start=c['start'],end=c['end']) for c in pack['candidates']}
    view=dict(task_id=inp['task_id'],task=inp['task'],candidates=pack['candidates'],public_contracts=[],schemas=[],
              tool_inventory=inp.get('tool_inventory',[]),quality_policy=inp.get('quality_policy'),
              evidence_context=dict(types=inp.get('available_evidence_types',[]),note=inp.get('evidence_note','')))
    examples=[]
    source_ids=set()
    for s in inp['sources']:
        if s['id'] in source_ids: raise ValueError('duplicate_source_id')
        source_ids.add(s['id'])
        if s['kind']=='public_contract':
            original=json.loads(s['text'])
            def walk(node,pointer=''):
                if isinstance(node,dict):
                    result={}
                    for k,v in node.items():
                        loc=pointer+'/'+k.replace('~','~0').replace('/','~1')
                        if k in ('example','examples'):
                            examples.append(dict(source_id=s['id'],pointer=loc,value=v))
                        elif k in ('summary','description') and isinstance(v,str) and v:
                            ref=f'K{sum(x.startswith("K") for x in registry)+1:04d}'
                            q=json.dumps(v,ensure_ascii=False)[1:-1]
                            if q not in s['text']: raise ValueError('nonoriginal_reference')
                            start=s['text'].index(q)
                            registry[ref]=dict(source_id=s['id'],quote=q,start=start,end=start+len(q),pointer=loc)
                            result[k]=dict(text=v,ref_id=ref)
                        else: result[k]=walk(v,loc)
                    return result
                if isinstance(node,list): return [walk(v,pointer+'/'+str(i)) for i,v in enumerate(node)]
                return node
            contract=walk(original)
            def restore(node):
                if isinstance(node,dict):
                    if set(node)=={'text','ref_id'} and node['ref_id'] in registry:
                        return node['text']
                    return {k:restore(v) for k,v in node.items()}
                if isinstance(node,list): return [restore(v) for v in node]
                return node
            restored=restore(contract)
            for e in examples:
                if e['source_id']!=s['id']: continue
                keys=[x.replace('~1','/').replace('~0','~') for x in e['pointer'].split('/')[1:]]
                target=restored
                for k in keys[:-1]: target=target[int(k)] if isinstance(target,list) else target[k]
                target[keys[-1]]=e['value']
            if restored!=original: raise ValueError('contract_roundtrip_failed')
            view['public_contracts'].append(dict(source_id=s['id'],contract=contract))
        elif s['kind']=='schema': view['schemas'].append(dict(source_id=s['id'],ddl=s['text']))
        elif s['kind']=='task':
            if s['id']!='task' or s['text']!=inp['task']: raise ValueError('wrong_task_source')
        else: raise ValueError('unsupported_source_kind')
    if 'task' not in source_ids: raise ValueError('missing_task_source')
    return view,dict(**pack,reference_registry=registry,excluded_examples=examples,
                     source_hashes={s['id']:sha(s['text']) for s in inp['sources']},contracts_roundtrip_verified=True)


def nonempty(value): return isinstance(value,str) and bool(value.strip())


def id_list(value, allowed, empty=False):
    return isinstance(value,list) and (bool(value) or empty) and all(isinstance(x,str) and x in allowed for x in value) and len(value)==len(set(value))


def validate_selection(out, inp, pack):
    if not isinstance(out,dict) or set(out)!={'task_id','selection','items','unresolved'}:
        return ['invalid_selection_root']
    errors=[]
    if out['task_id']!=inp['task_id']: errors.append('wrong_task_id')
    if not isinstance(out['items'],list) or not out['items']: return errors+['no_items']
    candidates={c['candidate_id'] for c in pack['candidates']}
    contracts={k for k in pack['reference_registry'] if k.startswith('K')}
    source_ids={s['id'] for s in inp['sources']}
    items={}
    fields={'id','candidate_ids','contract_ref_ids','requirement_level','requirement','gate','predicate','evidence','method'}
    for item in out['items']:
        if not isinstance(item,dict) or set(item)!=fields or not nonempty(item.get('id')):
            errors.append('invalid_item_shape'); continue
        rid=item['id']
        if rid in items: errors.append('duplicate_item_id:'+rid)
        items[rid]=item
        if any(not nonempty(item[k]) for k in ('requirement','gate','predicate')): errors.append('empty_business_content:'+rid)
        if not id_list(item['candidate_ids'],candidates): errors.append('invalid_candidate_refs:'+rid)
        if not id_list(item['contract_ref_ids'],contracts,True): errors.append('invalid_contract_refs:'+rid)
        if item['requirement_level'] not in ('required','quality'): errors.append('invalid_level:'+rid)
        if item['method'] not in ('deterministic','semantic','hybrid'): errors.append('invalid_method:'+rid)
        ev=item['evidence']
        if not isinstance(ev,list) or not ev:
            errors.append('missing_evidence:'+rid)
        else:
            for e in ev:
                if not isinstance(e,dict) or set(e)!={'description','source_id'} or not nonempty(e.get('description')) or (e.get('source_id') is not None and (not isinstance(e['source_id'],str) or e['source_id'] not in source_ids)):
                    errors.append('invalid_evidence:'+rid)
    if not isinstance(out['selection'],list): return errors+['invalid_selection']
    selections={}
    for row in out['selection']:
        if not isinstance(row,dict) or set(row)!={'candidate_id','action','item_ids','reason'} or not nonempty(row.get('candidate_id')):
            errors.append('invalid_selection_row'); continue
        cid=row['candidate_id']
        if cid not in candidates or cid in selections: errors.append('duplicate_or_unknown_candidate:'+cid)
        selections[cid]=row
        if row['action'] not in ('keep','merge','background','ambiguous') or not nonempty(row['reason']): errors.append('invalid_disposition:'+cid)
        if not id_list(row['item_ids'],items,row['action']=='background'): errors.append('invalid_mapping:'+cid)
        if row['action']=='background' and row['item_ids']: errors.append('background_has_items:'+cid)
        if id_list(row['item_ids'],items,True):
            reverse={rid for rid,item in items.items() if isinstance(item['candidate_ids'],list) and cid in item['candidate_ids']}
            if set(row['item_ids'])!=reverse: errors.append('mapping_mismatch:'+cid)
    if set(selections)!=candidates: errors.append('missing_candidate_dispositions')
    affected=set()
    if not isinstance(out['unresolved'],list): errors.append('invalid_unresolved')
    else:
        for u in out['unresolved']:
            if not isinstance(u,dict) or set(u)!={'issue','affected_items','needed'} or not nonempty(u.get('issue')) or not nonempty(u.get('needed')) or not id_list(u.get('affected_items'),items):
                errors.append('invalid_unresolved_entry')
            else: affected.update(u['affected_items'])
    for row in selections.values():
        if row['action']=='ambiguous' and isinstance(row['item_ids'],list) and all(isinstance(i,str) for i in row['item_ids']) and not set(row['item_ids']).issubset(affected):
            errors.append('unrecorded_ambiguity:'+row['candidate_id'])
    return errors


def compile_rubric(out, inp, pack):
    errors=validate_selection(out,inp,pack)
    if errors: raise ValueError('invalid_selection:'+','.join(errors))
    registry=pack['reference_registry']; original={s['id']:s['text'] for s in inp['sources']}
    # Reverify offsets at compilation; tampered mappings must never be trusted.
    if sha(inp['task'])!=pack['task_sha256']: raise ValueError('task_hash_mismatch')
    for sid,h in pack['source_hashes'].items():
        if sha(original[sid])!=h: raise ValueError('source_hash_mismatch')
    for ref in registry.values():
        if original[ref['source_id']][ref['start']:ref['end']]!=ref['quote']: raise ValueError('reference_span_mismatch')
    rubric=[]
    for row in out['items']:
        refs=[]; seen=set()
        for key in row['candidate_ids']+row['contract_ref_ids']:
            ref=registry[key]; identity=(ref['source_id'],ref['quote'])
            if identity not in seen:
                refs.append(dict(source_id=ref['source_id'],quote=ref['quote']));seen.add(identity)
        rubric.append(dict(id=row['id'],criterion=f"要求：{row['requirement']}\nG：{row['gate']}\nP：{row['predicate']}",
            sources=refs,requirement_level=row['requirement_level'],evidence=copy.deepcopy(row['evidence']),
            decision=dict(method=row['method'],**DECISIONS)))
    coverage=[dict(quote=registry[s['candidate_id']]['quote'],item_ids=s['item_ids'],
                   interpretation='['+s['candidate_id']+'] '+s['action']+'：'+s['reason']) for s in out['selection'] if s['action']!='background']
    result=dict(task_id=inp['task_id'],rubric=rubric,coverage=coverage,unresolved=copy.deepcopy(out['unresolved']))
    errors=validate_output(result,inp)
    if errors: raise ValueError('invalid_compiled_rubric:'+','.join(errors))
    return result


def evaluate_scores(out,task_id,item_ids,candidate_ids):
    """No total score compensation. bool, absent, duplicate, invalid => review.

    A zero rejects only this rubric version. Original tasks are never deleted.
    All nonzero with a 1 remains review; all 2 is an eligible candidate.
    """
    errors=[]
    if not isinstance(out,dict) or set(out)!={'task_id','dimensions'} or not isinstance(out.get('dimensions'),list):
        return dict(status='review',errors=['invalid_judge_root'],scores={},zero_dimensions=[],task_retained=True)
    if out['task_id']!=task_id: errors.append('wrong_task_id')
    scores={}
    for d in out['dimensions']:
        if not isinstance(d,dict) or set(d)!={'name','score','reason','findings'} or not isinstance(d.get('name'),str):
            errors.append('invalid_dimension');continue
        name=d['name']
        if name not in DIMENSIONS or name in scores: errors.append('duplicate_or_unknown_dimension:'+name)
        if type(d['score']) is not int or d['score'] not in (0,1,2): errors.append('invalid_score:'+name)
        else: scores[name]=d['score']
        if not nonempty(d['reason']): errors.append('empty_score_reason:'+name)
        if not isinstance(d['findings'],list) or not d['findings']:
            errors.append('missing_score_evidence:'+name);continue
        for f in d['findings']:
            if not isinstance(f,dict) or set(f)!={'item_ids','candidate_ids','problem','counterexample'}:
                errors.append('invalid_finding:'+name);continue
            if not id_list(f['item_ids'],item_ids,True) or not id_list(f['candidate_ids'],candidate_ids,True) or (not f['item_ids'] and not f['candidate_ids']) or not nonempty(f['problem']) or not nonempty(f['counterexample']):
                errors.append('invalid_finding_evidence:'+name)
    if set(scores)!=set(DIMENSIONS): errors.append('incomplete_dimensions')
    zeros=[name for name in DIMENSIONS if scores.get(name)==0]
    status='review' if errors else ('rejected' if zeros else ('eligible' if all(v==2 for v in scores.values()) else 'review'))
    return dict(status=status,errors=errors,scores=scores,zero_dimensions=zeros,task_retained=True)
