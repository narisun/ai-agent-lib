// Exercise the shipped panel with controlled responses; no server or model calls.
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
class Element {
  constructor() { this.events={}; this.children=[]; this.innerHTML=''; this.textContent=''; this.value=''; this.hidden=false; }
  addEventListener(n, f) { this.events[n]=f; }
  setAttribute() {}
  focus() {}
  replaceChildren(...children) { this.children=children; this.innerHTML=''; this.textContent=''; this.value=children[0]?.value || ''; }
}
const names=['select','[role="status"]','article','[data-meta]','[data-older]','[data-details]','[data-close]','[data-refresh]'];
const nodes=Object.fromEntries(names.map(n=>[n,new Element()]));
const panel=new Element(); panel.querySelector=n=>nodes[n];
let click;
const document={
  readyState:'complete', hidden:false,
  getElementById:()=>null,
  createElement:n=>n==='aside'?panel:new Element(),
  body:{append(){},classList:{add(){},remove(){}}},
  addEventListener(n,f){if(n==='click')click=f},querySelector:()=>null
};
let loads=[];
const versions=['A','B'].map((id,i)=>({id,version:2-i,title:id,created_at:'2026-10-16',request_id:'request'}));
async function fetch(url,{signal}) {
  if(url.endsWith('/documents')) return {ok:true,json:async()=>({items:versions,next_before:null})};
  loads.push(url);
  if(url.endsWith('/B')) return new Promise((resolve,reject)=>signal.addEventListener('abort',()=>reject(new Error('aborted'))));
  return {ok:true,text:async()=>'MEMO A'};
}
vm.runInNewContext(fs.readFileSync(require('node:path').join(__dirname, '../src/agentcraft2_ui/assets/memos.js'),'utf8'),{
  document,location:{pathname:'/chat/thread/review-thread'},fetch,AbortController,setInterval(){},Date,Map,encodeURIComponent
});
(async()=>{
  click({target:{closest:()=>true},preventDefault(){}});
  await new Promise(setImmediate);
  assert.equal(nodes.article.innerHTML,'MEMO A');
  nodes.select.value='B';
  const pending=nodes.select.events.change();
  await new Promise(setImmediate);
  nodes.select.value='A';
  await nodes.select.events.change();
  await pending;
  assert.equal(nodes.article.innerHTML,'MEMO A');
  assert.equal(nodes.select.value,'A');
  assert.equal(loads.length,3);
  assert.equal(nodes['[data-details]'].hidden, false);
})();
