# 配置与运行

这是从实际工作流整理出的可配置版本。使用源码检出并以 `pip install -e .` 安装；技能、日历及渲染脚本按仓库目录定位。支持 macOS、Linux；常驻服务使用系统的进程管理器。

## 1. 本地配置

运行 `python scripts/configure.py`，生成 `config/*.json`、`private/config.json` 和 `private/draft-profile.json`，已有文件不会被覆盖。这些目录均不提交 Git。

- `config/service.json`：扫描频率、数据目录、技能与写作选项。默认每 60 秒扫描标题，15 分钟检查新上市汇总。
- `config/local.json`：可选 `codex_executable` 绝对路径和 `proxy_url`（仅本机回环代理）；不填则从 PATH 找 Codex 并直连。此文件不放账号密码。
- `config/publisher.json`：Word 验收、邮件与公众号私有草稿队列。
- `config/mobile.json`：IMAP 手机回复、正式通知、写作提醒和草稿更新。
- `private/config.json`：邮件服务器、邮箱应用密码及公众号 appid/appsecret。填写自己的账号；密钥不写入命令、提示词或 README。
- `private/draft-profile.json`：作者、封面永久素材 ID、配色、字号和留言开关。样例作者为空、封面为空、留言开启；真实素材须自行提供。

所有进程启动前设置以下本机环境变量。可放入被忽略的 `.env` 后在自己的 shell 中加载；程序不会自动读取 `.env`。

```bash
export HKIPO_RECIPIENT='your-address@example.invalid' # 换成你的真实邮箱
export HKIPO_QR_PATH="$PWD/private/qr.jpg"             # 自备原始 JPEG
export HKIPO_QR_SHA256="$(python -c 'import hashlib,os; print(hashlib.sha256(open(os.environ["HKIPO_QR_PATH"],"rb").read()).hexdigest())')"
export HKIPO_PROFILE_PATH="$PWD/private/draft-profile.json"
```

缺少收件人或图片及其校验值时，投递或带页脚渲染会报错，不使用任何原作者的个人信息作为兜底。合成测试图片仅供离线测试。

## 2. 扫描与写作

先完成 README 中的只读启动，检查 `data/service/` 中的状态与健康信息。首次扫描只建基线，不自动重放历史新股。

```bash
python -m hkipo --help
python -m hkipo quota --settings config/local.json
python -m hkipo watch --config config/service.json --classify --work --enable-writer
```

写作使用既有 Codex CLI 登录；当前代码默认模型为 `gpt-6.1-sol`，需要所用 CLI/账号支持它。单篇 `write` 可用 `--model` 选择模型。额度读取失败会暂停，不绕过保护切换账号；默认保留 20% 额度，并按实际窗口恢复时间续做。

独立写作与审核以完整 PDF、事实底稿和冻结版本交接。审核失败后依次定点修订、重新核对正式原文，并以已证实的事实收窄表述；每轮重新独立审核，保留较好的检查点。正式证据仍有缺口时，工作进度和下一次检查时间留在本地。港交所资料更新后，相关稿件可自动重新进入审核。

披露易整段查询异常时，先检查独立来源，再按香港日期分段重试。分段中可核实的公告仍可入队；原扫描水位和告警会保留到覆盖缺口补齐。

启用招股提醒时，在 `config/service.json` 填入 `offer_notice_config`（例如 `private/config.json`）和带时区的 `offer_notice_enabled_since`（启用时间）。设置 `HKIPO_RECIPIENT` 后，以 `watch --classify --work --enable-writer` 运行。提醒以港交所正式招股时间表为准，同一发行期只发送一次，写作独立运行。切换前已发现的项目不会自动补发。启用后如因暂时故障错过当天，在已核实的完整认购窗口内可补发并标注原开售日。

## 3. Word 与手机排版验收

安装 LibreOffice 和中文字体。macOS 可使用“冬青黑体简体中文”；Linux 建议安装 Noto Sans CJK，并在 LibreOffice 中确认中文字体替代正常。`HKIPO_SOFFICE` 可指定 LibreOffice 可执行文件。

Word 从同一份内容生成 DOCX，转换为 PDF 和逐页 PNG，再由独立视觉审核检查漏字、裁切、重叠及图片。主渲染入口失效时直接调用本机 LibreOffice 再转成页面图像。有多个本地可执行文件时可尝试不同安装。渲染全不可用时预约后续重试，保留已审核原稿，不超额消耗排版审核次数。全部通过才能冻结。

手机排版工具需另行安装：

```bash
npm install
npx playwright install chromium
```

默认使用 Playwright 的 Chromium；`HKIPO_CHROME_PATH` 可指定本机 Chrome，`HKIPO_NODE` / `HKIPO_PLAYWRIGHT_MODULE` 可覆盖运行路径。几何检查使用本机 HTML，阻止外部页面资源请求。

手机邮件流程还要求 `private/approved-template.json`：由实际模板预览验收产生，包含 `template_sha256` 和 `local_visual_confirmed: true`。指纹通过 `hkipo.mobile_layout.template_fingerprint()` 计算；必须先检查真实模板及公众号草稿呈现，再记录通过。样式、图片或模板变化后需要重新验收，仓库不提供现成的“已通过”凭据。

