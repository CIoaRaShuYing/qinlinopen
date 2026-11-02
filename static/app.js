'use strict';
const $ = id => document.getElementById(id);
let current = null, opening = false, refreshing = false, smsTimer = null;
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
function showLogin() { $('login-view').hidden=false; $('dashboard').hidden=true; $('logout').hidden=true; $('boot-message').hidden=true; $('doors').replaceChildren();$('activity-list').replaceChildren();$('community-select').replaceChildren();$('community-name').textContent='尚未获取小区';$('operation-message').textContent='';current=null; }
function showDashboard() { $('login-view').hidden=true; $('dashboard').hidden=false; $('logout').hidden=false; $('boot-message').hidden=true; }
function formatTime(value) { return value ? new Date(value*1000).toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false}) : '尚未检查'; }
function node(tag, cls, content) { const el=document.createElement(tag); if(cls)el.className=cls; if(content!==undefined)el.textContent=content; return el; }
function render(data) {
  current=data;
  const states={valid:['凭证有效','可以选择门禁并发送开门指令。'],expired:['凭证已过期','请重新获取短信验证码，连接亲邻账户。'],unknown:['暂时无法验证',data.error?.message || '请检查网络后刷新，或重新验证手机号。'],unconfigured:['尚未连接亲邻','使用手机号验证，或在服务器导入已有会话。']};
  const state=states[data.state]||states.unknown;
  $('credential-status').className=`status-panel ${data.state}`; $('state-label').textContent=state[0]; $('state-detail').textContent=state[1]; $('last-checked').textContent=`最近检查 ${formatTime(data.checkedAt)}`;
  if(data.state==='expired'||data.state==='unconfigured')$('auth-details').open=true;
  const community=data.communities.find(c=>c.id===data.communityId);
  $('community-name').textContent=community?.name || '尚未获取小区';
  const select=$('community-select'); select.replaceChildren(); select.hidden=data.communities.length<2; select.disabled=opening||refreshing;
  const defaultButton=$('set-default');defaultButton.hidden=data.state!=='valid'||!data.communityId;defaultButton.disabled=opening||refreshing||data.defaultCommunityId===data.communityId;defaultButton.textContent=data.defaultCommunityId===data.communityId?'默认展示小区':'设为默认展示小区';
  for(const c of data.communities){const option=node('option','',c.name);option.value=c.id;option.selected=c.id===data.communityId;select.append(option);}
  const grid=$('doors');grid.replaceChildren();
  for(const door of data.doors){
    const card=node('article','door-card'), top=node('div','door-card-top');
    const glyph=node('span','door-glyph','▯');glyph.setAttribute('aria-hidden','true');top.append(glyph,node('span','door-kind',door.kind));
    const button=node('button','primary');button.type='button';button.disabled=data.state!=='valid'||opening||refreshing;button.setAttribute('aria-label',`打开${door.name}`);button.append(node('span','','开门'),node('span','','↗'));button.addEventListener('click',()=>openDoor(door));
    card.append(top,node('h3','',door.name),node('p','door-meta',door.online==='online'?'设备在线':door.online==='offline'?'设备离线':'设备状态未知'),button);grid.append(card);
  }
  $('empty-doors').hidden=data.doors.length>0;
  if(!data.doors.length){$('empty-doors').querySelector('h3').textContent=data.state==='valid'?'当前小区没有可用门禁':'先连接你的亲邻账户';$('empty-doors').querySelector('p').textContent=data.state==='valid'?'可刷新列表，或检查亲邻 App 中的门禁权限。':'完成手机验证后，即可看到有权限的门禁。';}
  const list=$('activity-list');list.replaceChildren();
  const names={open:'开门',sms:'发送验证码',login:'亲邻登录'};
  const outcomes={accepted:'平台已接受',sent:'已发送',authenticated:'已连接',OPEN_RESULT_UNKNOWN:'结果未知',QINLIN_SESSION_EXPIRED:'凭证失效',QINLIN_REJECTED:'平台拒绝',SMS_REJECTED:'发送未确认'};
  for(const item of data.recent||[]){const li=node('li');const door=data.doors.find(d=>String(d.id)===item.target);li.append(node('span','',`${names[item.action]||'操作'}${door?' · '+door.name:''} · ${outcomes[item.result]||'未完成'}`),node('time','',formatTime(item.time)));list.append(li);}
  if(!list.children.length)list.append(node('li','muted','还没有操作记录'));
}
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
