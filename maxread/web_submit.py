from __future__ import annotations

import hashlib
import secrets
import uuid

from .db import Store
from .sources import extract_supported_inputs


WEB_SESSION_COOKIE = "maxread_web_session"
WEB_SESSION_BYTES = 32
WEB_BINDING_TTL_MINUTES = 10
WEB_SUBMISSION_LIMIT = 5
WEB_RATE_LIMIT = 10


def session_hash(token: str) -> str:
    return hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()


def new_web_identity(store: Store, token: str = ""):
    session_token = str(token or "").strip() or secrets.token_urlsafe(WEB_SESSION_BYTES)
    digest = session_hash(session_token)
    identity = store.get_web_identity(digest)
    identity = store.get_or_create_web_identity(
        digest,
        "" if identity else f"web_{secrets.token_hex(6)}",
    )
    return session_token, identity


def web_identity_payload(identity) -> dict:
    bound = bool(str(identity.get("feishu_open_id") or ""))
    return {
        "public_id": str(identity.get("public_id") or ""),
        "account_type": "feishu" if bound else "guest",
        "display_name": str(identity.get("display_name") or ("飞书用户" if bound else "游客")),
        "bound": bound,
        "acting_as": str(identity.get("_actor_type") or "") == "admin",
    }


def issue_binding_code(store: Store, identity) -> dict:
    code = f"{secrets.randbelow(1_000_000):06d}"
    store.issue_web_binding_code(
        int(identity["id"]),
        hashlib.sha256(code.encode("ascii")).hexdigest(),
        WEB_BINDING_TTL_MINUTES,
    )
    return {
        "code": code,
        "command": f"绑定 {code}",
        "expires_in_seconds": WEB_BINDING_TTL_MINUTES * 60,
    }


def claim_binding_code(store: Store, code: str, feishu_open_id: str):
    clean = str(code or "").strip()
    if len(clean) != 6 or not clean.isdigit():
        return None
    return store.claim_web_binding_code(
        hashlib.sha256(clean.encode("ascii")).hexdigest(),
        feishu_open_id,
    )


