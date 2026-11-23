# PDF下载与研究worker契约

本单元只实现下载、持久队列领取和调用既有writer的本地研究衔接；没有发送、草稿创建、常驻安装或测试时真实模型调用。沿用StateStore.dbpath，不创建第二套业务数据库。

```python
store = StateStore('/private/runtime/state.sqlite3')
downloader = PDFDownloader('/private/runtime/pdf', store)
worker = Worker(store, downloader, '/private/runtime/research',
                {'offer': '/absolute/offer/SKILL.md',
                 'allotment': '/absolute/allotment/SKILL.md',
                 'exception': '/absolute/exception/SKILL.md'}, writer=fake_or_none)
worker.run_one()       # 一组；writer=None时通过门槛后才显式调用真实write_article
worker.run_batch(10)   # 有界批次，一股失败继续下一股
```

## 先确认发行人、文档类型和同次发行身份

`store.confirm_classification(event_id, issuer_type, event_type, document_role, episode_id, promote_candidate=False)`持久记录外部已确认的分类，不调用模型猜测。

- issuer_type：common_equity / etf / reit / other。
- event_type：ipo_offer / ipo_allotment / ipo_exception / refinancing / preliminary / other。
- document_role：announcement / prospectus / allotment / exception / other。
- episode_id：同一发行事件明确共享的稳定字符串；同代码不同次事件必须不同。

只有common_equity及与源事件kind一致的ipo类型能进writer。其余待needs_classification；不自动把ETF、REIT、老股配股融资和AP/PHIP当普通IPO。分类未确认时不会为了分类型先调用模型。

offer同股票/语言/episode必须有announcement和prospectus两种已确认角色；仅一份公告进入waiting_sources，不写财务猜测。后续另一份文档入库pending时，同episode的waiting_sources任务一起领取；可匹配已入库历史PDF，但必须明确标为同episode。allotment/exception单份正式文档即可。

旧events.baseline=2候选不自动排队：显式确认分类且promote_candidate=True才可晋升。baseline=1历史不会被此参数批量重放。初始来源推断不足和待补材料均保留，不谎报已完成。

## 原子领取与中断恢复

`store.claim_job_group(worker_id, lease_seconds=1800, max_attempts=3, now=None)`返回token/event_ids/events/classifications/lease_until或None。同股票/kind/语言/episode一次事务领取，多个worker不能同时拥有同组；不同episode不相互覆盖。所有写事务BEGIN IMMEDIATE。

`finish_claim(token,status,reason=None,retry_seconds=0,now=None)`只允许未过期token完成；旧worker失去lease后不能把任务宣称完成。`renew_claim(token,lease_seconds)`续租。worker运行期间只有一个生命周期内heartbeat线程，结束立即stop/join，不是安装后台服务。超时退出可在lease过期后重领，attempts达到上限停止自动执行。下载及写作异常留安全类型/短原因，下一股继续。

claim_job_group、finish_claim、renew_claim及finish_research_claim的真实时间均在取得BEGIN IMMEDIATE写锁和完成schema检查后采样。等锁期间自然到期的lease不能完成、续租或发布research；领取也以锁后时间回收过期owner并计算完整新lease。claim_job_group/finish_claim显式传入now时仍使用该测试时钟；claim_token、running状态及lease截止时间三项SQL fencing保持不变，旧owner不能覆盖新owner的任务或研究结果。

状态：pending/running/failed/blocked/needs_classification/waiting_sources/ready_for_delivery。ready_for_delivery仅表示writer的本地待交付状态，后续Word视觉QA、release冻结与delivery预检仍必须执行。Worker不会调用delivery。

## PDF与版本缓存

`PDFDownloader(root,store,fetcher=None,attempts=2,max_bytes=150MiB)`；可注入`fetcher(url)->(bytes,final_url)`离线验证。仅接受已观察到的官方HTTPS精确主机www1.hkexnews.hk/www.hkexnews.hk/www2.hkexnews.hk，路径需.pdf；每次redirect也检查白名单。HTTP错误由urllib抛出，HTML和伪PDF无法落库。

验证`%PDF-`签名、大小、PyMuPDF可打开、有页、未加密且逐页可读取结构。扫描页可打开不代表可提取文字，后续evidence/quality仍负责unreadable阻断。验证成功后临时文件fsync+os.replace到SHA256.pdf；旧hash文件始终保留。pdf_versions表记录URL/hash/最终URL/页数/时间；`download(url)`返回DownloadedPDF(url,final_url,path,sha256,pages,downloaded_at,changed,previous_sha256)，`history(url)`查版本。HTTP429不立即重试，Retry-After秒数或HTTP日期转为job retry_after持久等待，其他网络失败有界退避。

每次真正研究前重新抓取选定源；相同URL新正文产生新hash、不同研究缓存ID，旧证据不覆盖。已ready任务不会自发联网重刷；如需检查原URL静默替换，调用方可显式把对应已完成job置pending再运行worker，或后续实现有界周期重检，不能声称本单元已自动发现所有静默替换。

