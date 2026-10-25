const test=require("node:test"),assert=require("node:assert/strict"),{viewModel}=require("../public/dashboard.js");
const names=["IMFS Integration","IMFS M1","IMFS M2","IMFS M3","IMFS M4","Ollama","IMFS Integration Backup (Oracle)"];
const telemetry=(overrides={})=>({connection_state:"live",received_at:"2026-10-12T00:00:00Z",payload:{node:{id:"workstation-07-5d",uptime_seconds:3600},network:{active_route:"starlink-mini",starlink:"reachable",dialog:"reachable"},battery:{percent:88,on_ac:true,status:"unknown"},resources:{cpu_percent:20,ram_percent:50,disk_percent:80},services:{items:names.map(name=>({name,status:name==="Ollama"?"no_check":"healthy",...(name==="IMFS Integration Backup (Oracle)"?{role:"backup"}:{})}))},...overrides}});
test("Starlink active and Dialog reachable",()=>{const m=viewModel(telemetry());assert.equal(m.network.starlink.label,"Active");assert.equal(m.network.dialog.label,"Reachable")});
test("Dialog active and Starlink unreachable",()=>{const m=viewModel(telemetry({network:{active_route:"dialog-ethernet",starlink:"unreachable",dialog:"reachable"}}));assert.equal(m.network.dialog.label,"Active");assert.equal(m.network.starlink.label,"Unreachable")});
test("Starlink active and Dialog unreachable",()=>{const m=viewModel(telemetry({network:{active_route:"starlink-mini",starlink:"reachable",dialog:"unreachable"}}));assert.equal(m.network.starlink.label,"Active");assert.equal(m.network.dialog.label,"Unreachable")});
test("both connections unreachable produces warning",()=>{const m=viewModel(telemetry({network:{active_route:"unknown",starlink:"unreachable",dialog:"unreachable"}}));assert.equal(m.network.warning,true);assert.equal(m.network.starlink.label,"Unreachable");assert.equal(m.network.dialog.label,"Unreachable")});
test("unknown route does not promote reachable connections",()=>{const m=viewModel(telemetry({network:{active_route:"unknown",starlink:"reachable",dialog:"reachable"}}));assert.equal(m.network.starlink.label,"Reachable");assert.equal(m.network.dialog.label,"Reachable")});
test("stale telemetry blanks Dell services but keeps Oracle backup status",()=>{
  const value=telemetry();
  value.connection_state="stale";
  const m=viewModel(value);
  assert.equal(m.connectionLabel,"STALE");
  assert.equal(m.network.starlink.label,"Unknown");
  assert.equal(m.battery.percent,null);
  assert.equal(m.services.items.find(x=>x.name==="IMFS Integration").status,"unknown");
  assert.equal(m.services.items.find(x=>x.name==="IMFS Integration Backup (Oracle)").status,"healthy");
  assert.equal(m.services.healthy,1);
});
test("disconnected host is offline with unavailable telemetry",()=>{const m=viewModel({connection_state:"disconnected",payload:null});assert.equal(m.connectionLabel,"OFFLINE");assert.equal(m.resources.cpu.value,null);assert.equal(m.services.label,"0/7 Unknown")});
test("missing battery and resources remain unavailable",()=>{const m=viewModel(telemetry({battery:{},resources:{}}));assert.equal(m.battery.percent,null);assert.equal(m.battery.power,"Unavailable");assert.equal(m.resources.cpu.value,null);assert.equal(m.resources.ram.value,null);assert.equal(m.resources.disk.value,null)});
test("unhealthy service states and count are preserved",()=>{const m=viewModel(telemetry({services:{items:[{name:names[0],status:"healthy"},{name:names[1],status:"starting"},{name:names[2],status:"stopped"},{name:names[3],status:"unhealthy"},{name:names[4],status:"unknown"},{name:names[5],status:"no_check"},{name:names[6],status:"healthy",role:"backup"}]}}));assert.equal(m.services.label,"3/7 Healthy");assert.deepEqual(m.services.items.map(x=>x.status),["healthy","starting","stopped","unhealthy","unknown","no_check","healthy"]);assert.equal(m.services.items[6].role,"backup")});
test("Ollama running without healthcheck stays Running label but counts as healthy",()=>{
  const running=viewModel(telemetry());
  assert.equal(running.services.items.find(x=>x.name==="Ollama").status,"no_check");
  assert.equal(running.services.healthy,7);
  assert.equal(running.services.label,"7/7 Healthy");
  assert.equal(running.services.border,"healthy");
  assert.equal(running.services.level,"good");
  const stopped=viewModel(telemetry({services:{items:[...names.slice(0,5).map(name=>({name,status:"healthy"})),{name:"Ollama",status:"stopped"},{name:"IMFS Integration Backup (Oracle)",status:"healthy",role:"backup"}]}}));
  assert.equal(stopped.services.items.find(x=>x.name==="Ollama").status,"stopped");
  assert.equal(stopped.services.healthy,6);
  assert.equal(stopped.services.border,"critical");
  const missing=viewModel(telemetry({services:{items:names.slice(0,5).map(name=>({name,status:"healthy"}))}}));
  assert.equal(missing.services.items.find(x=>x.name==="Ollama").status,"unknown");
  assert.equal(missing.services.items.find(x=>x.name==="IMFS Integration Backup (Oracle)").status,"unknown");
  assert.equal(missing.services.healthy,5);
});
test("IMFS services still render when Ollama and backup are present",()=>{const m=viewModel(telemetry());assert.deepEqual(m.services.items.map(x=>x.name),names);assert.equal(m.services.items[0].status,"healthy");assert.equal(m.services.items[6].role,"backup")});
test("battery charge labels require explicit status telemetry",()=>{assert.equal(viewModel(telemetry({battery:{percent:96,on_ac:true,status:"unknown"}})).battery.status,"");assert.equal(viewModel(telemetry({battery:{percent:70,on_ac:true,status:"charging"}})).battery.status,"Charging");assert.equal(viewModel(telemetry({battery:{percent:100,on_ac:true,status:"full"}})).battery.status,"Fully Charged")});
test("Oracle backup stays visible when Dell connection is offline",()=>{
  const m=viewModel({
    connection_state:"disconnected",
    received_at:null,
    payload:{
      node:{id:"unknown",uptime_seconds:null},
      network:{active_route:"unknown",starlink:"unknown",dialog:"unknown"},
      battery:{},
      resources:{},
      services:{items:[{name:"IMFS Integration Backup (Oracle)",status:"healthy",role:"backup"}]}
    }
  });
  assert.equal(m.connectionLabel,"OFFLINE");
  assert.equal(m.services.items.find(x=>x.name==="IMFS Integration").status,"unknown");
  assert.equal(m.services.items.find(x=>x.name==="IMFS Integration Backup (Oracle)").status,"healthy");
  assert.equal(m.services.label,"1/7 Healthy");
});
