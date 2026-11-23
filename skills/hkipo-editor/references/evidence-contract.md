# 证据与唯一契约

在当前项目根定位 `runtime/hkipo/contracts.py`，导入 `ARTICLE_SCHEMA`、`REVIEW_SCHEMA`、`CORE_FIELDS`、`HARD_FIELDS`；若不存在则先定位宿主项目，不自行复制旧schema充当当前契约。

完整PDF/公告和逐页文本均须可达。记录文档ID、hash、总页数、实际审阅页及不可读页；“全部提取成功”不等于“全部人工/模型读完”。分批阅读可以，但合起来应覆盖完整原文，表格/图片不可读页须渲染核对，不能用关键词检索替代。遇未读页或仅facts测试输入，明确覆盖限制，不宣称完整来源验收。

每个事实按schema包含id、key、value、items、unit、currency、period、basis、status、evidence；证据带document_id、PDF一基页码和短原文。`supported`只表明机械证据存在，不代表独立语义verified。核查必须绑定同一实体、指标、年度/期间、单位、币种、调整口径和表头，不能因某数字在全书任意位置出现就通过。保荐人须全称全名单，不混承销商；基石须完整实体清单，不拿一页局部名单当全部。

`unresolved`是尚未解决；`not_disclosed`须有足够原文阅读依据；`not_applicable`须说明原因。缺口写进gaps，不能把未定位的绿鞋/基石写“无”，不能把预期时间写已发生。固定价不得写“上限”，条件回拨不得写成实际回拨；分配后版本替代初始结构须留文档版本关系。计算单列calculations，输入fact_ids、运算、结果与口径可复算；跨币种先有同一时点汇率证据。

JSON bundle包含契约规定的event_id、kind、sources_seen、facts、calculations、article（title、blocks）、gaps、author_inputs_used。blocks用role/text/fact_ids；无证据的用户仓位、中签、交易和心情不进入author_inputs_used。核心字段逐项有状态，HARD_FIELDS须正向支持方可进入发送验收；其余未解决项由项目闸门裁决，作者不能以一段免责声明代替缺失事实。

来源附录单独输出，包含字段→页码/短引/计算与缺口，不粘进公众号正文。另进程审核输出REVIEW_SCHEMA，从完整原文重新核查，不读取作者自评通过结论。冻结bundle及其hash后生成Word、邮件、公众号富文本；三份须同源，改一处须重冻结并重新审核。skill本身不发送。外部执行器负责附件完整、渠道授权、幂等键和回执；文件存在、WPS打开成功或命令返回不等于发送/云同步成功。

机器计算单位使用严格协议：同单位同币种除法输出 `""`、`ratio`、`倍`、`概率` 为原始比值（概率为 fraction），输出 `%` 时结果乘100；同口径由独立审阅确认。股价乘每手股数只允许两个输入 `per_share`（非空币种）×`shares_per_lot`（空币种）→`per_lot`（原币种），可交换输入次序；不要使用自由文本单位推断。其它多维乘法仍拒绝。舍入结果的 basis 必须有 `round_half_up:N`（如 `round_half_up:7`）；正文百分数可自然显示，不必展示机器值全部精度。

任务参数、历史回放范围和实际阅读过程不是PDF事实，走 gaps / sources_seen / author_inputs_used 或 disclaimer，不可放入 `status=supported, evidence=[]` 的 facts。author_inputs_used 仍须是明确作者输入；原文事实始终需要引用，来源审计与任务元数据没有无证据 supported 豁免。


v3.0补充：以上精确值、完整名单和条件属于后台核验范围，不能推导成正文逐项复述义务。正文显示最多两位小数，按所用单位正确四舍五入即合格；底稿继续保留精确输入与中间值。核验员须区分真正误导结论的省略与正常编辑取舍，不因正文只写绿鞋有/无、只选择重点财务、未列全部标准条件而认定遗漏。涉及盈利质量的因果判断须有据，但单列实际利润不自动触发完整会计解释义务。
