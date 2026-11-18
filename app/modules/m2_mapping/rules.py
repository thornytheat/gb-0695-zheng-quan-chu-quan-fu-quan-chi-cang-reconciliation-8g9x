from app.domain import action_math, day
from app.numeric import number
from app.errors import DomainError
from app.reasons import isolate
from app.lineage import build, detect_ambiguous


def _recode_edge(r):
    """解析换码记录；归属日期为空即晚到的归属，按待确认处理，不参与接续。"""
    if not r['record'] or not r['security'] or not r['alias']:
        raise DomainError('MISSING', '换码记录编号或新旧代码为空')
    effective = day(r['event_date'])
    version = int(r['version'])
    if version < 1:
        raise ValueError()
    record_date = r['record_date'].strip()
    if record_date:
        day(record_date)  # 归属日仅做格式校验，不要求与生效日同序
    return {'record': r['record'], 'old_code': r['security'], 'new_code': r['alias'],
            'effective': effective, 'version': version, '行号': r['行号'],
            'record_date': record_date}


def apply(ctx):
    positions = []
    groups = {}
    recode_groups = {}
    seen = set()
    duplicates = 0
    for r in ctx['rows']:
        try:
            if not r['record'] or not r['security']:
                raise DomainError('MISSING', '证券或记录编号为空')
            if r['kind'] not in ('action', 'position', 'recode'):
                raise DomainError('KIND', '业务类别无效')
            day(r['event_date'])
            key = tuple((k, v) for k, v in r.items() if k != '行号')
            if key in seen:
                duplicates += 1
                continue
            seen.add(key)
            if r['kind'] == 'recode':
                edge = _recode_edge(r)
                recode_groups.setdefault(edge['record'], []).append((r, edge))
            elif r['kind'] == 'action':
                action_math(r)
                record, ex = day(r['record_date']), day(r['ex_date'])
                if record >= ex:
                    raise DomainError('DATE_ORDER', '登记日必须早于除权日')
                version = int(r['version'])
                if version < 1:
                    raise ValueError()
                groups.setdefault((r['alias'] or r['security'], r['ex_date']), []).append(
                    {**r, 'version_n': version})
            else:
                shares = number(r['shares'])
                number(r['reported_cash'])
                if shares < 0 or shares != shares.to_integral_value():
                    raise DomainError('SHARES', '持仓须为非负整数')
                positions.append(r)
        except DomainError as e:
            isolate(ctx, r, e.code, e.message)
        except ValueError:
            isolate(ctx, r, 'VERSION', '版本必须为正整数')

    # 换码按记录编号做版本裁决：同版本定义冲突整体隔离，否则取最新版本。
    recodes, recode_history = [], []
    for ident, items in sorted(recode_groups.items()):
        top_version = max(edge['version'] for _, edge in items)
        top = [pair for pair in items if pair[1]['version'] == top_version]
        signatures = {(edge['old_code'], edge['new_code'], edge['effective'],
                       edge['record_date']) for _, edge in top}
        if len(signatures) > 1:
            for raw, _ in items:
                isolate(ctx, raw, 'RECODE_CONFLICT', '同版本换码定义冲突')
            continue
        winner = min(top, key=lambda pair: pair[0]['行号'])
        recodes.append(winner[1])
        recode_history.extend(raw for raw, _ in items if raw is not winner[0])

    confirmed = [e for e in recodes if e['record_date']]
    pending = [e for e in recodes if not e['record_date']]
    for raw in pending:
        isolate(ctx, next(x[0] for x in recode_groups[raw['record']]
                          if x[1]['version'] == raw['version']),
                'RECODE_PENDING', '换码归属记录晚到，接续待确认')

    # 结构歧义（多入/多出/环）不参与身份合并，避免把不相关证券串到同一条链。
    ambiguous = detect_ambiguous(confirmed)
    clean_confirmed = []
    for edge in confirmed:
        if edge['record'] in ambiguous:
            raw = next(x[0] for x in recode_groups[edge['record']]
                       if x[1]['version'] == edge['version'])
            isolate(ctx, raw, 'RECODE_AMBIGUOUS', '换码链存在多入边、多出边或环，归属不清')
        else:
            clean_confirmed.append(edge)

    codes = {r['security'] for r in positions}
    codes.update(r['alias'] or r['security'] for items in groups.values() for r in items)
    codes.update(r['security'] for items in groups.values() for r in items)
    lineage = build(clean_confirmed, pending, extra_codes=codes)
    ctx['lineage'] = lineage

    def json_safe(edge):
        return {k: (v.isoformat() if hasattr(v, 'isoformat') else v)
                for k, v in edge.items()}

    # 证券身份（发行人）解析：按记录自身日期落位，复用代码不会串到另一只证券。
    def issuer_of(code, on_date):
        issuer, _ = lineage.resolve(code, day(on_date))
        return issuer

    actions = []
    history = []
    for key, items in sorted(groups.items()):
        version = max(r['version_n'] for r in items)
        top = [r for r in items if r['version_n'] == version]
        signatures = {tuple(r[k] for k in
                            ('price', 'cash', 'bonus', 'rights', 'rights_price', 'record_date'))
                       for r in top}
        if len(signatures) > 1:
            for r in items:
                isolate(ctx, r, 'CONFLICT', '同版本公司行动定义冲突')
            continue
        winner = min(top, key=lambda r: r['record'])
        winner['canonical'] = winner['alias'] or winner['security']
        winner['issuer'] = issuer_of(winner['canonical'], winner['ex_date'])
        actions.append(winner)
        for r in items:
            if r is not winner:
                r['canonical'] = r['alias'] or r['security']
                r['issuer'] = issuer_of(r['canonical'], r['ex_date'])
                history.append(r)

    unique, valid = {}, []
    for r in positions:
        canonical = r['alias'] or r['security']
        item = {**r, 'canonical': canonical,
                'issuer': issuer_of(canonical, r['event_date'])}
        key = (r['record'], item['issuer'], r['event_date'])
        if key in unique:
            isolate(ctx, r, 'POSITION_CONFLICT', '同日持仓记录冲突')
            continue
        unique[key] = item
        valid.append(item)

    ctx.update(actions=actions, action_history=history, positions=valid,
               recodes=[json_safe(e) for e in clean_confirmed],
               recode_history=recode_history,
               recode_pending=[json_safe(e) for e in pending],
               duplicates=duplicates)

    rows = [{'记录': r['record'], '类别': r['kind'], '证券': r['canonical'],
             '发行人身份': r['issuer'], '版本': r['version'], '行号': r['行号']}
            for r in actions + valid]
    lineage_rows = lineage.edges() + lineage.pending_edges()
    metrics = [['有效行动', len(actions), '项'], ['持仓', len(valid), '条'],
               ['旧版行动', len(history), '项'],
               ['换码已接续', len(clean_confirmed), '项'],
               ['换码待确认', len(pending), '项']]
    return {'rows': rows, 'metrics': metrics,
            'lineage_rows': lineage_rows,
            'lineage': lineage.to_payload()}
