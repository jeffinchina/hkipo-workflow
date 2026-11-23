# 富途公众发现 → 持久候选有界接口

本单元不接 service/CLI/mobile，不发送邮件、不写稿。data_scope 固定为 `broker_customer_allotment`，不能推算全市场中签率或稳中。

运行独立匿名发现（bundled Node 与 Playwright 环境）：

```sh
node runtime/scripts/futu_discovery.mjs /tmp/futu-snapshot.json 3
```

最多1至20帖；允许导航作者profile12147652与数字feed。仅公开页，结束关闭整个独立browser。网页静态初始JSON仅提取当前detail，核feedCommon.feedId与当前URL一致、authorInfo.userId=12147652。发布时间取同一feedCommon.timestamp。页面滚动后提取完整公开正文和结构化orgPic/bigPic原始URL，跳过含imageLink的推广图；不保存登录态、cookie或token。Vue hydration后的运行时对象有循环引用，故从页面内原始script JSON读取。

```python
from hkipo.broker_source import ingest_snapshot
result = ingest_snapshot(snapshot, '/path/broker.sqlite', '/path/snapshots')
```

成功且非空、通过schema/身份校验才更新SQLite水位；失败/空列表仍按sha256落盘证据但不更新。首轮所有帖子建立静默baseline。后续新feed_id或新实质内容hash各有独立版本，重复输入不重建候选。实质内容含标题、正文、时间与原图集，不含抓取时间；图片顺序不影响hash。单股仍标needs_review、notification_eligible=False；多股needs_identity_mapping，禁止自行按出现顺序分图。股票关联仅来自“已经公布中签/已經公佈中籤”前主句，预告中的06935排除。

验证：7个unittest全过，Node语法通过。真实新脚本最多1帖smoke抓到09941/117647855976452，原始PNG及/big URL，无webp重编码参数；ingest结果baseline、0候选。研究证据 `research/sources/early-broker-probe/discovery/runtime-adapter-smoke.json`，落盘hash `ed577c2debfcab1f065a5661d6e0309351942e24386916e5c6bac2f89c25ce11`。本单元未证明早于港交所公告，也未接原图下载与完整市场配发表解析。

并发与恢复：schema初始化后BEGIN IMMEDIATE再读baseline/latest；队列broker_review_queue带自增revision_id和pending状态，持久保存新帖与实质修订，消费者可恢复读取。A→B→A产生两个修订队列项，内容版本表复用A；重复A不入队。归档临时文件使用唯一UUID。body取当前detail.feedFlattenText（原帖正文），不取DOM阅读数、评论或互动数。