def submit_web_papers(settings, store: Store, identity, content: str) -> dict:
    text = str(content or "").strip()
    if not text:
        raise ValueError("请粘贴 arXiv 链接或论文 ID")
    if len(text) > 8_000:
        raise ValueError("提交内容过长")
    paper_refs, web_refs = extract_supported_inputs(text)
    if web_refs and not paper_refs:
        raise ValueError("当前网页入口只接受 arXiv、HuggingFace Papers 或 papers.cool 论文链接")
    if not paper_refs:
        raise ValueError("没有识别到 arXiv 论文")
    if len(paper_refs) > WEB_SUBMISSION_LIMIT:
        raise ValueError(f"一次最多提交 {WEB_SUBMISSION_LIMIT} 篇")
    if store.recent_web_submission_count(identity["public_id"], 10) >= WEB_RATE_LIMIT:
        raise ValueError("提交过于频繁，请稍后再试")

    sender_id = store.web_identity_sender(identity)
    chat_id = f"web:{identity['public_id']}"
    conversation = store.ensure_web_conversation(identity)
    user_message_id = f"web-message:{uuid.uuid4().hex}"
    store.append_web_message(
        int(conversation["id"]),
        user_message_id,
        "user",
        text,
        kind="submission",
        channel="web",
        actor_type=str(identity.get("_actor_type") or "user"),
        actor_id=str(identity.get("_actor_id") or sender_id),
    )
    service_status = store.get_service_status()
    service_available = service_status["mode"] == "operational"
    event_id = f"web-event:{uuid.uuid4().hex}"
    items = []
    for ref in paper_refs:
        message_id = user_message_id
        record = store.get_paper(ref.paper_id)
        usage_id = store.add_usage_event(
            event_id,
            message_id,
            chat_id,
            "web",
            sender_id,
            "paper",
            ref.paper_id,
            ref.url,
            title=record.title if record else "",
            status="queued",
        )
        if record and record.status == "done" and record.doc_url:
            store.update_usage_event(usage_id, "done", doc_url=record.doc_url, title=record.title)
            items.append({
                "paper_id": ref.paper_id,
                "usage_id": usage_id,
                "status": "done",
                "cached": True,
                "doc_url": record.doc_url,
                "title": record.title,
            })
            store.append_web_message(
                int(conversation["id"]),
                f"web-result:cache:{usage_id}",
                "assistant",
                f"这篇已有可用文档：{record.title or ref.paper_id}",
                kind="result",
                source_id=ref.paper_id,
                doc_url=record.doc_url,
                status="done",
                channel="system",
                actor_type="system",
            )
            continue
        queued = store.enqueue_job(
            "paper",
            ref.paper_id,
            ref.url,
            event_id,
            message_id,
            chat_id,
            "web",
            sender_id,
            usage_id,
            suppress_progress_notifications=False,
        )
        if not queued["created"]:
            store.update_usage_event(usage_id, "watching")
        position = store.queue_position(int(queued["job_id"]))
        duration = store.recent_job_duration_seconds("paper")
        worker_count = max(1, int(settings.queue_workers))
        batch = max(1, (max(1, position) - 1) // worker_count + 1)
        items.append({
            "paper_id": ref.paper_id,
            "usage_id": usage_id,
            "job_id": int(queued["job_id"]),
            "status": "queued" if service_available else "waiting_for_service",
            "cached": False,
            "queue_position": position,
            "estimated_wait_seconds": max(0, batch - 1) * duration,
            "estimated_total_seconds": batch * duration,
        })
        store.update_web_job_progress(
            {"chat_type": "web", "message_id": user_message_id, "sender_id": sender_id},
            int(queued["job_id"]),
            ref.paper_id,
            (
                f"已加入队列，第 {max(1, position)} 位。"
                f"预计等待 {max(0, batch - 1) * duration // 60} 分钟，"
                f"预计完成约 {max(1, batch * duration // 60)} 分钟。"
            ),
            "queued" if service_available else "waiting_for_service",
        )
    return {
        "ok": True,
        "items": items,
        "service": service_status,
    }


WEB_SUBMIT_HTML = r'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>MaxRead</title>
  <style>
    :root{--bg:#f7f8f6;--paper:#fff;--line:#dde1dc;--line-soft:#eceeea;--text:#202521;--muted:#6f766f;--green:#236854;--green-soft:#e8f1ed;--orange:#bd6c2d;--red:#b23a32;--shadow:0 16px 40px rgba(32,37,33,.07)}
    *{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Helvetica Neue",sans-serif;font-size:15px;line-height:1.6;letter-spacing:0}
    button,textarea{font:inherit;letter-spacing:0}button{cursor:pointer}.shell{width:min(900px,calc(100% - 32px));margin:0 auto;padding:28px 0 64px}
    header{display:flex;align-items:center;justify-content:space-between;padding:0 2px 24px;border-bottom:1px solid var(--line)}.brand{display:flex;align-items:center;gap:11px}.brand-mark{width:36px;height:36px;display:grid;place-items:center;background:#1e2521;color:#fff;border-radius:7px}.brand-mark svg,.icon svg{width:19px;height:19px;fill:none;stroke:currentColor;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}.brand strong{display:block;font-size:17px}.brand span{display:block;color:var(--muted);font-size:12px}
    .account{height:38px;display:flex;align-items:center;gap:8px;padding:0 11px;border:1px solid var(--line);border-radius:7px;background:var(--paper);color:var(--text)}.account .dot{width:7px;height:7px;border-radius:50%;background:#aeb5ae}.account.bound .dot{background:var(--green)}
    main{padding-top:44px}.intro{margin-bottom:22px}.intro h1{font-size:30px;line-height:1.25;margin:0 0 8px;font-weight:720}.intro p{margin:0;color:var(--muted)}
    .submit-tool{background:var(--paper);border:1px solid var(--line);border-radius:8px;box-shadow:var(--shadow);overflow:hidden}.input-label{display:flex;align-items:center;gap:8px;padding:14px 16px 0;font-weight:650}.input-label .icon{display:flex;color:var(--green)}textarea{display:block;width:100%;min-height:150px;padding:12px 16px 18px;border:0;outline:0;resize:vertical;color:var(--text);background:transparent;line-height:1.65}textarea::placeholder{color:#a3aaa3}.submit-bar{min-height:58px;display:flex;align-items:center;justify-content:space-between;gap:12px;padding:10px 12px 10px 16px;border-top:1px solid var(--line-soft);background:#fbfcfa}.detected{color:var(--muted);font-size:13px}.submit-button{height:38px;display:inline-flex;align-items:center;gap:8px;padding:0 15px;border:1px solid var(--green);border-radius:7px;background:var(--green);color:#fff;font-weight:650}.submit-button:disabled{cursor:not-allowed;opacity:.55}.submit-button .icon{display:flex}
    .service-note{display:none;margin-top:12px;padding:10px 12px;border:1px solid #ead9c9;border-radius:7px;background:#fff8f1;color:#8c5428;font-size:13px}.service-note.visible{display:block}
    .section-head{display:flex;align-items:center;justify-content:space-between;margin:44px 0 12px}.section-head h2{font-size:18px;margin:0}.section-head button{width:36px;height:36px;display:grid;place-items:center;border:1px solid var(--line);border-radius:7px;background:var(--paper);color:var(--muted)}
    .conversation{display:grid;gap:16px;padding:4px 0 10px;border-top:1px solid var(--line)}.message{max-width:78%;padding-top:16px}.message.user{justify-self:end}.message.assistant{justify-self:start}.bubble{padding:11px 13px;border:1px solid var(--line);border-radius:8px;background:var(--paper);white-space:pre-wrap;overflow-wrap:anywhere}.message.user .bubble{border-color:#cadcd4;background:var(--green-soft)}.message-meta{display:flex;gap:8px;align-items:center;margin:5px 3px 0;color:var(--muted);font-size:11px}.message.user .message-meta{justify-content:flex-end}.status{display:inline-flex;align-items:center;gap:6px;color:var(--muted);font-size:12px;white-space:nowrap}.status::before{content:"";width:7px;height:7px;border-radius:50%;background:#aab0aa}.status.done{color:var(--green)}.status.done::before{background:var(--green)}.status.running,.status.queued,.status.waiting_for_service{color:var(--orange)}.status.running::before,.status.queued::before,.status.waiting_for_service::before{background:var(--orange)}.status.failed{color:var(--red)}.status.failed::before{background:var(--red)}.open-link{height:34px;display:inline-flex;align-items:center;gap:6px;margin-top:10px;padding:0 10px;border:1px solid var(--line);border-radius:7px;background:var(--paper);color:var(--green);text-decoration:none}.empty{padding:28px 2px;color:var(--muted);border-top:1px solid var(--line)}.admin-switch{display:grid;gap:8px;margin-top:18px;padding-top:16px;border-top:1px solid var(--line-soft)}.admin-switch label{color:var(--muted);font-size:12px}.admin-switch select{width:100%;height:40px;border:1px solid var(--line);border-radius:7px;background:#fff;padding:0 10px;font:inherit}
    dialog{width:min(430px,calc(100vw - 28px));border:1px solid var(--line);border-radius:8px;padding:0;color:var(--text);background:var(--paper);box-shadow:0 24px 80px rgba(26,31,27,.24)}dialog::backdrop{background:rgba(30,36,32,.34)}.dialog-body{padding:22px}.dialog-head{display:flex;align-items:flex-start;justify-content:space-between;gap:20px}.dialog-head h2{font-size:20px;margin:0}.close{width:34px;height:34px;display:grid;place-items:center;border:0;background:transparent;color:var(--muted)}.bind-copy{margin:20px 0 10px;color:var(--muted);font-size:13px}.command{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:12px 13px;border:1px solid var(--line);border-radius:7px;background:#f8faf8}.command code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:16px;color:var(--text)}.copy{width:34px;height:34px;display:grid;place-items:center;border:0;background:transparent;color:var(--green)}.expires{margin-top:10px;color:var(--muted);font-size:12px}.bound-box{margin-top:20px;padding:14px;border:1px solid #cfe0d8;border-radius:7px;background:var(--green-soft)}.error{min-height:22px;margin-top:10px;color:var(--red);font-size:13px}.toast{position:fixed;left:50%;bottom:26px;transform:translate(-50%,18px);padding:9px 13px;border-radius:7px;background:#252b27;color:#fff;opacity:0;pointer-events:none;transition:.2s}.toast.show{opacity:1;transform:translate(-50%,0)}
    @media(max-width:620px){.shell{width:min(100% - 24px,900px);padding-top:18px}header{padding-bottom:18px}.brand span{display:none}.account{max-width:50vw;overflow:hidden}.account-label{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}main{padding-top:32px}.intro h1{font-size:25px}.submit-bar{align-items:flex-start;flex-direction:column;padding:12px}.submit-button{width:100%;justify-content:center}.message{max-width:92%}.section-head{margin-top:36px}}
  </style>
</head>
<body>
  <div class="shell">
    <header>
      <div class="brand"><div class="brand-mark"><svg viewBox="0 0 24 24"><path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2Z"/></svg></div><div><strong>MaxRead</strong><span>论文阅读队列</span></div></div>
      <button class="account" id="account-button" onclick="openAccount()"><span class="dot"></span><span class="account-label">游客</span></button>
    </header>
    <main>
      <section class="intro"><h1>读一篇论文</h1><p>提交 arXiv 链接或论文 ID。</p></section>
      <section class="submit-tool">
        <label class="input-label" for="paper-input"><span class="icon"><svg viewBox="0 0 24 24"><path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/></svg></span>论文链接</label>
        <textarea id="paper-input" placeholder="https://arxiv.org/abs/2608.25927"></textarea>
        <div class="submit-bar"><span class="detected" id="detected">等待输入</span><button class="submit-button" id="submit-button" onclick="submitPapers()"><span class="icon"><svg viewBox="0 0 24 24"><path d="m5 12 14-7-4 14-3-6-7-1Z"/><path d="M12 13 19 5"/></svg></span>开始阅读</button></div>
      </section>
      <div class="service-note" id="service-note"></div>
      <div class="section-head"><h2>会话</h2><button aria-label="刷新" title="刷新" onclick="loadMessages()"><span class="icon"><svg viewBox="0 0 24 24"><path d="M20 11a8.1 8.1 0 0 0-15.5-2M4 4v5h5"/><path d="M4 13a8.1 8.1 0 0 0 15.5 2M20 20v-5h-5"/></svg></span></button></div>
      <section class="conversation" id="history"><div class="empty">暂无消息</div></section>
    </main>
  </div>
  <dialog id="account-dialog"><div class="dialog-body"><div class="dialog-head"><div><h2 id="account-title">绑定飞书账号</h2></div><button class="close" aria-label="关闭" onclick="closeAccount()"><span class="icon"><svg viewBox="0 0 24 24"><path d="M18 6 6 18M6 6l12 12"/></svg></span></button></div><div id="account-content"></div><div class="error" id="binding-error"></div></div></dialog>
  <div class="toast" id="toast"></div>
  <script>
    const state={me:null,poll:null,adminAuthenticated:false,actAs:sessionStorage.getItem('maxreadActAs')||''};const $=id=>document.getElementById(id);const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
    const basePath=window.location.pathname.startsWith('/maxread')?'/maxread':'';
    async function api(path,opts={}){const headers={'content-type':'application/json',...(opts.headers||{})};if(state.actAs)headers['x-maxread-act-as']=state.actAs;const response=await fetch(`${basePath}${path}`,{credentials:'same-origin',...opts,headers});const body=await response.json().catch(()=>({}));if(!response.ok)throw new Error(body.error||`HTTP ${response.status}`);return body}
    function icon(name){const paths={open:'<path d="M15 3h6v6M10 14 21 3M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/>',copy:'<rect width="14" height="14" x="8" y="8" rx="2"/><path d="M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2"/>'};return `<span class="icon"><svg viewBox="0 0 24 24">${paths[name]||''}</svg></span>`}
    function toast(text){$('toast').textContent=text;$('toast').classList.add('show');setTimeout(()=>$('toast').classList.remove('show'),1800)}
    function formatDuration(seconds){const m=Math.max(1,Math.round(Number(seconds||0)/60));return m<60?`${m} 分钟`:`${Math.floor(m/60)} 小时 ${m%60} 分钟`}
    async function loadMe(){try{state.me=await api('/api/web/me')}catch(error){if(state.actAs){state.actAs='';sessionStorage.removeItem('maxreadActAs');state.me=await api('/api/web/me')}else throw error}const button=$('account-button');button.classList.toggle('bound',state.me.bound);button.querySelector('.account-label').textContent=state.me.acting_as?`管理员 · ${state.me.display_name}`:(state.me.bound?state.me.display_name:'游客')}
    function detected(){const ids=[...$('paper-input').value.matchAll(/\b\d{4}\.\d{4,5}\b/g)].map(x=>x[0]);$('detected').textContent=ids.length?`识别到 ${new Set(ids).size} 篇`:'等待输入'}
    $('paper-input').addEventListener('input',detected);
    async function submitPapers(){const button=$('submit-button');button.disabled=true;try{const result=await api('/api/web/submit',{method:'POST',body:JSON.stringify({content:$('paper-input').value})});const item=result.items[0];$('paper-input').value='';detected();if(item?.cached)toast('已找到现有文档');else if(item)toast(`已入队，预计 ${formatDuration(item.estimated_total_seconds)}`);renderService(result.service);await loadMessages()}catch(error){toast(error.message)}finally{button.disabled=false}}
    function renderService(service){const box=$('service-note');if(!service||service.mode==='operational'){box.classList.remove('visible');return}box.textContent=`服务暂缓：${service.reason||service.mode}${service.expected_recovery_at?` · 预计 ${service.expected_recovery_at}`:''}`;box.classList.add('visible')}
    const labels={queued:'排队中',running:'处理中',done:'已完成',failed:'失败',waiting_for_service:'等待恢复'};
    function messageHtml(message){const status=message.status||'';const admin=message.actor_type==='admin'?'<span>管理员代入</span>':'';const action=message.doc_url?`<a class="open-link" href="${esc(message.doc_url)}" target="_blank" rel="noopener">${icon('open')}打开文档</a>`:'';return `<article class="message ${esc(message.role)}"><div class="bubble">${esc(message.content)}${action}</div><div class="message-meta">${status?`<span class="status ${esc(status)}">${esc(labels[status]||status)}</span>`:''}<span>${esc(message.channel==='feishu'?'飞书':'网页')}</span>${admin}<span>${esc(message.updated_at||message.created_at||'')}</span></div></article>`}
    async function loadMessages(){try{const rows=await api('/api/web/messages');$('history').innerHTML=rows.length?rows.map(messageHtml).join(''):'<div class="empty">暂无消息</div>';if(!state.poll)state.poll=setInterval(loadMessages,4000)}catch(error){$('history').innerHTML='<div class="empty">会话暂时无法加载</div>'}}
    async function loadAdminAccounts(){try{const status=await api('/api/admin/status');state.adminAuthenticated=Boolean(status.authenticated);if(!state.adminAuthenticated)return '';const accounts=await api('/api/web/admin/accounts');return `<div class="admin-switch"><label for="admin-account">管理员代入用户</label><select id="admin-account" onchange="setActAs(this.value)"><option value="">我的网页身份</option>${accounts.map(a=>`<option value="${esc(a.public_id)}" ${a.public_id===state.actAs?'selected':''}>${esc(a.display_name||'游客')} · ${esc(a.account_type)} · ${esc(a.submission_count)}</option>`).join('')}</select></div>`}catch(_error){return ''}}
    async function setActAs(publicId){state.actAs=publicId;if(publicId)sessionStorage.setItem('maxreadActAs',publicId);else sessionStorage.removeItem('maxreadActAs');await loadMe();await loadMessages();closeAccount();toast(publicId?'已进入用户态':'已退出用户态')}
    async function openAccount(){$('binding-error').textContent='';$('account-dialog').showModal();const admin=await loadAdminAccounts();if(state.me?.acting_as){$('account-title').textContent='管理员代入态';$('account-content').innerHTML=`<div class="bound-box"><strong>${esc(state.me.display_name)}</strong><br><span>操作会记录管理员审计标记</span></div>${admin}`;return}if(state.me?.bound){$('account-title').textContent='飞书账号';$('account-content').innerHTML=`<div class="bound-box"><strong>${esc(state.me.display_name)}</strong><br><span>网页与飞书提交共享同一会话</span></div>${admin}`;return}$('account-title').textContent='绑定飞书账号';$('account-content').innerHTML='<div class="bind-copy">正在生成绑定码...</div>';try{const data=await api('/api/web/binding-code',{method:'POST',body:'{}'});$('account-content').innerHTML=`<div class="bind-copy">在飞书私聊「读不动了」发送：</div><div class="command"><code>${esc(data.command)}</code><button class="copy" aria-label="复制" onclick="copyCommand('${esc(data.command)}')">${icon('copy')}</button></div><div class="expires">绑定码 10 分钟内有效，完成后页面会自动更新。</div>${admin}`;pollBinding()}catch(error){$('binding-error').textContent=error.message}}
    function closeAccount(){$('account-dialog').close()}
    async function copyCommand(command){await navigator.clipboard.writeText(command);toast('已复制')}
    async function pollBinding(){for(let i=0;i<120&&$('account-dialog').open;i++){await new Promise(r=>setTimeout(r,2000));await loadMe();if(state.me.bound){$('account-title').textContent='绑定成功';$('account-content').innerHTML=`<div class="bound-box"><strong>${esc(state.me.display_name)}</strong><br><span>网页与飞书会话已经合并</span></div>`;await loadMessages();return}}}
    Promise.all([loadMe(),loadMessages()]).catch(()=>null);
  </script>
</body>
</html>'''
