from app.domain import day
from app.lineage import Lineage

def apply(ctx):
    rows=[];eligible=[];selected=set()
    lineage=ctx['lineage']
    def identity_matches(p,action):
        # 权益按发行人身份匹配：换码后旧代码即便被新发行人复用，
        # 旧持仓的身份与新行动不一致，不能落到新发行人名下。
        if p['issuer']!=action['issuer']:
            return False
        if isinstance(lineage,Lineage):
            return lineage.resolve(p['canonical'],day(p['event_date']))[0]==p['issuer']
        return True  # deepcopy 还原后身份已固化在行上
    for action in ctx['actions']:
        matching=[p for p in ctx['positions']
                  if identity_matches(p,action)
                  and day(p['event_date'])<=day(action['record_date'])]
        latest={}
        for p in matching:
            prior=latest.get(p['record'])
            if prior is None or (p['event_date'],p['行号'])>(prior['event_date'],prior['行号']): latest[p['record']]=p
        for p in sorted(latest.values(),key=lambda x:x['record']):
            eligible.append({'position':p,'action':action});selected.add((p['record'],p['event_date']))
            rows.append({'持仓记录':p['record'],'证券':p['canonical'],'发行人身份':p['issuer'],'资格':'有登记权益','股份':p['shares'],'行动':action['record']})
    for p in ctx['positions']:
        if (p['record'],p['event_date']) not in selected:
            rows.append({'持仓记录':p['record'],'证券':p['canonical'],'发行人身份':p['issuer'],'资格':'无登记权益','股份':p['shares'],'行动':''})
    ctx['eligible']=eligible
    return {'rows':rows,'metrics':[['有权益',len(eligible),'条'],['无权益',sum(r['资格']=='无登记权益' for r in rows),'条'],['登记日',' / '.join(sorted({a['record_date'] for a in ctx['actions']})),'']]}
