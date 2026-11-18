"""换码归属与复权链接续测试。

场景：
- R0001 已确认换码 600100 -> 600559（带归属日期，含两个版本做版本裁决）；
- R0002 待确认换码 600333 -> 600777（归属晚到，复权链在此断开）；
- 旧持仓不得落到复用代码的新发行人名下，因子不得跨证券连乘；
- 同一份换码记录重跑不新增批次，补齐归属后可查到接续依据。
"""
from pathlib import Path

import pytest

from app.config import SAMPLE
from app.pipeline import calculate
from app.lineage import build, detect_ambiguous
from datetime import date

DEMO = Path(__file__).resolve().parent.parent / 'sample_data' / 'recode-demo.csv'


@pytest.fixture
def demo():
    return calculate(DEMO.read_text(encoding='utf-8'))


def test_recode_kind_accepted(demo):
    assert demo['input_count'] == 12
    assert {r['kind'] for r in demo['rows']} == {'action', 'position', 'recode'}


def test_recode_version_resolution_keeps_latest(demo):
    # R0001 有 v1/v2，采信 v2；v1 进 recode_history，不产生第二条换码边。
    edges = demo['stages']['2']['lineage_rows']
    r1 = [e for e in edges if e['换码记录'] == 'R0001']
    assert len(r1) == 1 and r1[0]['版本'] == 2 and r1[0]['状态'] == '已确认'
    assert len(demo['recode_history']) == 1
    assert demo['stages']['2']['metrics'][3] == ['换码已接续', 1, '项']


def test_pending_recode_is_quarantined_not_linked(demo):
    pending = demo['stages']['4']['pending_links']
    assert len(pending) == 1 and pending[0]['换码记录'] == 'R0002'
    assert any(q['原因码'] == 'RECODE_PENDING' and q['record'] == 'R0002'
               for q in demo['quarantine'])
    # 待确认边不出现在已接续链接中。
    assert all(link['换码记录'] != 'R0002' for link in demo['stages']['4']['links'])


def test_confirmed_recode_merges_issuer_identity(demo):
    issuers = {r['记录']: r['发行人身份'] for r in demo['stages']['2']['rows']}
    # 换码前后两段属于同一发行人，身份沿用根代码。
    assert issuers['P1001'] == issuers['P1002'] == '600100'


def test_pending_recode_keeps_separate_identities(demo):
    issuers = {r['记录']: r['发行人身份'] for r in demo['stages']['2']['rows']
               if r['类别'] == 'position'}
    assert issuers['P1004'] == '600333'
    assert issuers['P1005'] == '600777@2026-11-16#待确认'


def test_factor_chain_continuous_across_confirmed_recode(demo):
    chain = next(c for c in demo['stages']['4']['chains']
                 if c['发行人身份'] == '600100')
    codes = [r['证券'] for r in chain['rows']]
    assert codes == ['600100', '600559']  # 跨代码同链
    # 累计因子为两次单次因子连乘（Decimal 精确）。
    from decimal import Decimal
    single = [Decimal(r['单次因子']) for r in chain['rows']]
    assert Decimal(chain['rows'][-1]['累计复权因子']) == (single[0] * single[1]).quantize(
        Decimal('0.00000001'))


def test_factor_chain_breaks_at_pending_recode(demo):
    # 待确认两侧是两条独立链，绝不连乘。
    issuers = {c['发行人身份'] for c in demo['stages']['4']['chains']}
    assert '600333' in issuers
    assert any(i.startswith('600777@') and i.endswith('#待确认') for i in issuers)
    assert demo['stages']['4']['metrics'][3] == ['复权链断点', 1, '处']


def test_old_holdings_never_attached_to_new_issuer(demo):
    # P1004 是旧代码 600333 在换码生效前的持仓；R0002 待确认时它只能匹配旧行动 A1004，
    # 绝不能匹配新代码 600777 的 A1005。
    entitled = {(r['持仓记录'], r['行动']) for r in demo['stages']['3']['rows']
                if r['资格'] == '有登记权益'}
    assert ('P1004', 'A1004') in entitled
    assert all(action != 'A1005' for pos, action in entitled if pos == 'P1004')
    assert ('P1005', 'A1005') in entitled  # 新发行人自己的持仓正常登记


def test_post_effective_position_under_old_code_has_no_rights(demo):
    # P1006 是换码生效日后仍用旧代码 600333 的快照，归属待确认，无登记权益。
    no_rights = {r['持仓记录'] for r in demo['stages']['3']['rows']
                 if r['资格'] == '无登记权益'}
    assert 'P1006' in no_rights


def test_reconciliation_conservation_includes_recodes(demo):
    assert demo['balanced'] is True
    accepted = (len(demo['actions']) + len(demo['action_history'])
                + len(demo['positions']) + len(demo['recodes'])
                + len(demo['recode_history']))
    assert demo['input_count'] == accepted + demo['duplicates'] + len(demo['quarantine'])


