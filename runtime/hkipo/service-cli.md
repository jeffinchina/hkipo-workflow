# 本地单轮命令

在 `工作流重建/runtime` 目录使用 `python3 -m hkipo service`。命令仅执行一次 `LocalService.cycle`，不启动循环、子进程分派、部署或发送。扫描可能访问官方公告网络；分类可能下载官方PDF；writer启用后可能调用模型。

```sh
# 默认仅扫描，首次建立基线
python3 -m hkipo service --data-dir /absolute/private/service-data
# 仅分类已存事件和快照，不扫描
python3 -m hkipo service --data-dir /absolute/private/service-data --classify
# 扫描并分类，仍禁writer
python3 -m hkipo service --data-dir /absolute/private/service-data --scan --classify
# 单轮有界研究：必须同时明确选择work及允许writer
python3 -m hkipo service --config /absolute/service-settings.json --classify --work --enable-writer
```

`--config` 是 `ServiceSettings` JSON（`--settings` 仅为兼容别名），不同于既有 `write --settings`、`quota --settings` 使用的网络配置。最小内容为 `{"data_dir":"/absolute/private/service-data"}`；支持ServiceSettings既有字段（批次数、skill_paths、author_inputs、writer_options等）。writer网络配置须放在 `writer_options.settings_path`。`--data-dir` 覆盖JSON中的目录。JSON中的 `enable_writer` 始终由本次 `--enable-writer` 决定；只写配置不能启用模型。`--enable-writer` 必须和 `--work` 同用；单独 `--work` 返回disabled及退出码2。

任意指定 `--scan`、`--classify`、`--work` 后，仅执行选定阶段；均不指定时只scan。目录内数据库固定为 `state.sqlite3`，当前LocalService没有自定义数据库路径API；本入口没有 `--db`。原有 `scan --db`、`write`、`quota`、`validate` 参数保持兼容。旧scan与service的审计和周期语义不同，不应把旧数据库随意搬进服务目录后视为已完成迁移。

标准输出为原cycle JSON（包括health）；配置错误用argparse报错，避免回显配置内容。退出码：0本轮无检测到的故障；2参数错误、work禁用或worker存在非ready结果；3扫描失败或health异常；4锁忙。无任务时work结果为空，退出0不表示已经产生文章；ready_for_delivery也只代表既有worker状态，不代表已经外发。持久health可能包含上轮未清除的错误，应结合各阶段结果查看。

验证使用fake LocalService、fake Monitor和fake writer，不实际扫描、下载、调用模型或发送。真实常驻稳定性、吞吐和生产网络行为不在该CLI验证范围内。

完整配置示例（示例路径，不含私人配置值）；保存为独立的service-settings.json，不能直接拿网络local-settings.json充当服务配置：

```json
{
  "data_dir": "/absolute/private/service-data",
  "enable_writer": false,
  "skill_paths": {
    "offer": "/absolute/project/工作流重建/skills/hkipo-editor"
  },
  "writer_options": {
    "settings_path": "/absolute/project/工作流重建/runtime/local-settings.json"
  }
}
```

将示例路径换成已有本地路径。这里仅向writer透传网络配置文件路径，不复制其中的值，也不为scan/分类配置代理。即使JSON改成enable_writer=true，仍须在本次命令提供`--work --enable-writer`才允许写作。

## 前台watch入口

watch复用同一ServiceSettings配置，默认仅持续扫描；只有本次显式`--classify`才调用run_loop的dispatch=True，派发独立pipeline。JSON中的enable_writer不能绕过命令行开关。watch的`--work`必须同时有`--classify --enable-writer`；`--enable-writer`也必须有`--work`，否则参数错误且不构造LocalService。

```sh
# 以下为操作示例，本次没有实际启动watch
python3 -m hkipo watch --config /absolute/service-settings.json
python3 -m hkipo watch --config /absolute/service-settings.json --classify --max-cycles 1
python3 -m hkipo watch --config /absolute/service-settings.json --classify --work --enable-writer
```

`--max-cycles`仅接受正整数；不填则持续到停止信号。这里的次数是发现周期数，受扫描频率/聚合频率及已有水位影响，并不保证每次都重新抓取所有源。pipeline每轮受既有max_classification_groups/max_worker_groups限制；上一子进程仍存活时不重复启动，跨进程也有pipeline锁。

SIGINT（Ctrl-C）及SIGTERM只设置stop_event：允许进行中的扫描结束，停止下一轮发现。命令退出前恢复原信号处理器；没有强杀已经进入写作的子进程。派发的子进程不是daemon，Python进程退出时可能等候它完成；max-cycles也不保证整个父进程立即退出。没有即时取消模型调用的承诺。异常时恢复信号处理器后继续抛出原异常，按Python非零退出；不会把异常改成成功JSON。

正常停止输出`{"status":"stopped","cycles":N}`并退出0；独占monitor-loop锁忙时输出already_running并退出4；参数错误退出2。watch的0只表示发现循环停止，run_loop当前不返回逐轮health/分类/writer最终结果，因此不表示抓取无故障、文章ready或子进程成功。部署前及停止后应读取data_dir/health.json与各阶段记录；需要阶段退出码的验收使用单轮service入口。此处只接现有run_loop，没有扩展其健康汇总API。

## 部署前检查（本次未部署）

- 复制runtime/service-settings.example.json到独立配置文件并替换示例绝对路径；data_dir须是可写私有目录，数据库固定state.sqlite3。三种skill_paths均指向技能目录，目录内应存在SKILL.md及references；不能填SKILL.md文件路径。
- writer_options.settings_path指向已有local-settings.json，只给writer透传；检查该文件实际存在，勿把其私密内容写入示例或日志。先保持enable_writer=false且命令行不带writer开关。
- 先完成fake测试；若获准真实运行，先用service单轮检查来源、首次基线、响应留证、health及分类，再决定是否启动watch。首次基线不会自动补写历史事件。
- 开启真实writer前检查技能、作者输入、材料和独立视觉复核门，并由主控确认额度；watch没有新增自动读额度或到阈值停止机制。最终以既有writer/质量门结果为准。
- 本次没有安装launchd、写入~/Library、启动真实watch或测试生产网络；系统级守护、崩溃重启、信号下真实长请求行为及常驻吞吐仍待获准验证。

watch验收全部使用fake LocalService.run_loop和fake signal注册：覆盖默认dispatch=False、显式分类/写作、非法组合、正整数限制、信号设置停止事件、异常/正常恢复处理器和锁忙退出码，不实际启动线程循环或子进程。
