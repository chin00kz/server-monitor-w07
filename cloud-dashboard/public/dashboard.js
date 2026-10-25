(function(root,factory){const api=factory();if(typeof module==="object"&&module.exports)module.exports=api;if(root)root.DashboardPresentation=api})(typeof globalThis!=="undefined"?globalThis:this,function(){
const SERVICE_NAMES=["IMFS Integration","IMFS M1","IMFS M2","IMFS M3","IMFS M4","Ollama"],ROUTES={starlink:"starlink-mini",dialog:"dialog-ethernet"};
function networkState(value,isRoute,connection){if(connection!=="live")return{label:"Unknown",level:"neutral"};if(!["reachable","unreachable"].includes(value))return{label:"Unknown",level:"neutral"};if(value==="unreachable")return{label:"Unreachable",level:"bad"};return isRoute?{label:"Active",level:"good"}:{label:"Reachable",level:"neutral"}}
function networkModel(network={},connection="disconnected"){const active=Object.values(ROUTES).includes(network.active_route)?network.active_route:"unknown",starlink=networkState(network.starlink,active===ROUTES.starlink,connection),dialog=networkState(network.dialog,active===ROUTES.dialog,connection);return{
starlink,
dialog,
starlinkRole:connection!=="live"?"UNKNOWN":active===ROUTES.starlink?"PRIMARY":starlink.label==="Unreachable"?"OFFLINE":starlink.label==="Unknown"?"UNKNOWN":"STANDBY",
dialogRole:connection!=="live"?"UNKNOWN":active===ROUTES.dialog?"PRIMARY":dialog.label==="Unreachable"?"OFFLINE":dialog.label==="Unknown"?"UNKNOWN":"STANDBY",
warning:connection==="live"&&starlink.label==="Unreachable"&&dialog.label==="Unreachable",
level:connection!=="live"?"unknown":(starlink.label==="Unreachable"&&dialog.label==="Unreachable")?"critical":(starlink.label==="Unreachable"||dialog.label==="Unreachable")?"warning":(starlink.level==="good"||dialog.level==="good")?"healthy":"info"
}}
function batteryModel(battery={},connection="disconnected"){
if(connection!=="live"||!Number.isFinite(battery.percent))
return{percent:null,power:"Unavailable",status:"Battery telemetry unavailable",statusLevel:"neutral",level:"neutral",border:"unknown"};

const onAC=battery.on_ac;
const raw=typeof battery.status==="string"?battery.status.toLowerCase().trim():"";
const power=onAC===true?"AC Connected":onAC===false?"On Battery":"Power source unknown";

let status="";
let statusLevel="neutral";

if(onAC===false){
  status="Running on hopes & dreams";
  statusLevel="bad";
}else if(onAC===true){
  if(raw==="charging"){
    status="Charging";
    statusLevel="good";
  }else if(["full","fully_charged","fully charged"].includes(raw)){
    status="Fully Charged";
    statusLevel="good";
  }else if(["not charging","not_charging"].includes(raw)){
    status="Plugged in · Not charging";
  }else if(battery.percent>=99){
    status="Battery full · AC connected";
    statusLevel="good";
  }
  // unknown / empty status stays "" so UI does not invent charge state
}

const level=onAC===false?(battery.percent<=15?"bad":"warn"):"good";
return{
  percent:battery.percent,
  power,
  status:status||(onAC===false?"Running on hopes & dreams":""),
  statusLevel,
  level,
  border:level==="bad"?"critical":level==="warn"?"warning":"healthy"
};
}
function metricModel(value,connection="disconnected"){if(connection!=="live"||!Number.isFinite(value))return{value:null,level:"neutral"};return{value,level:value>=90?"bad":value>=75?"warn":"good"}}
function resourcesBorder(resources,connection){
  if(connection!=="live")return"unknown";
  const levels=[resources.cpu.level,resources.ram.level,resources.disk.level];
  if(levels.includes("bad"))return"critical";
  if(levels.includes("warn"))return"warning";
  if(levels.every(level=>level==="good"))return"healthy";
  return"unknown";
}
function serviceOk(status){return status==="healthy"||status==="no_check"}
function serviceModel(services={},connection="disconnected"){const source=new Map((services.items||[]).map(item=>[item.name,item.status])),items=SERVICE_NAMES.map(name=>({name,status:connection==="live"?source.get(name)||"unknown":"unknown"})),healthy=items.filter(item=>serviceOk(item.status)).length;return{items,healthy,total:items.length,level:connection!=="live"?"neutral":healthy===items.length?"good":"warn",label:connection==="live"?`${healthy}/${items.length} Healthy`:`0/${items.length} Unknown`,border:connection!=="live"?"unknown":healthy===items.length?"healthy":items.some(item=>item.status==="unhealthy"||item.status==="stopped")?"critical":"warning"}}
function viewModel(result={}){const connectionState=["live","stale","disconnected"].includes(result.connection_state)?result.connection_state:"disconnected",payload=result.payload||{},resources={cpu:metricModel(payload.resources?.cpu_percent,connectionState),ram:metricModel(payload.resources?.ram_percent,connectionState),disk:metricModel(payload.resources?.disk_percent,connectionState)};return{connectionState,connectionLabel:connectionState==="live"?"LIVE":connectionState==="stale"?"STALE":"OFFLINE",node:payload.node||{},network:networkModel(payload.network,connectionState),battery:batteryModel(payload.battery,connectionState),resources,resourcesBorder:resourcesBorder(resources,connectionState),services:serviceModel(payload.services,connectionState)}}
return{SERVICE_NAMES,networkModel,batteryModel,metricModel,serviceModel,viewModel}});

