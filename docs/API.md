# 接口

`POST /api/run`：JSON中的csv文本，省略使用样例；或multipart字段file。最大5MB。

`POST /api/m1`至`POST /api/m7`：独立运行到指定模块，JSON结构同运行接口。

`GET /api/history`：历史批次。`POST /api/review`接收id、revision和decision（通过或退回）。`GET /api/evidence.csv`提供单一证据导出。

结构错误422，版本变化409，文件过大413；错误返回code与message。所有页面可从左侧七个模块入口访问。

`GET /api/runs/<id>`读取指定批次；页面与证据CSV通过run_id查询参数绑定批次，模块导航保留该参数。

换码（kind=recode）随同一CSV提交。M2返回`lineage_rows`（已确认/待确认换码边）与`lineage`；M4返回`chains`（按发行人身份连乘的复权链，含单次与累计复权因子）、`links`（已接续及接续身份）与`pending_links`（待确认断点）。待确认换码以RECODE_PENDING进入隔离，不合并身份、不连乘因子；补齐归属日期后重跑生成新批次，断点转为可查到接续依据的链接。证据CSV含“换码链/复权链接续/复权链断点/复权链”分段。

JSON请求体必须是对象，数组、标量、null、损坏JSON及错误上传字段返回400，且不运行默认样例。
