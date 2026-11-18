from datetime import date, timedelta
from decimal import Decimal
from app.domain import action_math
from app.numeric import fmt
from app.lineage import Lineage


def apply(ctx):
    rows = []
    factors = {}
    by_issuer = {}
    for a in ctx['actions']:
        reference, factor = action_math(a)
        factors[(a['canonical'], a['ex_date'])] = {'reference': reference, 'factor': factor}
        by_issuer.setdefault(a['issuer'], []).append((a, reference, factor))
        rows.append({'证券': a['canonical'], '发行人身份': a['issuer'], '除权日': a['ex_date'],
                     '前收盘_CNY': a['price'], '参考价_CNY': fmt(reference),
                     '复权因子': fmt(factor, '0.00000001'), '行动版本': a['version']})

    # 复权链按发行人身份接续：同一发行人跨代码的因子连乘；待确认换码处断开不连乘。
    chains = []
    for issuer, items in sorted(by_issuer.items()):
        items.sort(key=lambda t: (t[0]['ex_date'], t[0]['record']))
        cumulative = Decimal(1)
        chain_rows = []
        for a, reference, factor in items:
            cumulative *= factor
            chain_rows.append({'发行人身份': issuer, '证券': a['canonical'],
                               '除权日': a['ex_date'], '单次因子': fmt(factor, '0.00000001'),
                               '累计复权因子': fmt(cumulative, '0.00000001'),
                               '行动': a['record']})
        chains.append({'发行人身份': issuer, 'rows': chain_rows,
                       '链尾因子': fmt(cumulative, '0.00000001')})

    # 接续依据：已确认换码两侧解析到同一发行人身份才算接通；
    # 待确认换码只在 pending_links 展示，不产生接续，防止因子串到复用代码的新证券。
    lineage = ctx['lineage']
    if isinstance(lineage, Lineage):
        links = lineage.edges()
        pending = lineage.pending_edges()
        connected = 0
        for link in links:
            effective = date.fromisoformat(link['生效日'])
            old_issuer, _ = lineage.resolve(link['原代码'], effective - timedelta(days=1))
            new_issuer, _ = lineage.resolve(link['新代码'], effective)
            link['接续身份'] = new_issuer
            if old_issuer == new_issuer:
                link['接续结论'] = '已接续'
                connected += 1
            else:
                link['接续结论'] = '待确认'
    else:
        # deepcopy 还原后的普通 dict：身份与链接已固化，直接沿用已存储结论。
        payload = lineage
        links = [dict(x) for x in payload.get('确认换码', [])]
        pending = [dict(x) for x in payload.get('待确认换码', [])]
        connected = sum(1 for x in links if x.get('状态') == '已确认')
        for link in links:
            link.setdefault('接续结论', '已确认')
            link.setdefault('接续身份', link.get('新代码', ''))

    ctx['factors'] = {str(k): v for k, v in factors.items()}
    ctx['factor_chains'] = chains
    return {'rows': rows,
            'metrics': [['参考价', len(rows), '项'], ['因子', len(factors), '项'],
                        ['复权链接续', connected, '处'],
                        ['复权链断点', len(pending), '处'],
                        ['冲突隔离', sum(r['原因码'] == 'CONFLICT' for r in ctx['quarantine']), '条']],
            'chains': chains, 'links': links, 'pending_links': pending}
