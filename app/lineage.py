"""换码归属与证券身份（发行人）时间线。

一只证券换码后旧代码可能被另一只新证券复用，因此复权链与登记权益不能按
代码直接拼接。本模块把每个代码沿时间轴切成若干归属片段（segment），每条换码
边在生效日同时切开原代码与新代码两条时间线，再沿换码边把归属同一证券的片段
归到同一个发行人身份：

- 已确认换码（有归属日期）：旧侧切分前片段 与 新侧切分后片段 同身份；该身份
  沿换码边传递，多跳换码（A→B→C）后三段都追溯到最初代码 A；
- 旧侧切分后片段不与任何边相连，留待复用该代码的新证券，得到独立身份
  （代码@生效日），因此旧持仓不会落到复用代码的新发行人名下；
- 待确认换码（归属缺失）同样切开两侧但不传递身份，新侧切分后片段身份带
  #待确认 标记，复权链在此断点不连乘；
- 无法裁决的结构（多入边、多出边、环）由 detect_ambiguous 先剔除并隔离。

时间区间一律使用半开边界：[起点, 切分日) 归属旧侧，[切分日, ...) 归属新侧。
所有日期入参为 datetime.date，解析在 M2 完成，本模块只做纯数据运算。
"""

from collections import defaultdict


class Lineage:
    def __init__(self, code_segments, identity, confirmed_flag, edges, pending):
        # code_segments: code -> [(start_iso or None, identity, confirmed)] 时间升序
        self._code_segments = code_segments
        self._identity = identity            # (code, start_iso) -> 身份
        self._confirmed = confirmed_flag      # (code, start_iso) -> 归属是否确认
        self._edges = edges
        self._pending = pending

    def resolve(self, code, on_date):
        """返回 (发行人身份ID, 归属是否已确认)。待确认片段身份带 #待确认 标记。"""
        segs = self._code_segments.get(code)
        if not segs:
            return code, True
        chosen = segs[0]
        day_iso = on_date.isoformat()
        for start, ident, confirmed in segs:
            if start is None or day_iso >= start:
                chosen = (start, ident, confirmed)
        return chosen[1], chosen[2]

    def edges(self):
        return list(self._edges)

    def pending_edges(self):
        return list(self._pending)

    def to_payload(self):
        return {'确认换码': self._edges, '待确认换码': self._pending}


def build(confirmed, pending, extra_codes=()):
    """confirmed/pending 元素统一为 dict：
    record, old_code, new_code, effective(datetime.date), version, 行号,
    record_date（已确认边为归属日文本，待确认边为 ''）。
    调用方须先用 detect_ambiguous 剔除结构歧义边。
    """
    cuts = defaultdict(set)       # code -> {切分日 iso}
    codes = set(extra_codes)
    for e in confirmed + pending:
        codes.update((e['old_code'], e['new_code']))
        cuts[e['old_code']].add(e['effective'].isoformat())
        cuts[e['new_code']].add(e['effective'].isoformat())

    # 每个代码按切分日展开为半开片段，起点为 None 表示时间线最初归属。
    code_segments = {code: [None] + sorted(cuts.get(code, ())) for code in sorted(codes)}
    identity, confirmed_flag = {}, {}
    for code, starts in code_segments.items():
        for start in starts:
            key = (code, start)
            # 默认身份：基础片段用代码本身；切分后片段用 代码@起算日，天然不与原证券重名。
            identity[key] = code if start is None else '%s@%s' % (code, start)
            confirmed_flag[key] = True

    # 待确认边：新侧切分后片段标记待确认，身份不向任何边传递（复权链断点）。
    for e in pending:
        key = (e['new_code'], e['effective'].isoformat())
        identity[key] = '%s@%s#待确认' % (e['new_code'], e['effective'].isoformat())
        confirmed_flag[key] = False

    # 已确认边：旧侧切分前片段 -> 新侧切分后片段，身份沿边收敛到同一根。
    # 用并查集在“片段键”上合并；多跳边通过传递闭包自然追溯到最初代码。
    parent = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def link(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for code, starts in code_segments.items():
        for start in starts:
            find((code, start))
    for e in confirmed:
        cut_iso = e['effective'].isoformat()
        old_side = (e['old_code'], _start_before(code_segments[e['old_code']], cut_iso))
        new_side = (e['new_code'], cut_iso)
        link(old_side, new_side)

    # 组件根身份：取组件内“基础片段（start 为 None）代码”，否则取最小片段键。
    groups = defaultdict(list)
    for key in list(parent):
        groups[find(key)].append(key)
    final_identity = {}
    for root, members in groups.items():
        bases = sorted(code for code, start in members if start is None)
        if bases:
            name = bases[0]
        else:
            code, start = min(members)
            name = '%s@%s' % (code, start)
        # 组件含待确认片段则整体标注待确认。
        is_pending = any(not confirmed_flag[m] for m in members)
        if is_pending:
            name += '#待确认'
        for m in members:
            final_identity[m] = name
            if is_pending:
                confirmed_flag[m] = False

    ordered = {}
    for code, starts in code_segments.items():
        ordered[code] = [(start, final_identity[(code, start)], confirmed_flag[(code, start)])
                         for start in starts]

    def edge_row(e, status):
        return {'换码记录': e['record'], '原代码': e['old_code'], '新代码': e['new_code'],
                '生效日': e['effective'].isoformat(), '版本': e['version'],
                '归属日期': e.get('record_date', ''), '状态': status, '行号': e['行号']}

    edge_rows = [edge_row(e, '已确认')
                 for e in sorted(confirmed, key=lambda e: (e['effective'], e['record']))]
    pending_rows = [edge_row(e, '待确认')
                    for e in sorted(pending, key=lambda e: (e['effective'], e['record']))]

    return Lineage(ordered, final_identity, confirmed_flag, edge_rows, pending_rows)


def _start_before(starts, cut_iso):
    """返回该代码时间线上严格早于切分日的最后一个起点（基础片段为 None）。"""
    prior = None
    for start in starts:
        if start is not None and start >= cut_iso:
            break
        prior = start
    return prior


def detect_ambiguous(edges):
    """结构裁决：任一代码出现多入边/多出边，或存在环，则涉及的边全部判歧义。
    返回有歧义的换码记录 record 集合。"""
    incoming, outgoing = {}, {}
    for e in edges:
        incoming.setdefault(e['new_code'], []).append(e)
        outgoing.setdefault(e['old_code'], []).append(e)
    bad = set()
    for items in (*incoming.values(), *outgoing.values()):
        if len({x['record'] for x in items}) > 1:
            bad.update(x['record'] for x in items)
    nxt = {e['old_code']: e['new_code'] for e in edges if e['record'] not in bad}
    for start in nxt:
        seen, cur = set(), start
        while cur in nxt:
            if cur in seen:
                bad.update(e['record'] for e in edges
                           if e['old_code'] in seen or e['new_code'] in seen)
                break
            seen.add(cur)
            cur = nxt[cur]
    return bad
