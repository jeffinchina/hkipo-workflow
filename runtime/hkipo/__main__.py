import argparse
import json
from pathlib import Path


def _positive_int(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("必须为正整数")
    return number


def _service_settings(args, parser):
    from .service import ServiceSettings
    try:
        settings = json.loads(Path(args.config).read_text()) if args.config else {}
        if not isinstance(settings, dict):
            raise ValueError("settings must be a JSON object")
        if args.data_dir:
            settings["data_dir"] = args.data_dir
        # Persistent configuration cannot silently enable model work.
        settings["enable_writer"] = args.enable_writer
        return ServiceSettings(**settings)
    except (OSError, ValueError, TypeError):
        parser.error("需要有效ServiceSettings JSON及data_dir（可用--data-dir覆盖）；请检查本地配置")


def _watch(service, max_cycles, dispatch):
    import signal
    import threading
    stop = threading.Event()
    previous = {}
    try:
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous[signum] = signal.signal(signum, lambda _signum, _frame: stop.set())
        if dispatch is None:
            return service.run_loop(stop,max_cycles=max_cycles)
        return service.run_loop(stop, max_cycles=max_cycles, dispatch=dispatch)
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def main(argv=None):
    parser = argparse.ArgumentParser(description="港股公告监控与可审计文章工作流")
    commands = parser.add_subparsers(dest="command", required=True)
    q = commands.add_parser("quota", help="只读实时额度，不调用模型")
    q.add_argument("--settings")
    s = commands.add_parser("scan", help="单次官方公告只读扫描，首次仅建基线")
    s.add_argument("--db", required=True)
    s.add_argument("--include-gem-aggregate", action="store_true")
    service = commands.add_parser("service", help="本地有界单轮周期，不启动调度或外发")
    service.add_argument("--data-dir", help="服务目录；数据库固定为其中的state.sqlite3")
    service.add_argument("--config", "--settings", dest="config", help="ServiceSettings JSON（--settings为兼容别名）；不是writer网络配置")
    service.add_argument("--scan", action="store_true", help="执行扫描；未指定阶段时默认扫描")
    service.add_argument("--classify", action="store_true", help="下载并分类，可能访问官方网络")
    service.add_argument("--work", action="store_true", help="运行有界worker；默认禁用写作")
    service.add_argument("--enable-writer", action="store_true", help="明确允许本轮work调用writer（不发送）")
    watch = commands.add_parser("watch", help="前台持续扫描；信号停止发现，不安装调度或外发")
    watch.add_argument("--data-dir", help="服务目录；数据库固定为其中的state.sqlite3")
    watch.add_argument("--config", "--settings", dest="config", help="ServiceSettings JSON；不是writer网络配置")
    watch.add_argument("--classify", action="store_true", help="显式派发独立分类pipeline")
    watch.add_argument("--work", action="store_true", help="需同时指定--classify和--enable-writer")
    watch.add_argument("--enable-writer", action="store_true", help="明确允许pipeline写作，不发送")
    watch.add_argument("--max-cycles", type=_positive_int, help="正整数；完成指定发现周期后退出")
    w = commands.add_parser("write", help="读取本地完整PDF，写作及独立复核；不发送")
    w.add_argument("--event", required=True)
    w.add_argument("--pdf", action="append", required=True)
    w.add_argument("--out", required=True)
    w.add_argument("--skill", default=str(Path(__file__).resolve().parents[2] / "skills/hkipo-editor"))
    w.add_argument("--author-inputs")
    w.add_argument("--model", default="gpt-6.1-sol")
    w.add_argument("--effort", default="medium", choices=["low", "medium", "high", "xhigh", "max", "ultra"])
    w.add_argument("--review-effort", default="high", choices=["low", "medium", "high", "xhigh", "max", "ultra"])
    w.add_argument("--max-revisions", type=int, default=6)
    w.add_argument("--no-docx", action="store_true")
    w.add_argument("--visual-registry", help="独立视觉审核注册文件，不能由文章模型自报")
    w.add_argument("--auto-scope-review", action="store_true", help="显式启用一次独立范围依赖审核；不创建source scope")
    w.add_argument('--auto-visual-review', action='store_true', help='缺少图片证据时先执行有界独立审图，再写作')
    w.add_argument('--visual-effort', default='low', choices=['low','medium'])
    w.add_argument('--visual-max-batches', type=_positive_int, default=32)
    w.add_argument('--source-mode', choices=['text_first','legacy_strict'], default='text_first')
    w.add_argument("--scoped-visual-registry")
    w.add_argument("--scope-approval-registry")
    f = commands.add_parser("finalize-scoped", help="独立范围审批后无模型最终验证及渲染")
    f.add_argument("--out", required=True)
    f.add_argument("--scope-approval-registry", required=True)
    f.add_argument("--request-id", required=True)
    w.add_argument("--settings", help="仅CLI/quota子进程的本项目网络配置")
    v = commands.add_parser("validate", help="重读PDF，验证冻结稿和独立审核")
    v.add_argument("--bundle", required=True)
    v.add_argument("--sources", required=True)
    v.add_argument("--review")
    m = commands.add_parser('mobile', help='手机回复邮件入口；默认只读，不公开发表')
    m.add_argument('action', choices=['register','poll','reminder','preview','draft','receipt'])
    m.add_argument('--db', required=True)
    m.add_argument('--private-config', required=True, help='显式读取既有私有email/wechat配置，勿放入日志')
    m.add_argument('--event', help='已复核来源事件JSON，仅register需要')
    m.add_argument('--event-id')
    m.add_argument('--raw-hash')
    m.add_argument('--out')
    m.add_argument('--thumb-media-id')
    m.add_argument('--cover-path', help='只能选择本次已复核的原始分配图')
    m.add_argument('--authorize-cover', action='store_true')
    m.add_argument('--layout-approval')
    m.add_argument('--authorize-send', action='store_true', help='只在具备对应外发授权时使用')
    ms=commands.add_parser('mobile-service',help='本机独立手机邮件worker，配置默认不外发')
    ms.add_argument('--config',required=True)
    ms.add_argument('--watch',action='store_true')
    ms.add_argument('--max-cycles',type=_positive_int)
    pub=commands.add_parser('publisher',help='本地Word视觉验收与邮件/私有草稿worker，默认不投递')
    pub.add_argument('--config',required=True)
    pub.add_argument('--watch',action='store_true')
    pub.add_argument('--max-cycles',type=_positive_int)
    args = parser.parse_args(argv)
    if args.command == 'publisher':
        from .publisher import Publisher,PublisherSettings
        try:
            settings=PublisherSettings(**json.loads(Path(args.config).read_text()))
        except (OSError,ValueError,TypeError):
            parser.error('需要有效的PublisherSettings本地JSON配置')
        instance=Publisher(settings)
        result=_watch(instance,args.max_cycles,None) if args.watch else instance.cycle()
    elif args.command == 'mobile-service':
        from .mobile_service import MobileSettings,MobileService
        try:
            settings=MobileSettings(**json.loads(Path(args.config).read_text()))
        except (OSError,ValueError,TypeError):
            parser.error('需要有效的MobileSettings本地JSON配置')
        instance=MobileService(settings)
        result=_watch(instance,args.max_cycles,None) if args.watch else instance.cycle()
    elif args.command == 'mobile':
        from .mobile_cli import run
        result = run(args)
    elif args.command == "quota":
        from .quota import read_limits, evaluate_limits
        result = evaluate_limits(read_limits(settings_path=args.settings))
    elif args.command == "finalize-scoped":
        from .writer import finalize_scoped_job
        result = finalize_scoped_job(args.out, args.scope_approval_registry, args.request_id)
    elif args.command == "scan":
        from .store import StateStore
        from .sources import TitleSearchSource, AggregateSource
        from .monitor import Monitor
        sources = [TitleSearchSource(), AggregateSource("Main-Board")]
        if args.include_gem_aggregate:
            sources.append(AggregateSource("GEM"))
        result = Monitor(StateStore(args.db), sources).scan()
    elif args.command in {"service", "watch"}:
        from .service import LocalService
        settings = _service_settings(args, parser)
        if args.enable_writer and not args.work:
            parser.error("--enable-writer必须与--work一起使用")
        if args.command == "watch":
            if args.work and not (args.classify and args.enable_writer):
                parser.error("watch --work必须同时指定--classify和--enable-writer")
            result = _watch(LocalService(settings), args.max_cycles, args.classify)
        else:
            scan = args.scan or not (args.classify or args.work)
            result = LocalService(settings).cycle(scan=scan, classify=args.classify, work=args.work)
    elif args.command == "write":
        from .writer import write_article
        event = json.loads(Path(args.event).read_text())
        author = json.loads(Path(args.author_inputs).read_text()) if args.author_inputs else {}
        result = write_article(event, args.pdf, args.out, args.skill, author,
                               args.model, args.effort, args.review_effort,
                               not args.no_docx, args.max_revisions, args.visual_registry, args.settings,
                               scoped_visual_registry=args.scoped_visual_registry, scope_approval_registry=args.scope_approval_registry,
                               auto_scope_review=args.auto_scope_review,auto_visual_review=args.auto_visual_review,
                               visual_effort=args.visual_effort,visual_max_batches=args.visual_max_batches,source_mode=args.source_mode)
    else:
        from .quality import validate_bundle
        read = lambda p: json.loads(Path(p).read_text())
        result = validate_bundle(read(args.bundle), read(args.sources), read(args.review) if args.review else None)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if args.command == 'mobile' and result.get('status') in {'blocked','invalid_arguments','needs_reconciliation'}:
        return 2
    if args.command in {'mobile-service','publisher'}:
        state=result.get('last',result)
        if state.get('status') in {'failed','blocked'}:return 3
    if (args.command == "validate" and not result["pass"]) or (args.command == "write" and result["status"] != "ready_for_delivery"):
        return 2
    if args.command == "scan" and not result["ok"]:
        return 3
    if args.command == "watch" and result.get("status") == "already_running":
        return 4
    if args.command == "service":
        if any(result.get(part, {}).get("status") == "busy" for part in ("scan", "pipeline")):
            return 4
        if not result.get("health", {}).get("ok", False) or result.get("scan", {}).get("ok") is False:
            return 3
        work = result.get("work", {})
        if work.get("status") == "disabled" or any(item.get("status") not in {'ready_for_delivery','material_only'} for item in work.get("results", [])):
            return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