writer的event_id是稳定逻辑ID=SHA256(stock_code+kind+episode_id)，不同体裁kind必分离；research_id作为版本目录/缓存ID，绑定stock/kind/episode及排序后的源event_id+PDF SHA。同URL正文变更有新research_id但同logical_event_id，delivery可更新该体裁同一逻辑草稿。writer输入保留source_event_ids/source_events/source_hashes/episode_id，pdf_paths为实际去重后的源文件。相同hash组合已ready则重用，不再次写作；任何hash变化失效。research_runs保存输入来源及writer返回，目录按研究ID隔离。

worker_stages记录first_seen/downloaded/written/blocked及时间。first_seen处理记录另保留原discovered_at，published_at及精度，不把聚合日期00:00当真实分钟发布时间，不据此虚构发现延迟KPI。

## 验证

3项下载测试及11项worker测试通过：签名/坏PDF/HTML/非官方redirect、重试、旧内容保留、新hash缓存失效、公告等待招股书补齐、未确认和ETF阻断、单份中签/异常、原子并发claim、lease失效与重试上限、单股失败继续、同股不同发行隔离、baseline候选显式晋升、Retry-After600秒持久等待及失租后不发布ready。测试全部用生成PDF、fake HTTP、fake writer；没有真实Codex/API交付。

## 实时额度延期与原子恢复

额度触线只接受quota_gate本轮读取并由evaluate_limits生成的专用QuotaDeferred；不从异常文案推测。五小时剩余额度<=20%或真实更长window耗尽且提供未来resetsAt时，记录blocking_windows、blocking_resets_at、observed_at及reason。多个阻塞window取最晚实际reset，retry_at为该值+120秒；周窗耗尽不能用五小时reset代替。spend control、未知rateLimitReachedType、缺失/失效reset或不可验证读数没有可靠恢复时间，保持QuotaUnavailable原有有界失败流程，不伪造未来重试。

write_article仅将该专用异常及仍新鲜的observation转成`status=deferred, quota_defer={reason,retry_at,observation}`；其他异常仍抛出，事实/视觉/配置门不自动解封。Worker和StateStore再次验证observation不超过60秒、阻塞window数值及未来reset，重新计算retry_at，不信任传入的retry_at字段。每次真实模型调用仍先quota_gate，重试不会复用旧的允许读数。

StateStore.defer_research_claim在同一BEGIN IMMEDIATE事务内，用claim_token及未过期lease fencing：释放为pending，持久retry_after，attempts只减回本次claim增量，并将research_runs保留为deferred。失效token/lease不写研究缓存。重启后和同episode新材料也不能提前绕过该等待；到期只获得重新claim及重读额度的机会，不承诺一定写作成功。未知额度失败、普通writer非ready结果仍按原有限次failed/blocked逻辑处理；无需安装新调度，不重置或购买额度。

本次验证只使用fake quota/clock/writer及本地fixture PDF：reset+120、周窗、未知硬限、实时性、重启等待、重复延期不耗尽业务attempt、失效token/lease fencing及普通blocked维持不变；没有真实模型、网络或外发。

锁等待边界增量验证：临时SQLite另一连接真实持锁1.3秒，跨过1秒lease；finish/renew/finish_research均拒绝修改，保留running/attempts/token/lease且不写研究缓存。领取等待后可回收过期owner并发放以锁后时间计算的新lease，旧token无法完成、续租或覆盖既存研究；2020年显式now仍可确定性完成和计算retry_after。worker针对性回归18项通过，包括原有quota defer锁等待测试；未扩为生产负载验证。

## 2026-11-23 审核后定向修订

以上普通writer非ready直接blocked的描述，现由以下行为细化：needs_source先对本次完整原文定向研究，必要时仅核对具体请求页；保留上一稿和可复算来源，不因首轮补源要求直接退出。每轮重跑独立事实/作者规范审核与机械校验，revision-history记录解决/新增/仍存问题。新一轮阻断更多时保留问题更少的稿件作下一轮编辑底稿。补源结论只是研究提示，不能替代独立审核；全文范围核实后的not_disclosed不得写成没有。

默认每次六轮修订。确有进展但本批尚未完成时，revision_pending在finish_research_claim同一lease fencing事务中转pending，持久60秒retry_after，退回当前claim的一次业务attempt；下批继续精确同源/规范/模型/作者输入的已完成稿件审核对。不设跨批次的累计轮数截断；后续批次仍有实际问题减少则继续。近期多轮没有实际进展或确实无法取得必要来源时保留真实阻断供故障修复，不伪造ready。其他失败、真实源不可得和未通过状态保持可见，监控health纳入blocked/failed。

缓存比较排除下载时间等易变审计字段，仍绑定官方PDF和原生逐页文字hash、规范、事件/发行期、模型及作者输入；新增可信图审证据触发重验，不丢弃同一正式原文的已有稿件。原始input-manifest及逐轮结果保留。

机器运算增加严格shares / shares_per_lot → lots，以及BASE货币金额 / BASE_per_QUOTE → 同数量级QUOTE金额。依赖计算先逐层复算，允许无环计算ID引用，拒绝环、深度超限、未知/不受支持输入、错币种方向、错单位或错结果；独立审核覆盖所有上游节点和原始事实。不从自由文本猜换算。
