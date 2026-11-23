# 交付模块契约

`Deliverer(store, config_path=显式私有JSON路径)`；测试可传`config=内存字典,smtp_sender=fake,wechat_client=fake`。不隐式读原配置、不读.wx_token.json、不写token缓存。实际发送为调用者的显式操作，无import副作用。

- `email(release,recipient='recipient@example.invalid')`：同bundle文章纯文本+HTML正文，强制article.docx附件。配置email含smtp_host/smtp_port/user/password。
- `draft(release,thumb_media_id=None,cover_path=None,cover_authorized=False,update_existing=False)`：只草稿，配置wechat含appid/appsecret。必须显式封面永久素材ID，或经授权本地cover；否则needs_cover、完全不请求API。正文现由render产生纯文字HTML，无自动旧图。已有同event/account confirmed草稿且新payload不同，必须update_existing=True才能draft/update原media_id；未开启时返回update_requires_explicit_request。
- 返回status：confirmed / already_confirmed / needs_reconciliation / needs_cover / update_requires_explicit_request。预检失败抛仅含安全码的DeliveryError。format_ui_verified永远False，API回执不能证明平台最终排版。

release是dict或JSON路径，必须含event_id/bundle_path/review_path/quality_path/output_dir/files_sha256/sourcesmanifest_path/bundle_sha256/review_sha256/visual_qa_path。sourcesmanifest_path是prepare_documents生成的完整PDF manifest；files_sha256为output_dir下basename->SHA256，必须包含article.txt、article.html、article.docx。visual_qa JSON必须confirmed:true及docx_sha256等于冻结Word。

发送前重读bundle/review与全部输出hash，重跑validate_bundle(真实PDF/hash/引用及独立review)，要求存量quality和实时quality均pass；文章txt/html必须等于由bundle重渲染的正文，禁止将完成通知冒充文章。数据读取后被冻结在内存再发，避免读后文件改动影响本次payload。

key=event/channel/recipient-or-account/payload的SHA256；payload包括标题、正文、邮件Word hash或草稿封面hash/ID。原子BEGIN IMMEDIATE在账本写unknown前还检查同event/channel任何未决unknown，防通过更换正文绕过。任何transport后错误均保留unknown，不自动重试；先对账再由人决定恢复。成功收据只记Message-ID或media_id、payload/account hash。SMTP服务接受不等于收件人最终收取，成功后落盘前崩溃仍可能不确定，不承诺exactly-once。

官方文档于2026-11-19只读curl核验（旧offiaccount路径404，新service路径有效）：

- https://developers.weixin.qq.com/doc/service/api/draftbox/draftmanage/api_draft_add.html ：draft/add、永久封面素材、正文限制。
- https://developers.weixin.qq.com/doc/service/api/draftbox/draftmanage/api_draft_update.html ：media_id/index/articles object，返回errcode。
- https://developers.weixin.qq.com/doc/service/api/base/api_getaccesstoken.html ：GET token及appid/secret/grant_type，内存使用。
- https://developers.weixin.qq.com/doc/service/api/material/permanent/api_addmaterial.html ：永久图片素材media_id。
- https://developers.weixin.qq.com/doc/service/api/material/permanent/api_uploadimage.html ：media/uploadimg，正文图片URL；仅PNG/JPG且<1MB。

WeChatClient允许以上5种API路径，显式拒绝其他接口（含发布）；禁重定向和写入重试。transport可注入requester(method,url,body,headers)->dict。异常不会回显带access_token的URL。12项纯fake测试通过，未读取真实私有配置，未发邮件/请求账号API/创建草稿。

草稿版本回退A→B→A按当前最新confirmed payload判断；显式更新仍draft/update同media_id，不会被历史A投递key误当成已是当前内容。
