'use strict';
const $ = id => document.getElementById(id);
let current = null, opening = false, refreshing = false, smsTimer = null, pendingReset = null, resetting = false;
let selectedApiDoor = null;
async function api(path, data) {
  let response;
  try { response = await fetch(path, {method: data === undefined ? 'GET' : 'POST', headers: data === undefined ? {} : {'Content-Type':'application/json'}, body: data === undefined ? undefined : JSON.stringify(data), credentials:'same-origin', cache:'no-store'}); }
  catch { const error = new Error('网络连接中断，请稍后刷新。若刚提交开门，请先现场确认。'); error.code = 'NETWORK_ERROR'; throw error; }
  let result;
  try { result = await response.json(); } catch { throw new Error('服务响应异常，请稍后刷新。若刚提交开门，请先现场确认。'); }
  if (!response.ok || !result.ok) {
    const error = new Error(result.error?.message || '请求失败，请稍后重试'); error.code = result.error?.code;
    if (error.code === 'ADMIN_UNAUTHORIZED') showLogin();
    throw error;
  }
  return result.data;
}
function message(id, text, success=false) { $(id).textContent=text; $(id).classList.toggle('success',success); }
function showLogin() { $('login-view').hidden=false; $('dashboard').hidden=true; $('logout').hidden=true; $('boot-message').hidden=true; $('doors').replaceChildren();$('api-doors').replaceChildren();$('api-message').textContent='';$('reset-dialog').close();$('door-api-dialog').close();selectedApiDoor=null;pendingReset=null;$('inventory-notices').replaceChildren();$('retired-list').replaceChildren();$('activity-list').replaceChildren();$('community-select').replaceChildren();$('community-name').textContent='尚未获取小区';$('operation-message').textContent='';current=null; }
function showDashboard() { $('login-view').hidden=true; $('dashboard').hidden=false; $('logout').hidden=false; $('boot-message').hidden=true; }
function formatTime(value) { return value ? new Date(value*1000).toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false}) : '尚未检查'; }
function node(tag, cls, content) { const el=document.createElement(tag); if(cls)el.className=cls; if(content!==undefined)el.textContent=content; return el; }
function render(data) {
  current=data;
  const states={valid:['凭证有效','可以选择门禁并发送开门指令。'],expired:['凭证已过期','请重新获取短信验证码，连接亲邻账户。'],unknown:['暂时无法验证',data.error?.message || '请检查网络后刷新，或重新验证手机号。'],unconfigured:['尚未连接亲邻','使用手机号验证，或在服务器导入已有会话。']};
  const state=states[data.state]||states.unknown;
  $('credential-status').className=`status-panel ${data.state}`; $('state-label').textContent=state[0]; $('state-detail').textContent=state[1]; $('last-checked').textContent=`最近检查 ${formatTime(data.checkedAt)}`;
  if(data.state==='expired'||data.state==='unconfigured')$('auth-details').open=true;
  const view=data.state==='valid'?data:(data.cachedInventory||data);
  const community=view.communities.find(c=>c.id===view.communityId);
  $('community-name').textContent=community?.name || '尚未获取小区';
  const select=$('community-select'); select.replaceChildren(); select.hidden=view.communities.length<2; select.disabled=opening||refreshing||data.state!=='valid';
  const defaultButton=$('set-default');defaultButton.hidden=data.state!=='valid'||!data.communityId;defaultButton.disabled=opening||refreshing||data.defaultCommunityId===data.communityId;defaultButton.textContent=data.defaultCommunityId===data.communityId?'默认展示小区':'设为默认展示小区';
  for(const c of view.communities){const option=node('option','',c.name);option.value=c.id;option.selected=c.id===view.communityId;select.append(option);}
  const grid=$('doors');grid.replaceChildren();
  for(const door of view.doors){
    const card=node('article','door-card'), top=node('div','door-card-top');
    const glyph=node('span','door-glyph','▯');glyph.setAttribute('aria-hidden','true');top.append(glyph,node('span','door-kind',door.kind));
    const button=node('button','primary');button.type='button';button.disabled=data.state!=='valid'||opening||refreshing;button.setAttribute('aria-label',`打开${door.name}`);button.append(node('span','','开门'),node('span','','↗'));button.addEventListener('click',()=>openDoor(door));
    const detail=node('button','door-detail','接口与长期 Token  ›');detail.type='button';detail.setAttribute('aria-label',`查看${door.name}接口与长期 Token`);
    const access=(data.apiDoors||[]).find(x=>x.communityId===view.communityId&&x.doorId===door.id);
    detail.disabled=!access;detail.addEventListener('click',()=>showDoorApi(access));
    card.append(top,node('h3','',door.name),node('p','door-meta',data.state!=='valid'?'服务器缓存 · 当前无法验证':door.online==='online'?'设备在线':door.online==='offline'?'设备离线':'设备状态未知'),button,detail);grid.append(card);
  }
  $('empty-doors').hidden=view.doors.length>0;
  if(!data.doors.length){$('empty-doors').querySelector('h3').textContent=data.state==='valid'?'当前小区没有可用门禁':'先连接你的亲邻账户';$('empty-doors').querySelector('p').textContent=data.state==='valid'?'可刷新列表，或检查亲邻 App 中的门禁权限。':'完成手机验证后，即可看到有权限的门禁。';}
  const list=$('activity-list');list.replaceChildren();
  const names={open:'开门',sms:'发送验证码',login:'亲邻登录',reset_tokens:'重置 Token'};
  const outcomes={accepted:'平台已接受',sent:'已发送',authenticated:'已连接',reset:'已重置',OPEN_RESULT_UNKNOWN:'结果未知',QINLIN_SESSION_EXPIRED:'凭证失效',QINLIN_REJECTED:'平台拒绝',SMS_REJECTED:'发送未确认'};
  for(const item of data.recent||[]){const li=node('li');const door=data.doors.find(d=>String(d.id)===item.target);li.append(node('span','',`${names[item.action]||'操作'}${door?' · '+door.name:''} · ${outcomes[item.result]||'未完成'}`),node('time','',formatTime(item.time)));list.append(li);}
  if(!list.children.length)list.append(node('li','muted','还没有操作记录'));
  renderAccess(data.apiDoors||[]);
  renderInventoryNotices(data.inventoryEvents||[]);
}
function showDoorApi(item){selectedApiDoor={communityId:item.communityId,doorId:item.doorId};$('api-message').textContent='';renderAccess(current.apiDoors||[]);$('door-api-dialog').showModal();$('close-door-api').focus();}
$('close-door-api').addEventListener('click',()=>{$('door-api-dialog').close();selectedApiDoor=null;$('api-doors').replaceChildren();});
$('door-api-dialog').addEventListener('cancel',()=>{selectedApiDoor=null;$('api-doors').replaceChildren();});
function renderInventoryNotices(events){
  const box=$('inventory-notices');box.replaceChildren();box.hidden=!events.length;
  for(const event of events){
    const notice=node('article','inventory-notice');notice.append(node('strong','','门禁接口配置发生变化'));
    let text;
    if(event.kind==='reactivated')text=`「${event.doorName}」重新出现在授权列表中。地址不变，Token 已更换，请进入该门详情重新配置调用方。`;
    else {const removed=(event.removed||[]).map(x=>`${x.name}（ID ${x.id}）`).join('、'),added=(event.added||[]).map(x=>`${x.name}（ID ${x.id}）`).join('、');text=`${event.kind==='communities'?'小区':'门禁'}标识列表已变化。${removed?'已移除：'+removed+'，对应旧接口已停用。':''}${added?'新增：'+added+'；新接口使用独立地址和 Token，请进入对应门卡详情重新配置。':''} 系统不会根据名称自动替换开门目标。`;}
    notice.append(node('p','',text));const ack=node('button','secondary','已了解');ack.type='button';ack.addEventListener('click',async()=>{ack.disabled=true;try{const result=await api(`/api/inventory-events/${event.id}/acknowledge`,{});current.inventoryEvents=result.inventoryEvents;renderInventoryNotices(result.inventoryEvents);}catch(error){ack.disabled=false;$('operation-message').textContent=error.message;}});notice.append(ack);box.append(notice);
  }
}
async function copyValue(value) {
  try { await navigator.clipboard.writeText(value); $('api-message').textContent='已复制'; }
  catch { $('api-message').textContent='浏览器未允许复制，请选中字段内容后手动复制。'; }
}
function renderAccess(items) {
  const grid=$('api-doors');grid.replaceChildren();$('reset-all-tokens').disabled=!items.length||resetting;
  const retired=$('retired-list');retired.replaceChildren();const inactive=items.filter(x=>!x.active);$('retired-interfaces').hidden=!inactive.length;
  for(const item of inactive){const button=node('button','quiet',`${item.communityName} / ${item.doorName} · 已失效`);button.addEventListener('click',()=>showDoorApi(item));retired.append(button);}
  for(const item of items.filter(x=>selectedApiDoor&&x.communityId===selectedApiDoor.communityId&&x.doorId===selectedApiDoor.doorId)){
    $('door-api-title').textContent=item.doorName;
    const card=node('article','api-card'),heading=node('div','section-heading');
    heading.append(node('h3','',item.doorName),node('span','muted',item.communityName));card.append(heading);
    function field(label,value,secret=false){
      const wrap=node('div','api-field'),caption=node('label','',label),row=node('div','api-field-row'),input=node('input');
      input.type=secret?'password':'text';input.readOnly=true;input.value=value;input.autocomplete='off';input.spellcheck=false;input.setAttribute('aria-label',`${item.doorName} ${label}`);
      row.append(input);const copy=node('button','secondary',secret?'复制 Token':'复制地址');copy.type='button';copy.addEventListener('click',()=>copyValue(value));row.append(copy);
      if(secret){const reveal=node('button','quiet','显示');reveal.type='button';reveal.addEventListener('click',()=>{input.type=input.type==='password'?'text':'password';reveal.textContent=input.type==='password'?'显示':'隐藏';});row.append(reveal);}
      wrap.append(caption,row);card.append(wrap);
    }
    field('POST 请求地址',item.url);
    if(!item.active){card.append(node('p','retired-warning','此接口已失效，旧 Token 不能继续开门。请返回门列表选择当前有效的门，并重新配置请求地址和 Token。'));grid.append(card);continue;}
    field('长期 Token',item.token,true);
    const example=`curl --request POST '${item.url}' --header 'Authorization: Bearer ${item.token}'`;
    const preview=node('pre','request-preview',`POST ${item.url}\nAuthorization: Bearer <该门的 Token>\n无需请求体`);card.append(preview);
    const actions=node('div','api-actions'),copy=node('button','secondary','复制完整 cURL'),reset=node('button','quiet danger','重置此门 Token');
    copy.type=reset.type='button';copy.addEventListener('click',()=>copyValue(example));reset.disabled=resetting;reset.addEventListener('click',()=>askReset(item));actions.append(copy,reset);card.append(actions);grid.append(card);
  }
  if(!items.length)grid.append(node('p','muted','连接亲邻账户并加载门列表后，这里会显示各门的请求地址和 Token。'));
}
function askReset(item=null){
  if(resetting)return;
  pendingReset=item?{communityId:item.communityId,doorId:item.doorId}:{all:true};
  $('reset-description').textContent=item?`即将重置「${item.communityName} / ${item.doorName}」的 Token。其他门不受影响。`:`即将重置所有小区已生成的 ${current.apiDoors.length} 个门禁 Token。`;
  $('reset-error').textContent='';$('reset-dialog').showModal();$('cancel-reset').focus();
}
$('reset-all-tokens').addEventListener('click',()=>askReset());
$('cancel-reset').addEventListener('click',()=>{if(!resetting){pendingReset=null;$('reset-dialog').close();}});
$('reset-dialog').addEventListener('cancel',event=>{if(resetting)event.preventDefault();else pendingReset=null;});
$('reset-form').addEventListener('submit',async event=>{
  event.preventDefault();if(!pendingReset||resetting)return;resetting=true;$('confirm-reset').disabled=true;$('cancel-reset').disabled=true;
  try{const data=await api('/api/door-tokens/reset',{...pendingReset,confirm:true});pendingReset=null;$('reset-dialog').close();render(data);const text=`已重置 ${data.count} 个 Token，旧 Token 已失效，请更新调用方。`;$('api-message').textContent=text;$('operation-message').textContent=text;$('operation-message').classList.remove('error');}
  catch(error){$('reset-error').textContent=error.message+' 如果网络中断，重置可能已完成，请取消后刷新查看。';}
  finally{resetting=false;$('confirm-reset').disabled=false;$('cancel-reset').disabled=false;if(current)renderAccess(current.apiDoors||[]);}
});
async function load(force=false,communityId) { refreshing=true;if(current)render(current);try{const data=await api(force?'/api/refresh':'/api/status',force?(communityId?{communityId}:{}):undefined);render(data);}finally{refreshing=false;if(current)render(current);} }
async function openDoor(door){
  if(opening||refreshing||current?.state!=='valid')return;
  opening=true;render(current);$('operation-message').classList.remove('error');$('operation-message').textContent=`正在向「${door.name}」发送开门指令…`;
  $('refresh').disabled=true;
  try{const result=await api('/api/doors/open',{communityId:current.communityId,doorId:door.id});$('operation-message').textContent=`${door.name}：${result.message}`;}
  catch(error){$('operation-message').classList.add('error');$('operation-message').textContent=error.message;if(error.code==='QINLIN_SESSION_EXPIRED'&&current){current.state='expired';$('auth-details').open=true;}}
  finally{opening=false;$('refresh').disabled=false;if(current){render(current);try{await load();}catch{/* Preserve the command result if the status refresh fails. */}}}
}
$('admin-form').addEventListener('submit',async event=>{event.preventDefault();const button=event.submitter;button.disabled=true;message('login-message','正在登录…');try{await api('/api/auth/login',{username:$('username').value,password:$('password').value});$('password').value='';message('login-message','');showDashboard();await load();}catch(error){message('login-message',error.message);if(!$('dashboard').hidden){$('operation-message').textContent=error.message;$('operation-message').classList.add('error');}}finally{button.disabled=false;}});
$('logout').addEventListener('click',async()=>{try{await api('/api/auth/logout',{});showLogin();$('mobile').value='';$('sms-code').value='';}catch(error){$('operation-message').textContent=error.message;}});
$('refresh').addEventListener('click',async()=>{const button=$('refresh');button.disabled=true;try{await load(true);}catch(error){$('operation-message').textContent=error.message;$('operation-message').classList.add('error');}finally{button.disabled=false;}});
$('community-select').addEventListener('change',async event=>{const select=event.target;select.disabled=true;try{await load(true,Number(select.value));}catch(error){$('operation-message').textContent=error.message;}finally{select.disabled=false;}});
$('set-default').addEventListener('click',async()=>{const button=$('set-default');button.disabled=true;try{const data=await api('/api/preferences/community',{communityId:current.communityId});render(data);$('operation-message').textContent='已设为默认展示小区，下次登录时优先展示。';$('operation-message').classList.remove('error');}catch(error){$('operation-message').textContent=error.message;$('operation-message').classList.add('error');button.disabled=false;}});
$('send-sms').addEventListener('click',async()=>{
  if(!$('mobile').reportValidity())return;
  const button=$('send-sms');button.disabled=true;message('qinlin-message','正在发送…');
  try{const result=await api('/api/qinlin/sms',{mobile:$('mobile').value});message('qinlin-message',result.message,true);let remaining=result.retryAfter;button.textContent=`${remaining} 秒后重发`;clearInterval(smsTimer);smsTimer=setInterval(()=>{remaining--;button.textContent=remaining>0?`${remaining} 秒后重发`:'获取验证码';if(remaining<=0){clearInterval(smsTimer);button.disabled=false;}},1000);}
  catch(error){message('qinlin-message',error.message);button.disabled=false;}
});
$('qinlin-form').addEventListener('submit',async event=>{event.preventDefault();const button=event.submitter;button.disabled=true;message('qinlin-message','正在验证…');try{const result=await api('/api/qinlin/login',{mobile:$('mobile').value,code:$('sms-code').value});$('sms-code').value='';render(result);if(result.state==='valid'){$('auth-details').open=false;message('qinlin-message','已连接亲邻账户',true);$('operation-message').textContent='亲邻账户已连接，可以选择门禁。';$('operation-message').classList.remove('error');}else message('qinlin-message','凭证已保存，但暂未完成状态检查，请刷新重试。',true);}catch(error){message('qinlin-message',error.message);}finally{button.disabled=false;}});
(async()=>{try{const session=await api('/api/auth/session');if(session.authenticated){showDashboard();await load();}else showLogin();}catch(error){$('boot-message').hidden=false;$('boot-message').textContent=error.message;}})();