## 4. 邮件、公众号草稿与手机回复

先保持 `enable_deliveries: false`，检查准备结果。部署验收可用 `acceptance_mode` 和明确的 `accepted_event_ids` 仅处理所选事件；不要批量重放历史记录。真实邮件与草稿链路验收后，再按需启用投递，并设置 publisher 的 `production_accepted: true`。

```bash
python -m hkipo publisher --config config/publisher.json --watch
python -m hkipo mobile-service --config config/mobile.json --watch
```

公众号只创建或更新私有草稿，作者、封面、留言字段实际传入接口；没有自动公开发表步骤。正式配发或异常公告触发通知，作者可以回复关联邮件，在手机上完成后续文字。原始回复保留本地，按已验收模板生成草稿并回执；写作提醒使用经核实的香港工作日日历。

每只新股单独保存一篇私有草稿。同批两只或以上时，保存草稿就区分各篇封面。首篇使用默认封面，后续各篇从已配置的备选封面中使用画面明显不同的图片。作者在手机的公众号助手按需要合并，逐篇确认「声明原创」，并自行预览、发表和群发。

在 `config/publisher.json` 的 `offer_cover_paths` 填入备选 JPEG 或 PNG 本机路径。请选不同画面的图片，按希望使用的顺序排列。程序根据已核实的香港招股日期和招股文件日期避开同批已用的封面，并在重试时保留分配。备选不足时，分析邮件照常发送，私有草稿在下一次尝试前等待备选素材。

邮件连接、登录或公众号令牌获取遇到故障时，已确认尚未发送的渠道会自动退避重试。SMTP/接口写入后超时可能已经送达，回执不确定的渠道进入待核对队列。已确认的渠道保留回执，另一渠道恢复时无需重发。健康信息、投递回执和下次重试时间仅留本地。

## 5. 常驻运行与维护

四个入口分别承担发现与写作、Word 与投递、手机回复、暗盘收盘提醒。应由进程管理器分别托管，避免写作阻塞扫描。macOS 可运行 `python scripts/make_launch_agents.py` 生成本机 plist，输出在被忽略的 `data/deployment/`；该命令不安装或启动服务。生成时 `--enable-writer` 才允许常驻写作，投递仍由配置中的开关决定。

日历在 `runtime/data/hk-calendar/` 中按年份保存公开来源及核验记录；缺少已核实年份会暂停提醒，不能猜下一工作日。更新年份时按该目录的 `SOURCES.md` 复核。

维护检查：

```bash
python scripts/run_tests.py
python scripts/privacy_scan.py
python scripts/privacy_scan.py --staged
```

仓库另附 `examples/github-actions-tests.example.yml`，具备 GitHub Actions 工作流写入权限后，可移至 `.github/workflows/tests.yml` 启用远端检查。当前发布授权不包含该权限，默认执行本地检查及提交前隐私扫描。

回归测试使用合成图片和模拟模型、邮件、接口。13 个历史原文回放测试因不附带私人研究目录而默认跳过；其余测试离线运行。发布前另查暂存内容，不能只靠忽略规则或自动扫描判断所有内容都适宜公开。


## 暗盘收盘点评

运行配置生成器后，填写 `config/grey.json`，确认 `reply_db` 指向手机回复入口使用的同一账本。首次默认禁用；按所用账号完成邮件验收后将 `enabled` 设为 `true`，然后运行：

```bash
python -m hkipo grey-service --config config/grey.json --watch
```

此进程独立于 IMAP 和 PDF/模型工作：只在暗盘收盘后有界采集，通常从18:30:30后开始，固定18:40将同一股票的辉立和富途分别写入一封邮件；半日市仍在18:40提醒。正式配发未公布、核验未完成或某一平台数据缺失都会明确显示，不延期邮件、不拼接两平台高低价。晚到配发继续独立核验入库，不重复当天提醒。

富途使用致富证券官方页面明确标注的富途数据；辉立采用辉立官方日行情，并与 AAStocks 官方页面标注的辉立分时交叉核验。辉立分时为5分钟图表采样，富途为分钟数据，均不伪装成完整逐笔。真实开盘与图表参考锚分开；行情只保留当日，必须当日收盘后取回并本地落档。具体字段及文风见[暗盘点评规范](grey-analysis.md)。

`service.json` 的 `grey_state_db` 允许为当前/未来暗盘证券核实启动时已有的正式招股文件，只建立IPO发行期关联，不重发历史申购文章。正式配发保留原始类型，worker在完整来源与独立复核后产出资料底稿，投递端不把它自动发布成长篇文章。


### QQ 邮件回复关联

部分 IMAP 服务会改写送达邮件的 Message-ID，并对中文主题、BEFORE 或 Message-ID 搜索返回空结果。工作流先按已登记提醒查询；必要时仅在同一账号的已发送邮件中查找有界候选，读取服务器 INTERNALDATE 进一步限制时间范围，再逐项核对精确邮件头、完整正文及附件，建立稳定标识映射。作者回复仍须有同账号已发送副本且正文、附件、关联线程完全一致；不会仅凭发件人或相同主题认定本人回复。

系统发出的已登记提醒和状态回执不会被再次当作作者正文。身份核验后，原始作者文字留档并进入既有排版、私有草稿及回执流程。