if(typeof document!=="undefined"){
const $=selector=>document.querySelector(selector),P=globalThis.DashboardPresentation,loginView=$("#login-view"),dashboardView=$("#dashboard-view");
const formatUptime=seconds=>{if(!Number.isFinite(seconds))return"Unavailable";const d=Math.floor(seconds/86400),h=Math.floor(seconds%86400/3600),m=Math.floor(seconds%3600/60);return`${d?`${d}d `:""}${h}h ${m}m`},formatTime=value=>{const date=new Date(value);return Number.isNaN(date.getTime())?"Never":date.toLocaleString([],{dateStyle:"medium",timeStyle:"short"})};
const BORDER_LEVELS=["healthy","warning","critical","info","unknown"];
function setPanelBorder(id,level){const el=$("#"+id);if(!el)return;el.classList.add("status-border");for(const name of BORDER_LEVELS)el.classList.remove(`status-${name}`);el.classList.add(`status-${BORDER_LEVELS.includes(level)?level:"unknown"}`)}
function setText(id,value,level){const el=$("#"+id);el.textContent=value;if(level)el.className=level}function setStatus(id,item){const el=$("#"+id);el.className=`status ${item.level}`;el.replaceChildren(document.createElement("i"),document.createTextNode(item.label))}function setBar(id,item){const bar=$("#"+id+"-bar");setText(id,item.value===null?"—":`${item.value.toFixed(1)}%`,item.level);bar.className=`bar ${item.level}`;bar.querySelector("i").style.width=item.value===null?"0%":`${Math.min(100,Math.max(0,item.value))}%`}
function serviceRowClass(status){if(status==="healthy"||status==="no_check")return"status-healthy";if(status==="starting")return"status-warning";if(status==="unhealthy"||status==="stopped")return"status-critical";return""}
function render(result){const model=P.viewModel(result);setText("node-name",model.node.id&&model.node.id!=="unknown"?model.node.id:"Unknown host");setText("uptime",model.connectionState==="live"?formatUptime(model.node.uptime_seconds):"Unavailable");setText("last-update",result.received_at?formatTime(result.received_at):"Never");const badge=$("#connection-state");badge.className=`state-badge ${model.connectionState==="disconnected"?"offline":model.connectionState}`;setText("connection-label",model.connectionLabel);setStatus("starlink",model.network.starlink);setStatus("dialog",model.network.dialog);setText("starlink-role",model.network.starlinkRole,model.network.starlinkRole==="PRIMARY"?"network-role good":"network-role");setText("dialog-role",model.network.dialogRole,model.network.dialogRole==="PRIMARY"?"network-role good":"network-role");$("#network-warning").classList.toggle("hidden",!model.network.warning);setText("battery-percent",model.battery.percent===null?"—":`${model.battery.percent.toFixed(0)}%`,model.battery.level);setText("power-state",model.battery.power,model.battery.level);setText("battery-status",model.battery.status||(model.battery.percent===null?"Battery telemetry unavailable":""),model.battery.statusLevel);const batteryBar=$("#battery-bar");batteryBar.className=`bar ${model.battery.level}`;batteryBar.querySelector("i").style.width=model.battery.percent===null?"0%":`${model.battery.percent}%`;batteryBar.setAttribute("aria-valuenow",model.battery.percent??"");setBar("cpu",model.resources.cpu);setBar("ram",model.resources.ram);setBar("disk",model.resources.disk);setText("service-count",model.services.label,model.services.level);setPanelBorder("network-panel",model.network.level);setPanelBorder("power-panel",model.battery.border);setPanelBorder("resources-panel",model.resourcesBorder);setPanelBorder("services-panel",model.services.border);const list=$("#services");list.replaceChildren();for(const item of model.services.items){const row=document.createElement("div"),name=document.createElement("span"),state=document.createElement("strong");row.className=`service ${serviceRowClass(item.status)}`.trim();name.className="service-name";name.textContent=item.name;state.className=`service-status ${item.status}`;state.textContent=item.status==="no_check"?"Running":item.status.replaceAll("_"," ");row.append(name,state);list.append(row)}}
async function loadStatus(){const response=await fetch("/api/status",{cache:"no-store"});if(response.status===401){loginView.classList.remove("hidden");dashboardView.classList.add("hidden");return}if(!response.ok)throw new Error("Status unavailable");const result=await response.json();loginView.classList.add("hidden");dashboardView.classList.remove("hidden");render(result)}
$("#login-form").addEventListener("submit",async event=>{event.preventDefault();$("#login-error").textContent="";const response=await fetch("/api/auth/login",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({password:$("#password").value})});if(!response.ok){$("#login-error").textContent=response.status===429?"Too many attempts. Try again later.":"Sign-in failed.";return}$("#password").value="";await loadStatus()});$("#logout").addEventListener("click",async()=>{await fetch("/api/auth/logout",{method:"POST"});dashboardView.classList.add("hidden");loginView.classList.remove("hidden")});loadStatus().catch(()=>{loginView.classList.remove("hidden");dashboardView.classList.add("hidden")});setInterval(()=>loadStatus().catch(()=>{}),30000)}
