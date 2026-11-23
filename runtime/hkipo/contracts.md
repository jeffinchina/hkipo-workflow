# 监控模块集成契约

模块均为标准库Python，不执行模型、发信、草稿或系统任务。以runtime为PYTHONPATH。

```python
from hkipo.store import StateStore
from hkipo.sources import TitleSearchSource, AggregateSource
from hkipo.monitor import Monitor
store = StateStore('/path/to/private/state.sqlite3')
monitor = Monitor(store, [TitleSearchSource(), AggregateSource('Main-Board'), AggregateSource('GEM')], interval_seconds=60)
result = monitor.scan()  # 单次，只读联网；本地写状态
```

`DocumentEvent` frozen dataclass字段：event_id/source/source_id/stock_code/stock_name/kind/title/url/published_at/discovered_at/language/version。传event_id=''自动生成，非空则验证匹配。代码统一5位。ID=canonical HTTPS官方文档URL+kind+version+language+代码的SHA256；不同源同一文档同代码去重，多证券关联分别保留。source_id仅作来源证据，避免新上市聚合和搜索ID不同造成重复。URL不剔除query（保守避免不同文件误合并）。kind为offer/allotment/exception/preliminary/other。`to_dict()`输出可序列化字典。

`StateStore(dbpath)`：

- `ingest(events, baseline=False)`：返回实际新增DocumentEvent列表。非baseline且kind为offer/allotment/exception时创建唯一pending job。
- `commit_scan(source, events, cutoff, baseline=False, timestamp_precision=None)`：原子摄入和成功水位，返回(新增列表,是否首次基线)。系统启动时持久化monitor_started锚点，初轮不批量创建历史jobs；晚恢复来源不再一概算历史。日期精度的同日候选baseline=2暂不建job，精确标题源确认后可晋升唯一job。cutoff必须带时区，存UTC、不回退。
- `source_state(source)`：字典或None。字段last_success/cutoff/last_failure/failure_count/error/retry_not_before。
- `record_failure(source,safe_error)`：仅失败字段及每源一条开放alert，不推进cutoff；恢复后alert resolved。
- `jobs(status=None)`返回列表；`set_job_status(event_id,status)`接受pending/running/blocked/complete/failed。这里只提供状态存储，不是多worker领取协议；多worker需根另加原子claim，不能先查再置running。
- `begin_delivery(key,event_id,channel)`：插入unknown，只有第一次True；unknown/confirmed/failed已有项均False，调用方必须显式对账后决定恢复，禁止遇unknown直接再发。
- `finish_delivery(key,status,receipt=None)`：unknown/confirmed/failed；`delivery(key)`查询。发送方不在本模块。
- `alerts()`：未恢复告警列表。外部不得直接把alert列表作为发送授权。

SQLite每操作独立连接、30秒busy_timeout、BEGIN IMMEDIATE写事务、唯一约束；适合同机多个进程/线程，非网络共享盘分布式锁。

来源接口：`.name`及`.fetch(start_aware_datetime,end_aware_datetime)->SourceResult(source,events,complete,count,evidence)`。失败抛SourceError，不回空列表伪装成功。

`HttpClient(attempts=2,timeout=25,max_retry_after=120)`：HTTP/网络有界重试+退避，429尊重Retry-After。要求等待超过上限时返回deferred失败，不提前重试；Monitor将retry_not_before持久化，重启或下一轮也不提前请求。官方HTTPS限制包括重定向。可注入`.get(url)->str`替身离线测试。

`Monitor.scan(now=None)`：逐源单次抓取；每源成功原子保存，某源失败保留其lastgood/cutoff，返回总ok=false。默认成功水位回看72小时，首轮7天。`pending_classification()`返回晚恢复聚合日期不够精确的候选，Monitor.scan也返回pending_classification；这些候选不会永久吞掉，而由已运行的精确标题源见到后晋升。interval_seconds=60只为调度目标，不开常驻循环、不保证实际延迟。抓取窗口按日期覆盖，因此会读取包含边界日的全部公告，以ID去重。

## 线上核验与fixture

2026-11-19只读POST官方titlesearch首屏100/当天总587；官方 `/ncms/js/titlesearch_research.js` 明确GET `titleSearchServlet.do`采用累计rowRange、fromDate/toDate。实测rowRange=600返回loadedRecord=recordCnt=587，hasNextRow=false；result是嵌套JSON字符串。完整公开数据fixture `tests/fixtures/title-search-real.json` 含587文档、1203个证券关联，已通过解析。页面session和表单状态未存fixture。主板聚合真实表格抽取16个PDF，包括03275同一公司三链接；`main-board-real.html`仅公开表格。GEM真实空表`gem-real.html`按失败保守处理。

主板/GEM聚合用同一table schema，多单元格、多链接均保留；没有将AP/PHIP提升为正式offer。分类只是文档入口分流，不是已核验发行事实；现有公司再融资、ETF等可能命中，需要下游按原文/发行人类型进一步排除。preliminary/other留存但不创建分析job。

## 实测限制

- 官方titleSearch累计分页每次重新取前N条。总数变化、重复/缺页、超过max_records均整体失败，下一轮重叠恢复；不是宣称全日无新增。
- 聚合仅完整性后备，不是最快源；中英文官方路径作为同源后备，不能视作两独立权威。GEM当前空表无法区分确实无发行与页面失效，因此暂报源异常而不出“无新股”。
- 标题识别的补充/更正会单列URL事件；同URL悄然替换正文目前不能识别，需要下载层加入内容hash版本。
- 聚合URL日期只说明文件路径日期，published_at以当日00:00+08:00近似；搜索才有官方分钟时间。不可用聚合时间计算监控延迟KPI。
- 没有建立未见公告的绝对完整性证明。首次基线不发历史job，若业务需当日新事件首轮发出，应由人工审核选定baseline cutoff再显式回放。
- 没有运行常驻、真实投递或无新股邮件。本轮验证是只读线上抓取及22项监控/存储离线单元测试。

修复回归：标题查询边界先转Asia/Hong_Kong再strftime，避免UTC午夜错日。系统锚点跨失败/重启保存；晚恢复源旧日期历史不排job，同日近似时间保留候选，不能从聚合午夜推断精确发布时间。
