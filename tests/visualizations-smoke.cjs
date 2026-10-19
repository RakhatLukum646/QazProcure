const fs=require('fs'),vm=require('vm'),assert=require('assert');
class Node{constructor(tag='div'){this.tag=tag;this.children=[];this.style={};this.classList={toggle(){}};this.checked=false;this.textContent='';}append(...n){this.children.push(...n)}prepend(...n){this.children.unshift(...n)}before(){}replaceChildren(){this.children=[]}setAttribute(k,v){this[k]=v}addEventListener(){}}
const nodes=new Map();const get=id=>{if(!nodes.has(id))nodes.set(id,new Node());return nodes.get(id)};
const context={document:{createElement:t=>new Node(t),getElementById:get,querySelector:()=>new Node(),querySelectorAll:()=>[]},console};vm.createContext(context);vm.runInContext(fs.readFileSync('app/static/visualizations.js','utf8'),context);
const all=n=>[n,...n.children.filter(c=>c instanceof Node).flatMap(all)];
for(const lang of ['ru','kk'])for(const status of ['estimated','insufficient_peers']){
const example={target_kind:'planned_lot',planned_price:120,price_net:null,deviation_pct:null,contract_id:null,status,n_peers:5,unit:'шт',name_ru:'Бумага',name_kz:'Қағаз',fair_price:status==='estimated'?100:null,q25:90,q75:110};
context.input={request:{intent:'fair_price',language:lang,years:[2026],top_k:10},n_selected:1,n_evaluated:1,n_matches:status==='estimated'?1:0,examples:[example],limitations:['fair_price_is_statistical']};
vm.runInContext("renderAnalysis(input,{question:'Test'},'state')",context);
const elements=all(vm.runInContext('visual',context));const text=elements.map(n=>n.textContent).join(' ');
assert(text.includes(lang==='kk'?'Жоспарланған лот':'Планируемый лот'));
assert(!elements.some(n=>n.className==='range-actual'));
assert(!elements.some(n=>n.style.width==='NaN%'||n.style.left==='NaN%'));
assert(!text.includes('null'));if(lang==='kk'){assert(!text.includes('Ограничения метода'));assert(!text.includes('Проверено позиций'));}
if(status==='estimated')assert.equal(elements.filter(n=>n.className==='bar-row').length,1);
}
console.log('Visualization planned-lot RU/KK smoke checks passed');