def test_rerun_same_recode_uses_same_batch(client):
    text = DEMO.read_text(encoding='utf-8')
    first = client.post('/api/run', json={'csv': text}).get_json()
    again = client.post('/api/run', json={'csv': text}).get_json()
    assert first['manifest']['run_id'] == again['manifest']['run_id']
    assert again['reused'] is True
    assert len(client.get('/api/history').get_json()) == 1
    # 重跑不产生新批次，且复用批次的换码链与隔离内容保持不变。
    stored = client.get('/api/runs/' + first['manifest']['run_id']).get_json()
    assert stored['stages']['2']['lineage_rows'] == first['stages']['2']['lineage_rows']
    assert len(stored['quarantine']) == len(first['quarantine'])


def test_completing_attribution_creates_traceable_link(client):
    text = DEMO.read_text(encoding='utf-8')
    before = client.post('/api/run', json={'csv': text}).get_json()
    # 补齐 R0002 的归属日期后重跑：新批次，断点变成已接续，可查到接续依据。
    completed = text.replace(
        'R0002,recode,600333,600777,2026-11-16,,,,,,,,,1,',
        'R0002,recode,600333,600777,2026-11-16,2026-11-15,,,,,,,,1,')
    after = client.post('/api/run', json={'csv': completed}).get_json()
    assert after['manifest']['run_id'] != before['manifest']['run_id']
    links = after['stages']['4']['links']
    r2 = next(link for link in links if link['换码记录'] == 'R0002')
    assert r2['接续结论'] == '已接续' and r2['归属日期'] == '2026-11-15'
    assert after['stages']['4']['metrics'][3] == ['复权链断点', 0, '处']
    # 补齐后 600333 与 600777 并入同一发行人链。
    chain_issuers = {c['发行人身份'] for c in after['stages']['4']['chains']}
    assert not any(i.endswith('#待确认') for i in chain_issuers)


def test_ambiguous_recode_structure_is_isolated():
    edges = [
        {'record': 'X1', 'old_code': 'A', 'new_code': 'B', 'effective': date(2026, 11, 16),
         'version': 1, '行号': 1, 'record_date': '2026-11-15'},
        {'record': 'X2', 'old_code': 'A', 'new_code': 'C', 'effective': date(2026, 11, 16),
         'version': 1, '行号': 2, 'record_date': '2026-11-15'},
    ]
    assert detect_ambiguous(edges) == {'X1', 'X2'}


def test_cycle_is_ambiguous():
    edges = [
        {'record': 'X1', 'old_code': 'A', 'new_code': 'B', 'effective': date(2026, 11, 16),
         'version': 1, '行号': 1, 'record_date': '2026-11-15'},
        {'record': 'X2', 'old_code': 'B', 'new_code': 'A', 'effective': date(2026, 11, 17),
         'version': 1, '行号': 2, 'record_date': '2026-11-15'},
    ]
    assert detect_ambiguous(edges) == {'X1', 'X2'}


def test_half_open_effective_boundary():
    edge = {'record': 'X1', 'old_code': 'A', 'new_code': 'B',
            'effective': date(2026, 11, 16), 'version': 1, '行号': 1,
            'record_date': '2026-11-15'}
    lineage = build([edge], [], extra_codes=['A', 'B'])
    # 生效日前旧代码仍是原证券；生效日当天起新代码并入同一发行人。
    assert lineage.resolve('A', date(2026, 11, 15)) == ('A', True)
    assert lineage.resolve('B', date(2026, 11, 16)) == ('A', True)


def test_multi_hop_recode_traces_to_origin_identity():
    # A→B→C 两跳确认换码：三个代码在各自生效区间都解析到最初代码 A。
    e1 = {'record': 'E1', 'old_code': 'A', 'new_code': 'B',
          'effective': date(2026, 11, 16), 'version': 1, '行号': 1,
          'record_date': '2026-11-15'}
    e2 = {'record': 'E2', 'old_code': 'B', 'new_code': 'C',
          'effective': date(2026, 11, 20), 'version': 1, '行号': 2,
          'record_date': '2026-11-19'}
    lineage = build([e1, e2], [], extra_codes=['A', 'B', 'C'])
    assert lineage.resolve('A', date(2026, 11, 15)) == ('A', True)
    assert lineage.resolve('B', date(2026, 11, 17)) == ('A', True)
    assert lineage.resolve('C', date(2026, 11, 20)) == ('A', True)


def test_pending_boundary_splits_but_does_not_merge():
    edge = {'record': 'X1', 'old_code': 'A', 'new_code': 'B',
            'effective': date(2026, 11, 16), 'version': 1, '行号': 1,
            'record_date': ''}
    lineage = build([], [edge], extra_codes=['A', 'B'])
    assert lineage.resolve('A', date(2026, 11, 15)) == ('A', True)
    issuer, confirmed = lineage.resolve('B', date(2026, 11, 16))
    assert issuer == 'B@2026-11-16#待确认' and confirmed is False
    # 旧侧切分后片段独立留待复用，不与新侧合并。
    reuse_issuer, reuse_confirmed = lineage.resolve('A', date(2026, 11, 17))
    assert reuse_issuer == 'A@2026-11-16' and reuse_confirmed is True


