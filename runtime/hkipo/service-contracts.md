# 本地服务周期接口

本模块把既有监控、原响应留证、PDF下载、窄规则分类与worker接起来；无import启动、副作用发送、launchd安装。默认`enable_writer=False`。没有给SMTP/微信/下载器注入代理；`writer_options={'settings_path': '/absolute/local-settings.json'}`仅透传既有writer。

```python
from hkipo.service import ServiceSettings, LocalService
settings = ServiceSettings(
    data_dir='/absolute/private/data', scan_seconds=60,
    overlap_hours=.25, initial_days=3, aggregate_seconds=900,
    skill_paths={'offer':'/absolute/project/skills/hkipo-editor',
                 'allotment':'/absolute/project/skills/hkipo-editor',
                 'exception':'/absolute/project/skills/hkipo-editor'},
    writer_options={'settings_path':'/absolute/runtime/local-settings.json'},
    enable_writer=False,
)
service = LocalService(settings)
service.cycle(scan=True, classify=False, work=False) # 单次纯发现
service.cycle(scan=False, classify=True, work=False) # 下载/分类，不用模型
# 明确允许writer并由root控制额度后：
# service.cycle(scan=False, classify=True, work=True)
```

`cycle(scan=True,classify=False,work=False,now=None)`返回scan/classification/work各子结果与health。三者均True是有界前台整周期，适合离线回放与显式单次研究。worker可注入fake供测试；没有注入且enable_writer=False时，work返回disabled，即使误传work=True也不触真实模型。

生产循环建议`run_loop(stop_event,max_cycles=None,dispatch=True)`：独占monitor-loop.lock，主进程只运行scan周期，再`dispatch_pipeline()`启动独立spawn进程做分类、可选writer。写作慢不会阻塞下一次发现。父进程记录自身子进程对象，活着时不再启动第二个；跨父进程仍由pipeline.lock阻止重复。每个子进程只做max_classification_groups及max_worker_groups有界批次后退出。stop_event停止发现循环；已进入写作的子进程可完成当前有界批次，不声称stop会强杀模型。

scan和pipeline各有独立非阻塞flock，忙时返回busy，不排无限等待。SQLite claim/lease继续由store管理；scan-state.json和classification-state.json分开避免两个进程覆盖状态。未安装或启动任何真实循环。

## 官方响应审计与水位

SourceResult新增`responses=[{'url': official_request_url,'format':'html'|'json','body': decoded_response}]`。真实AggregateSource保留实际抓得的HTML；TitleSearchSource保留每次成功累计分页的原JSON字符串，不重造类别或逐行角色。

LocalService通过_AuditedSource包装：源已有有界网络重试、429及官方语言后备执行后，必须有complete响应及raw body，先在`data/snapshots`存档成功，Monitor才提交事件/成功水位。存档失败、缺raw、解析错误和空失效聚合页均不推进成功水位。旧lastgood未被失败覆盖。这里只存成功通过来源解析的响应；失败抓取只留安全错误码，未声称已存网络失败时未取得的body。

- bodies按SHA256寻址，文件0600、快照目录0700，临时文件fsync+原子rename；相同内容不反复占空间。
- latest/source-hash.json记录来源、原解码响应SHA、实际存储SHA、sanitized标记、请求URL、捕获时间、计数。hash指解码UTF-8响应正文，不冒充压缩前HTTP原始字节hash。
- 不保存headers、cookies；隐藏值/会话URL参数/token键去值，HTML脚本与session meta移除。只在确实完全未改时original_sha256=sha256。保留后的HTML仍是实际响应中的表格内容，不编造role。官方请求URL只允许公开搜索/聚合路径与已知无凭据参数。
- 分类重新校验存储hash，再调用aggregate_evidence/title_evidence。损坏快照进入snapshot_errors与health，不拿summary当原始角色依据。

## 分类与材料衔接

只处理已有非baseline或时间不确定baseline=2、且能在真实官方角色证据中找到URL的候选；明确初始baseline=1不下载全历史、不排写作。标题误命中的无正式角色文档进入needs_classification，不让上千衍生产品任务阻塞普通IPO。

相同代码、语言、事件类型与公开日期归组，从已入库事件中补同批公告/招股书；下载后交窄规则分类，审计JSON存classification。常见真实双材料可自动confirm_classification，后续worker按episode合并成一篇。初始date-only候选的类型确证不等于时序确证：service不会自动promote baseline=2；精确标题源以后见到并晋升或人工显式回补，才排job。避免将启动前同日旧文件误当新通知。

已确认分类输入按源event IDs+实际角色/日期/locator规则指纹缓存；整个当日响应增加一条无关普通公告不会反复下载同一招股书。材料补齐、角色变化或规则版本变化会形成新输入。下载失败及Retry-After记classification-state，重启仍等待；失败组不挤占后续有界可执行组。

allotment/exception只匹配classification目录里已证实的offer episode，不能把普通老股配售当中签。新服务启动前IPO若没有已确认episode，仍需先补录该期的已证实审计，不能凭空构造。多份正式followup匹配同episode可合并角色；跨episode冲突留needs_classification。

## 频率与健康

默认主搜索60秒、聚合900秒、重叠15分钟；没有硬编码工作日07—10。重启从各源持久cutoff减重叠继续；首次最多3天基线。标题接口目前只证实日期粒度参数，仍须完整拉取边界日；没有编造小时参数或以首屏100条替代全量。短重叠避免每分钟固定重复3天，但当日公告很多时仍可能较重，不保证60秒全周期完成或绝对最快。

health.json聚合源告警、下载失败、快照损坏、各阶段最近结果和外发禁用标记；health-components保留不同进程阶段，避免扫描覆盖分类诊断。故障不发消息；恢复后沿既有store告警resolved。常见待分类/Word视觉QA门属于业务待办，不能把API或本地文件存在当交付完成。

## 离线验证

10项service测试及13项monitor回归通过。整周期正例以真实2026-05-08旧聚合快照metadata行+真实翼菲公告/437页招股书，经下载验证、分类、episode材料合并、fake writer得到本地ready；相同事件重跑不写第二篇。此旧metadata回放须显式allow_legacy_replay=True，格式标记legacy_aggregate，默认生产禁用，未伪称保存了该历史原HTML。

另用2026-11-19真实原聚合HTML验证raw留证/hash/16链接、不重放初始baseline；覆盖缺raw不推进水位、坏源/429跨重启与恢复、session去值及tamper、单实例/独立进程分派、writer默认禁用、15分钟重叠补漏、时间模糊基线不误重放及settings_path仅到fake writer。所有HTTP/进程/model均为fake或现有本地PDF；没有实际联网/CLI/发送/安装。

限制：已ready的同URL静默替换仍需显式重检策略，当前不每分钟重下所有既有大PDF；本模块未提供真实常驻稳定性或生产吞吐的运行证据，需root后续经受控启动验证。
