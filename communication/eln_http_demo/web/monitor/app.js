"use strict";
const $=id=>document.getElementById(id);
const labels={ACCEPTED:"已受理",RUNNING:"执行中",SUCCEEDED:"已完成",FAILED:"失败",CANCELLED:"已取消",delivered:"已送达",pending:"待投递",exhausted:"重试耗尽"};
const stages={received:"已到达入口",body_received:"请求体已读取",forwarding:"正在等待执行接口",upstream_response:"执行接口已响应",response_ready:"已生成 HTTP 响应",authorization:"授权校验未通过",routing:"路径或方法不匹配",task_lookup:"任务号不存在",validation:"参数校验未通过",admission:"执行前置条件未通过",recording_selection:"缺少动作录制",backend_rejected:"执行接口拒绝",transport_error:"执行接口连接或响应异常",body_error:"请求体读取失败",interrupted:"请求处理被中断",historical:"历史访问日志"};
let snapshot=null,historyPage=null,tab="requests",selected=null,detailRow=null,paused=false,busy=false,apiKey="",page=1,anchor=null,revision=0,searchTimer;
const time=s=>s?new Date(s).toLocaleString("zh-CN",{hour12:false}):"--";
const group=s=>["SUCCEEDED","delivered"].includes(s)?"success":["FAILED","exhausted"].includes(s)?"problem":["RUNNING","ACCEPTED","pending"].includes(s)?"pending":"other";
const acceptance=row=>({accepted:"已受理",rejected:"已拒绝",not_applicable:"不适用"}[row.acceptance]||"未确认");
const headers=()=>apiKey?{"X-API-Key":apiKey}:{};
function text(id,value){$(id).textContent=value??"--";}
function pill(value,kind){const el=document.createElement("span");el.className="pill "+({success:"good",problem:"bad",pending:"wait"}[kind]||"neutral");el.textContent=value;return el;}
function requestGroup(row){return !row.finished?"pending":row.httpStatus>=400||["interrupted","transport_error","body_error","backend_rejected","admission","recording_selection","validation"].includes(row.stage)?"problem":"success";}
function params(){const p=new URLSearchParams({search:$("search").value,result:$("filter").value,show_health:String($("show-health").checked)});for(const id of ["since","until"]){if($(id).value)p.set(id,new Date($(id).value).toISOString());}if(anchor!==null)p.set("anchor",anchor);return p;}
async function get(url){const r=await fetch(url,{headers:headers(),cache:"no-store",signal:AbortSignal.timeout(10000)});if(r.status===401){$("auth").hidden=false;throw Error("需要监控访问密钥");}if(!r.ok){let b;try{b=await r.json();}catch{}throw Error(b?.message||"HTTP "+r.status);}return r.json();}
function showError(message){text("errors",message);$("errors").hidden=false;}
function fields(row){
  if(tab==="requests"){
    const out=[["到达时间",time(row.timestamp)],["请求标识",row.id],["来源地址",row.peer],["请求方法",row.method],["请求路径",row.path],["处理阶段",stages[row.stage]||row.stage],["HTTP 返回",row.httpStatus??"尚未生成响应"],["耗时",row.durationMs==null?"--":row.durationMs+" ms"],["任务号",row.taskId],["业务错误码",row.businessCode],["原因",row.businessMessage||row.message],["任务受理",acceptance(row)],["记录来源",row.source==="journal"?"历史系统日志":"持久化入口日志"]];
    if(row.task){out.push(["关联任务当前状态",labels[row.task.state.status]||row.task.state.status],["执行说明",row.task.state.message]);for(const cb of row.task.callbacks.filter(c=>["SUCCEEDED","FAILED","CANCELLED"].includes(c.state.status)))out.push(["最终回调",(labels[cb.delivery]||cb.delivery)+" · "+cb.attempts+" 次"],["回调错误",cb.error||"无"]);}
    if(row.taskError)out.push(["任务数据读取错误",row.taskError]);return out;
  }
  const out=[["任务号",row.state.taskId],["任务类型",row.state.taskType],["当前状态",labels[row.state.status]||row.state.status],["完成次数",row.state.progress],["状态时间",time(row.state.timestamp)],["说明",row.state.message],["错误码",row.state.errorCode||"无"]];
  if(tab==="callbacks")out.push(["投递状态",labels[row.delivery]],["尝试次数",row.attempts],["接收地址",row.url],["投递错误",row.error||"无"]);else out.push(["任务受理",acceptance(row)],["回调地址",row.request.callbackUrl]);return out;
}
function detail(row){
  $("detail").hidden=!row;$("detail-empty").hidden=!!row;text("detail-kind",{requests:"HTTP",tasks:"TASK",callbacks:"CALLBACK"}[tab]);if(!row)return;
  $("detail-fields").replaceChildren();for(const [key,value] of fields(row)){const dt=document.createElement("dt"),dd=document.createElement("dd");dt.textContent=key;dd.textContent=value==null?"--":typeof value==="object"?JSON.stringify(value):value;$("detail-fields").append(dt,dd);}
  $("timeline").replaceChildren();$("timeline-section").hidden=!row.steps?.length;
  for(const s of row.steps||[]){const li=document.createElement("li"),strong=document.createElement("strong"),small=document.createElement("small"),p=document.createElement("p");strong.textContent=stages[s.stage]||s.stage;small.textContent=time(s.timestamp);p.textContent=s.message;li.append(strong,small,p);$("timeline").append(li);}text("detail-json",JSON.stringify(row,null,2));
}
async function loadDetail(){const id=selected;if(tab!=="requests"||!id)return;try{const row=await get("/requests/"+encodeURIComponent(id));if(tab==="requests"&&selected===id){detailRow=row;detail(row);}}catch(e){if(selected===id)showError(e.message);}}
function render(){
  if(!snapshot)return;const h=snapshot.health||{},unavailable=snapshot.errors.database;
  text("backend",h.backend);text("mode",h.simulation===true?"模拟执行":h.simulation===false?"真实执行":"未知");$("mode").className=h.simulation===false?"live":"";text("initialized",h.initialized===true?"已就绪":h.initialized===false?"未就绪":"未知");text("active",h.activeTaskId||"无");
  text("request-count",historyPage?.allTotal??snapshot.requestPage?.allTotal??"--");text("last-request","最近请求 "+time(snapshot.requests[0]?.timestamp));text("task-count",unavailable?"--":Object.values(snapshot.taskCounts).reduce((a,b)=>a+b,0));text("running-count","执行中 "+(unavailable?"--":snapshot.taskCounts.RUNNING||0));text("delivered-count",snapshot.callbackCounts.delivered);text("pending-count","待投递 "+(snapshot.callbackCounts.pending??"--"));text("failed-count",snapshot.callbackCounts.exhausted);$("failed-count").className=snapshot.callbackCounts.exhausted?"alert":"";text("recovery",h.requiresRecovery===true?"需要恢复确认":h.requiresRecovery===false?"无需恢复":"恢复状态 --");text("updated",time(snapshot.timestamp));text("source-api","API "+snapshot.sources.api);text("source-journal","历史库 "+(snapshot.sources.history||"--"));
  const errors=Object.entries(snapshot.errors).map(([k,v])=>({health:"服务状态",requests:"请求历史",database:"任务数据"}[k])+"："+v);text("errors",errors.join("\n"));$("errors").hidden=!errors.length;
  text("requests-size",historyPage?.total??0);for(const key of ["tasks","callbacks"])text(key+"-size",snapshot[key].length);$("history-filters").hidden=tab!=="requests";$("pagination").hidden=tab!=="requests";$("export").title=tab==="requests"?"导出全部筛选请求记录":"导出当前监控快照";
  const headings=tab==="requests"?["到达时间","来源 / 任务号","方法 / 路径","处理阶段 / 结果"]:tab==="tasks"?["状态时间","任务号","任务 / 进度","受理 / 当前状态"]:["状态时间","任务号","状态 / 尝试次数","回调结果"];
  const hr=document.createElement("tr");for(const value of headings){const th=document.createElement("th");th.textContent=value;hr.append(th);}$("head").replaceChildren(hr);
  let rows=tab==="requests"?(historyPage?.items||[]):snapshot[tab];if(tab!=="requests"){const search=$("search").value.toLowerCase(),filter=$("filter").value;rows=rows.filter(r=>JSON.stringify(r).toLowerCase().includes(search)).filter(r=>filter==="all"||filter===group(tab==="tasks"?r.state.status:r.delivery));}
  $("rows").replaceChildren();for(const row of rows){
    const id=tab==="tasks"?row.state.taskId:row.id,tr=document.createElement("tr");tr.tabIndex=0;tr.setAttribute("aria-label",tab==="requests"?row.method+" "+row.path:row.state.taskId);if(selected===id)tr.className="selected";
    let cells;if(tab==="requests"){const result=document.createElement("div");result.append(pill((row.httpStatus==null?"":row.httpStatus+" · ")+(stages[row.stage]||row.stage),requestGroup(row)));if(row.businessMessage){const summary=document.createElement("div");summary.className="response-summary";summary.textContent=typeof row.businessMessage==="string"?row.businessMessage:JSON.stringify(row.businessMessage);result.append(summary);}cells=[time(row.timestamp),row.peer+(row.taskId?"\n"+row.taskId:""),row.method+" "+row.path,result];}
    else if(tab==="tasks")cells=[time(row.state.timestamp),row.state.taskId,row.state.taskType+" · "+row.state.progress+" / "+(row.request.parameters?.count||1),pill(acceptance(row)+" · "+(labels[row.state.status]||row.state.status),group(row.state.status))];
    else cells=[time(row.state.timestamp),row.state.taskId,(labels[row.state.status]||row.state.status)+" · "+row.attempts+" 次",pill(labels[row.delivery],group(row.delivery))];
    for(const v of cells){const td=document.createElement("td");if(v instanceof Node)td.append(v);else td.textContent=v;tr.append(td);}const choose=()=>{selected=id;detailRow=null;render();if(tab==="requests")loadDetail();};tr.onclick=choose;tr.onkeydown=e=>{if(e.key==="Enter"||e.key===" "){e.preventDefault();choose();}};$("rows").append(tr);
  }
  $("empty").hidden=rows.length>0;if(tab==="requests"){const pages=Math.max(1,Math.ceil((historyPage?.total||0)/50));text("page-label",page+" / "+pages);$("prev-page").disabled=page<=1;$("next-page").disabled=page>=pages;text("record-footer",`共 ${historyPage?.total??0} 条匹配记录 · 本页 ${rows.length} 条`);detail(detailRow||rows.find(r=>r.id===selected));}
  else{text("record-footer",`${rows.length} 条记录 · ${tab==="tasks"?"最近 80 条任务记录":"最近 120 条回调事件"}`);detail(rows.find(r=>(tab==="tasks"?r.state.taskId:r.id)===selected));}
}
async function refresh(){
  if(busy)return;busy=true;const version=revision;try{const p=params();p.set("page",page);p.set("page_size",50);const [s,h]=await Promise.all([get("/snapshot"),tab==="requests"?get("/requests?"+p):Promise.resolve(historyPage)]);if(version!==revision)return;snapshot=s;historyPage=h;if(page>1&&h)anchor=h.anchor;$("auth").hidden=true;const partial=Object.keys(snapshot.errors).length>0,stale=Date.now()-Date.parse(snapshot.timestamp)>15000;text("connection",paused?"已暂停":stale?"数据过期":partial?"部分数据不可用":"实时连接");$("connection-dot").className=partial||stale?"bad":"ok";render();await loadDetail();}catch(e){text("connection","连接异常");$("connection-dot").className="bad";showError(e.message+"；页面保留上次数据");}finally{busy=false;if(version!==revision)refresh();}
}
function reset(){page=1;anchor=null;selected=null;detailRow=null;revision++;refresh();}
for(const button of document.querySelectorAll(".tab"))button.onclick=()=>{tab=button.dataset.tab;document.querySelectorAll(".tab").forEach(b=>b.classList.toggle("active",b===button));reset();render();};$("search").oninput=()=>{clearTimeout(searchTimer);searchTimer=setTimeout(reset,250);};$("filter").onchange=reset;for(const id of ["since","until","show-health"])$(id).onchange=reset;
$("prev-page").onclick=()=>{page--;selected=null;detailRow=null;revision++;refresh();};$("next-page").onclick=()=>{anchor=historyPage.anchor;page++;selected=null;detailRow=null;revision++;refresh();};$("latest").onclick=reset;$("refresh").onclick=refresh;
$("pause").onclick=()=>{paused=!paused;$("pause").querySelector("span").textContent=paused?"继续":"暂停";$("pause").querySelector("img").src=paused?"/assets/icons/play.svg":"/assets/icons/pause.svg";text("connection",paused?"已暂停":"连接中");if(!paused)refresh();};$("auth").onsubmit=e=>{e.preventDefault();apiKey=$("key").value;$("key").value="";refresh();};
$("export").onclick=async()=>{try{let blob,name;if(tab==="requests"){const response=await fetch("/requests/export?"+params(),{headers:headers()});if(!response.ok)throw Error("导出失败 HTTP "+response.status);blob=await response.blob();name="eln-request-history-"+Date.now()+".ndjson";}else{if(!snapshot)return;blob=new Blob([JSON.stringify(snapshot,null,2)],{type:"application/json"});name="eln-monitor-"+Date.now()+".json";}const url=URL.createObjectURL(blob),link=document.createElement("a");link.href=url;link.download=name;link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}catch(e){showError(e.message);}};
setInterval(()=>{if(!paused)refresh();},2000);refresh();
