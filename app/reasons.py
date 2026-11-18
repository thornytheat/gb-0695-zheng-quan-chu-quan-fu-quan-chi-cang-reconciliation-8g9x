REASONS = {"COLUMNS":"列数不符", "HEADER":"字段缺失", "MISSING":"必填值为空", "NUMBER":"数值无效", "RANGE":"超出范围", "DUPLICATE":"相同行重复", "CONFLICT":"同键数值冲突", "VERSION":"版本冲突", "EMPTY":"输入为空", "STRUCTURE":"文件结构错误",
"RECODE_PENDING":"换码归属待确认", "RECODE_CONFLICT":"同版本换码定义冲突", "RECODE_AMBIGUOUS":"换码链归属不清", "RECODE_DATE":"换码日期无效"}
def isolate(ctx, row, code, detail):
    ctx['quarantine'].append({**row, '原因码': code, '原因': detail})
