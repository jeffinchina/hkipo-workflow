# 窄规则自动分类接入

`classification.py`只读本地HTML/JSON/原PDF，不联网、不调用模型，不修改worker/store核心。目的为常见普通IPO自动建立已证实分类；规则不足的材料仍待核，不能以模型猜发行类型。

```python
evidence = aggregate_evidence(saved_official_html, official_main_or_gem_url)
evidence += title_evidence(saved_official_title_json)
decision = classify_offer(events, {event_id: local_verified_pdf_path}, evidence)
# 先把decision审计JSON留在私有研究目录，再在confirmed时写入既有分类表：
apply_classification(store, decision, promote_candidates=True)
worker.run_one()  # 根集成层调用，不在分类模块内部触发
```

`aggregate_evidence`仅接受已验证www2主板/GEM新上市路径，从实际table列标识解析announcement/prospectus/allotment，完整保留每格多链接、URL、5位代码、原名、源HTML SHA及行列/列标题。不能用普通网页随处出现“招股”就当新上市。`title_evidence`仅用原始LONG_TEXT官方headline类别分流，保留NEWS_ID及源JSON SHA，不把含“上市文件”的衍生权证标题误当IPO；仅明确上市文件-发售以供认购、新上市公告、官方15200叶节点“公告及通告 - [正式通告]”/“Announcements and Notices - [Formal Notice]”、配发结果和明确全球发售延期等类别可给角色证据。正式通告采用完整类别匹配，只提供announcement角色；标题中的Formal Notice/全球发售不能代替官方类别，也不能单独确认IPO，仍须PDF封面、代码、发行人及同日公告+招股书双文档确证。

`inspect_cover(path,code)`只看原PDF第1—3页：带标签股份代号须唯一匹配、公司名称与紧邻注册成立段、全球发售/发售股份标识、申请程序/申请缴款等条款。公告还须“本公告并非招股章程”；招股书须重要提示及送呈注册处/342C等前言。AP/PHIP、基金/ETF/REIT、供股、衍生权证及牛熊证封面类型明确排除；不使用股票代码区间或名称像某行业的启发式。不在招股书全文随机搜关键词证明发行类型。

`classify_offer(events,pdf_paths,evidence)`必须同时满足：官方明确announcement+prospectus角色；两份实际PDF SHA不同且前页角色相符；发行人法律名称及带标签代码一致；招股书独立日期行与官方公开日期相符；公告与招股书属同一公开日期。通过时返回decision=confirmed、assignments、issuer、stock_code、prospectus_public_date、episode_id及audit。episode_id由代码+招股书公开日+已证实法律名称hash组成，不只代码。不同日补充材料或原日期不清时保守留待核，不静默归入旧期。

`apply_classification(store,decision,promote_candidates=False)`仅confirmed时调用既有confirm_classification，返回写入数量。不触发writer。若明确要让已证实baseline=2候选回补排队，可传promote_candidates=True；不批量重放baseline=1历史。

`classify_followup(event,path,evidence,confirmed_offer)`只接已确证offer decision，要求相同发行人/代码、官方allotment/exception角色、前3页相应行动标题、精确引用原招股日期且日期邻近“招股章程”，并在该期公开日后90天内。没有已确认IPOepisode绝不把普通老股配售/融资公告送去写中签/异常文章。返回assignment沿用原episode_id，kind仍区分体裁。集成层必须持久保存offer审计结果以供后续匹配，不能构造空白自称confirmed的episode代替既有证据。

## 离线验证及证据边界

离线分类测试覆盖以下证据。使用真实主板16链接/GEM空表及当天587文档官方JSON，证明列角色解析及衍生产品/基金/常规公告不被提升。翼菲历史公告+437页招股书原件前3页自动形成一致公司、代码、公开日期及双角色证据，调用store后可合并领取。还覆盖错误代码、角色对调、日期冲突、缺招股书、preliminary、没有原IPOepisode与同仁堂真实延期公告精确回指。

翼菲官方角色使用本机原存2026-05-08聚合快照真实行metadata；fixture明确记录原路径及快照SHA，未伪造为保存的原始HTML。它能验证既有角色证据+真实PDF的组合规则，不能冒充该历史日原HTML抓取端到端验证。同仁堂followup测试以既有已确认episode/角色为前提，验证实际PDF引用绑定，不冒充重新证实该历史官方headline响应。测试无实时网络、无模型、无邮件或草稿。

规则刻意窄：英文日期/特殊封面/超过3页才显示身份或日期/复杂多期同日文件可能待核；当前自动通过实测对象为翼菲，不宣称覆盖所有港股新股。root可记录needs_classification原因作为规则补齐材料，不能悄然降为模型猜测。

新增fast-probe真实JSON回归：`positive-day-new-listings.json`的NEWS_ID12152406（06871，“公告及通告 - [正式通告]”）提供announcement角色，与`positive-day-offer.json`及原PDF双文档组合验证。独立边界测试不依赖原PDF：英文完整叶节点、非IPO正式通告仅有角色而缺PDF仍待核；只有标题含Formal Notice或含糊复合类别不产生announcement证据。英文类别及非IPO标题为显式合成边界样本，不冒充新抓官方响应。