def test_conflicting_recode_same_version_isolated(client):
    text = DEMO.read_text(encoding='utf-8')
    # 同版本同记录但新旧代码不一致 -> RECODE_CONFLICT。
    text += 'R0003,recode,600100,600999,2026-11-16,2026-11-15,,,,,,,,1,\n'
    text += 'R0003,recode,600100,600888,2026-11-16,2026-11-15,,,,,,,,1,\n'
    run = client.post('/api/run', json={'csv': text}).get_json()
    assert any(q['原因码'] == 'RECODE_CONFLICT' and q['record'] == 'R0003'
               for q in run['quarantine'])


def test_original_sample_unchanged_and_balanced():
    ctx = calculate(SAMPLE.read_text(encoding='utf-8-sig'))
    # 无换码记录时，身份退化为代码本身，既有口径完全不变。
    assert ctx['stages']['2']['metrics'][3] == ['换码已接续', 0, '项']
    assert ctx['stages']['2']['metrics'][4] == ['换码待确认', 0, '项']
    assert ctx['balanced'] is True
    assert all(p['issuer'] == p['canonical'] for p in ctx['positions'])


def test_multihop_chain_connects_three_codes_end_to_end():
    header = ('record,kind,security,alias,event_date,record_date,ex_date,'
              'price,cash,bonus,rights,rights_price,shares,version,reported_cash\n')
    body = (
        'E1,recode,A,B,2026-11-16,2026-11-15,,,,,,,,1,\n'
        'E2,recode,B,C,2026-11-20,2026-11-19,,,,,,,,1,\n'
        'AA1,action,A,,2026-11-12,2026-11-14,2026-11-15,10,0,1,0,0,,1,\n'
        'AB1,action,B,,2026-11-16,2026-11-17,2026-11-18,12,0,1,0,0,,1,\n'
        'AC1,action,C,,2026-11-19,2026-11-21,2026-11-22,14,0,1,0,0,,1,\n'
        'PA,position,A,,2026-11-14,,,,,,,,100,1,0.00\n'
    )
    ctx = calculate(header + body)
    chain = next(c for c in ctx['factor_chains'] if c['发行人身份'] == 'A')
    assert [r['证券'] for r in chain['rows']] == ['A', 'B', 'C']
    # 三个代码的因子在同一条链上连乘，没有因换码断成三条。
    assert len(ctx['stages']['4']['chains']) == 1
    assert ctx['stages']['4']['metrics'][2] == ['复权链接续', 2, '处']


def test_reused_code_keeps_new_issuer_separate_from_old_holdings():
    # 确认换码 A->B 后，另一只新证券在生效日后复用代码 A。
    header = ('record,kind,security,alias,event_date,record_date,ex_date,'
              'price,cash,bonus,rights,rights_price,shares,version,reported_cash\n')
    body = (
        'E1,recode,A,B,2026-11-16,2026-11-15,,,,,,,,1,\n'
        'AA1,action,A,,2026-11-12,2026-11-14,2026-11-15,10,0,1,0,0,,1,\n'
        'AB1,action,B,,2026-11-16,2026-11-17,2026-11-18,12,0,1,0,0,,1,\n'
        'ANEW,action,A,,2026-11-18,2026-11-20,2026-11-21,30,0,1,0,0,,1,\n'
        'POLD,position,A,,2026-11-14,,,,,,,,100,1,0.00\n'
        'PNEW,position,A,,2026-11-20,,,,,,,,50,1,0.00\n'
    )
    ctx = calculate(header + body)
    identities = {p['record']: p['issuer'] for p in ctx['positions']}
    assert identities['POLD'] == 'A'
    assert identities['PNEW'] == 'A@2026-11-16'  # 复用代码的新发行人独立身份
    moves = {(r['持仓记录'], r['行动']) for r in ctx['stages']['3']['rows']
             if r['资格'] == '有登记权益'}
    # 旧持仓只跟旧发行人的行动，绝不落到复用代码的新发行人 ANEW 名下。
    assert ('POLD', 'ANEW') not in moves
    assert ('POLD', 'AA1') in moves and ('POLD', 'AB1') in moves
    assert ('PNEW', 'ANEW') in moves
    # 因子链一分为二：原证券 A->B 一条，新发行人 A@切分日 一条，互不连乘。
    chain_map = {c['发行人身份']: [r['证券'] for r in c['rows']]
                 for c in ctx['factor_chains']}
    assert chain_map['A'] == ['A', 'B']
    assert chain_map['A@2026-11-16'] == ['A']
