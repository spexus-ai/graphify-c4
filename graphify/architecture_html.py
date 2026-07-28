"""C4 explorer rendered with Graphify's standard vis-network graph renderer."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from graphify.exporters.html import _html_styles
from graphify.paths import write_text_atomic


_TEMPLATE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Graphify C4 Architecture</title>
<script src="https://unpkg.com/vis-network@9.1.6/standalone/umd/vis-network.min.js"
        integrity="sha384-Ux6phic9PEHJ38YtrijhkzyJ8yQlH8i/+buBR8s3mAZOJrP1gwyvAcIYl3GWtpX1"
        crossorigin="anonymous"></script>
__STYLES__
<style>
#controls { padding: 12px; border-bottom: 1px solid #2a2a4e; display: grid; gap: 9px; }
#controls label { display: grid; gap: 4px; color: #aaa; font-size: 12px; }
#controls select, #controls button { width: 100%; background: #0f0f1a; border: 1px solid #3a3a5e; color: #e0e0e0; border-radius: 6px; padding: 7px 9px; font: inherit; }
#controls button { cursor: pointer; } #controls button:hover { border-color: #4E79A7; }
#mode-note { color: #777; font-size: 11px; line-height: 1.4; }
.edge-key { display: flex; gap: 8px; align-items: center; color: #aaa; font-size: 12px; margin: 4px 0; }
.edge-key i { width: 22px; border-top: 2px solid #22c55e; } .edge-key i.observed { border-top-color: #f59e0b; border-top-style: dashed; }
.drill-button { margin-top: 8px; width: 100%; background: #243455; color: #e0e0e0; border: 1px solid #4E79A7; border-radius: 5px; padding: 6px; cursor: pointer; }
</style></head>
<body>
<div id="graph"></div>
<div id="sidebar">
  <div id="controls">
    <label>Level <select id="level"><option value="context">Context</option><option value="container" selected>Container</option><option value="component">Component</option><option value="code">Code</option></select></label>
    <label>Focus <select id="focus"></select></label>
    <button id="reset">Reset focus</button>
    <div id="mode-note">Drag nodes to arrange them. Scroll to zoom. Double-click a C4 node to drill down. Component shapes and colours show their architectural layer.</div>
  </div>
  <div id="search-wrap"><input id="search" type="text" placeholder="Search visible nodes..." autocomplete="off"><div id="search-results"></div></div>
  <div id="info-panel"><h3>Node Info</h3><div id="info-content"><span class="empty">Click a node to inspect it</span></div></div>
  <div id="legend-wrap"><h3>Relationship evidence</h3><div class="edge-key"><i></i>Declared contract</div><div class="edge-key"><i class="observed"></i>Observed code evidence</div><div id="layer-legend-wrap"><h3 style="margin-top:18px">Layers</h3><div id="layer-key"></div></div><h3 style="margin-top:18px">Visible nodes</h3><div id="legend-controls"><label><input type="checkbox" id="select-all-cb" checked> Select All</label></div><div id="node-filter"></div></div>
  <div id="stats"></div>
</div>
<script>
const payload = __PAYLOAD__;
const levelTypes = {context:new Set(['person','software_system','external_system']),container:new Set(['container','datastore']),component:new Set(['component']),code:new Set(['code'])};
const nodeColors = {person:'#be123c',software_system:'#4E79A7',external_system:'#7c3aed',container:'#0284c7',datastore:'#0f766e',component:'#59A14F',code:'#9C755F'};
const shapeGlyph = {diamond:'◆',square:'■',triangle:'▲',triangleDown:'▼',star:'★',hexagon:'⬢',circle:'●',ellipse:'●',box:'■'};
const maxGraphNodes = 180, maxComponentNodes = 650, maxFocusedCodeNodes = 750, byId = new Map(payload.elements.map(item => [item.id,item]));
const level = document.querySelector('#level'), focus = document.querySelector('#focus'), info = document.querySelector('#info-content'), stats = document.querySelector('#stats'), search = document.querySelector('#search'), results = document.querySelector('#search-results'), nodeFilter = document.querySelector('#node-filter'), selectAll = document.querySelector('#select-all-cb'), layerKey = document.querySelector('#layer-key'), layerLegendWrap = document.querySelector('#layer-legend-wrap');
const nodesDS = new vis.DataSet(), edgesDS = new vis.DataSet();
const network = new vis.Network(document.querySelector('#graph'), {nodes:nodesDS,edges:edgesDS}, {
  physics:{enabled:true,solver:'forceAtlas2Based',forceAtlas2Based:{gravitationalConstant:-60,centralGravity:0.005,springLength:120,springConstant:0.08,damping:0.4,avoidOverlap:0.8},stabilization:{iterations:200,fit:true}},
  interaction:{hover:true,tooltipDelay:100,hideEdgesOnDrag:true,navigationButtons:false,keyboard:false,dragNodes:true},
  nodes:{shape:'dot',borderWidth:1.5}, edges:{smooth:{type:'continuous',roundness:0.2},selectionWidth:3}
});
let currentNodes = [], currentRelations = [], hiddenNodes = new Set(), pinnedNodeId = null;
function esc(value) { return String(value ?? '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;'); }
function visual(item) { const configured=(item&&item.visual)||{},color=configured.color||nodeColors[item.c4_type]||'#4E79A7'; return {color,shape:configured.shape||'dot'}; }
function descendant(id, parent) { let current=id,seen=new Set; while(current&&!seen.has(current)){if(current===parent)return true;seen.add(current);current=(byId.get(current)||{}).parent;} return false; }
function ancestor(id, types) { let current=id,seen=new Set; while(current&&!seen.has(current)){seen.add(current);const item=byId.get(current);if(!item)return null;if(types.has(item.c4_type))return current;current=item.parent;} return null; }
function nextLevel(type) { return ({software_system:'container',external_system:'container',container:'component',component:'code',datastore:'component'})[type] || null; }
function visible() { const wanted=levelTypes[level.value], selected=focus.value; return payload.elements.filter(item => wanted.has(item.c4_type) && (!selected || descendant(item.id,selected))); }
function packagePath(item) { return String((item||{}).source_file||'').split('/').slice(0,-1).join('/'); }
function relationships(selected) { const ids=new Set(selected.map(item=>item.id)), types=levelTypes[level.value], source=level.value==='code'?payload.observed_code_relations:payload.observed_relations, all=[]; for(const relation of source)all.push({...relation,origin:'observed',evidence_count:relation.evidence.length}); for(const relation of payload.declared_relations)all.push({...relation,origin:'declared',evidence_count:0}); const merged=new Map; for(const relation of all){const sourceId=ancestor(relation.source,types),targetId=ancestor(relation.target,types);if(!sourceId||!targetId||sourceId===targetId||!ids.has(sourceId)||!ids.has(targetId))continue;const sourceItem=byId.get(sourceId),targetItem=byId.get(targetId),importBridge=relation.origin==='observed'&&(relation.evidence||[]).some(item=>item.resolution==='go_import'||item.resolution==='go_import_type'),crossPackage=level.value==='code'&&packagePath(sourceItem)!==packagePath(targetItem);const key=[sourceId,targetId,relation.kind,relation.origin].join('|'),prior=merged.get(key)||{source:sourceId,target:targetId,kind:relation.kind,origin:relation.origin,evidence_count:0,bridge:false};prior.evidence_count+=relation.evidence_count;prior.bridge=prior.bridge||importBridge||crossPackage;merged.set(key,prior);} return [...merged.values()].sort((a,b)=>a.source.localeCompare(b.source)||a.target.localeCompare(b.target)||a.kind.localeCompare(b.kind)); }
function chooseSubgraph(elements, relations, pinnedId, maxNodes) { if(elements.length<=maxNodes)return {elements,relations,trimmed:false,bridgeCount:0}; const score=new Map(elements.map(item=>[item.id,0])), adjacency=new Map(elements.map(item=>[item.id,[]])); for(const relation of relations){const weight=relation.evidence_count+(relation.bridge?4:0);score.set(relation.source,(score.get(relation.source)||0)+weight);score.set(relation.target,(score.get(relation.target)||0)+weight);(adjacency.get(relation.source)||[]).push(relation);(adjacency.get(relation.target)||[]).push(relation);} const ranked=[...elements].sort((a,b)=>(score.get(b.id)-score.get(a.id))||a.id.localeCompare(b.id)),bridgeRank=(a,b)=>((score.get(b.source)+score.get(b.target))-(score.get(a.source)+score.get(a.target)))||b.evidence_count-a.evidence_count||a.source.localeCompare(b.source),ids=new Set, frontier=new Set, add=id=>{if(ids.size>=maxNodes||ids.has(id))return false;ids.add(id);for(const relation of adjacency.get(id)||[])frontier.add(relation);return true;},growConnected=()=>{while(ids.size<maxNodes){let best=null;for(const relation of frontier){const sourceSeen=ids.has(relation.source),targetSeen=ids.has(relation.target);if(sourceSeen===targetSeen)continue;if(!best||bridgeRank(relation,best)<0)best=relation;}if(!best)break;add(ids.has(best.source)?best.target:best.source);}}; if(pinnedId&&score.has(pinnedId)){add(pinnedId);for(const relation of (adjacency.get(pinnedId)||[]).sort(bridgeRank)){add(relation.source);add(relation.target);}growConnected();}else{for(const item of ranked){if(ids.size>=maxNodes)break;if(add(item.id))growConnected();}} const keptRelations=relations.filter(relation=>ids.has(relation.source)&&ids.has(relation.target)); return {elements:elements.filter(item=>ids.has(item.id)),relations:keptRelations,trimmed:true,bridgeCount:keptRelations.filter(item=>item.bridge).length}; }
function renderFocus() { const previous=focus.value; focus.innerHTML='<option value="">Entire system</option>'+payload.elements.filter(item=>item.c4_type!=='code').map(item=>`<option value="${esc(item.id)}">${esc(item.name)} — ${esc(item.id)}</option>`).join(''); focus.value=byId.has(previous)?previous:''; }
function updateSelectAllState() { const hidden=hiddenNodes.size; selectAll.checked=hidden===0; selectAll.indeterminate=hidden>0&&hidden<currentNodes.length; }
function applyNodeFilter() { nodesDS.update(currentNodes.map(item=>({id:item.id,hidden:hiddenNodes.has(item.id)}))); edgesDS.update(currentRelations.map((relation,index)=>({id:index,hidden:hiddenNodes.has(relation.source)||hiddenNodes.has(relation.target)}))); updateSelectAllState(); }
function renderNodeFilter() { nodeFilter.innerHTML=''; for(const item of currentNodes){const row=document.createElement('div'),checkbox=document.createElement('input'),dot=document.createElement('div'),label=document.createElement('span'),style=visual(item);row.className='legend-item';checkbox.type='checkbox';checkbox.className='legend-cb';checkbox.checked=!hiddenNodes.has(item.id);dot.className='legend-dot';dot.style.background=style.color;label.className='legend-label';label.textContent=item.name;checkbox.onchange=()=>{if(checkbox.checked)hiddenNodes.delete(item.id);else hiddenNodes.add(item.id);row.classList.toggle('dimmed',!checkbox.checked);applyNodeFilter();};row.append(checkbox,dot,label);row.onclick=event=>{if(event.target===checkbox)return;checkbox.checked=!checkbox.checked;checkbox.dispatchEvent(new Event('change'));};nodeFilter.append(row);} updateSelectAllState(); }
function renderLayerLegend() { const layers=new Map;for(const item of currentNodes){if(!item.layer)continue;layers.set(item.layer,visual(item));}layerLegendWrap.style.display=layers.size?'block':'none';layerKey.innerHTML=[...layers.entries()].sort((a,b)=>a[0].localeCompare(b[0])).map(([name,style])=>`<div class="legend-item"><span style="color:${esc(style.color)};width:18px;text-align:center">${shapeGlyph[style.shape]||'●'}</span><span class="legend-label">${esc(name)}</span></div>`).join(''); }
function updateGraph() { const allElements=visible(), allRelations=relationships(allElements), focusedComponent=(byId.get(focus.value)||{}).c4_type==='component', nodeLimit=level.value==='component'?maxComponentNodes:(level.value==='code'&&focusedComponent?maxFocusedCodeNodes:maxGraphNodes), subset=chooseSubgraph(allElements,allRelations,pinnedNodeId,nodeLimit); currentNodes=subset.elements;currentRelations=subset.relations;hiddenNodes=new Set(); const degree=new Map(currentNodes.map(item=>[item.id,0]));for(const relation of currentRelations){degree.set(relation.source,(degree.get(relation.source)||0)+1);degree.set(relation.target,(degree.get(relation.target)||0)+1);} const maxDegree=Math.max(...degree.values(),1); nodesDS.clear();edgesDS.clear();nodesDS.add(currentNodes.map(item=>{const style=visual(item);return {id:item.id,label:item.name,shape:style.shape,color:{background:style.color,border:style.color,highlight:{background:'#ffffff',border:style.color}},size:10+30*(degree.get(item.id)/maxDegree),font:{size:degree.get(item.id)>=maxDegree*.15?12:0,color:'#ffffff'},title:esc(`${item.name}\n${item.c4_type}${item.layer?` · ${item.layer}`:''}\n${item.source_file||item.id}`),_item:item,_degree:degree.get(item.id)};})); edgesDS.add(currentRelations.map((relation,index)=>({id:index,from:relation.source,to:relation.target,label:'',title:esc(`${relation.kind} (${relation.origin})${relation.evidence_count?` — ${relation.evidence_count} evidence`:''}`),dashes:relation.origin==='observed',width:relation.origin==='declared'?2:1,color:{color:relation.origin==='declared'?'#22c55e':'#f59e0b',opacity:relation.origin==='declared'?.85:.7},arrows:{to:{enabled:true,scaleFactor:.5}},_relation:relation}))); renderNodeFilter();renderLayerLegend(); info.innerHTML='<span class="empty">Click a node to inspect it</span>'; results.style.display='none'; search.value=''; stats.innerHTML=`${allElements.length} ${esc(level.value)} nodes &middot; ${allRelations.length} relationships${subset.trimmed?` &middot; showing ${subset.elements.length} nodes${subset.bridgeCount?` including ${subset.bridgeCount} cross-package bridges`:''}${pinnedNodeId?' plus selected node neighbors':''}`:''}`; network.setOptions({physics:{enabled:true}});network.stabilize(200);network.once('stabilizationIterationsDone',()=>network.setOptions({physics:{enabled:false}})); }
function showInfo(nodeId) { const node=nodesDS.get(nodeId);if(!node)return;const item=node._item,neighbors=network.getConnectedNodes(nodeId),neighborItems=neighbors.map(id=>{const neighbor=nodesDS.get(id);return `<span class="neighbor-link" style="border-left-color:${esc(neighbor.color.background)}" data-nid="${esc(id)}">${esc(neighbor.label)}</span>`;}).join(''),next=nextLevel(item.c4_type); info.innerHTML=`<div class="field"><b>${esc(item.name)}</b></div><div class="field">Type: ${esc(item.c4_type)}</div>${item.layer?`<div class="field">Layer: ${esc(item.layer)}</div>`:''}<div class="field">Source: ${esc(item.source_file||'declared')}</div><div class="field">Degree: ${node._degree}</div>${next?'<button class="drill-button" id="drill">Drill down</button>':''}${neighbors.length?`<div class="field" style="margin-top:8px;color:#aaa;font-size:11px">Neighbors (${neighbors.length})</div><div id="neighbors-list">${neighborItems}</div>`:''}`; const button=document.querySelector('#drill');if(button)button.onclick=()=>drill(item.id); }
function drill(id) { const item=byId.get(id),next=item&&nextLevel(item.c4_type);if(!next)return;focus.value=item.id;level.value=next;updateGraph(); }
network.on('click',params=>{if(params.nodes.length){const nodeId=params.nodes[0];if(pinnedNodeId!==nodeId){pinnedNodeId=nodeId;updateGraph();}showInfo(nodeId);}else info.innerHTML='<span class="empty">Click a node to inspect it</span>';});network.on('doubleClick',params=>{if(params.nodes.length)drill(params.nodes[0]);});
document.addEventListener('click',event=>{const link=event.target.closest('.neighbor-link');if(link)showInfo(link.dataset.nid);});
search.addEventListener('input',()=>{const query=search.value.toLowerCase().trim();results.innerHTML='';if(!query){results.style.display='none';return;}const matches=currentNodes.filter(item=>item.name.toLowerCase().includes(query)).slice(0,20);if(!matches.length){results.style.display='none';return;}results.style.display='block';for(const item of matches){const entry=document.createElement('div'),style=visual(item);entry.className='search-item';entry.textContent=item.name;entry.style.borderLeft=`3px solid ${style.color}`;entry.onclick=()=>{pinnedNodeId=item.id;updateGraph();network.focus(item.id,{scale:1.5,animation:true});network.selectNodes([item.id]);showInfo(item.id);results.style.display='none';search.value='';};results.append(entry);}});
selectAll.addEventListener('change',()=>{hiddenNodes=selectAll.checked?new Set():new Set(currentNodes.map(item=>item.id));applyNodeFilter();renderNodeFilter();});level.onchange=()=>{pinnedNodeId=null;updateGraph();};focus.onchange=()=>{pinnedNodeId=null;updateGraph();};document.querySelector('#reset').onclick=()=>{focus.value='';pinnedNodeId=null;updateGraph();};renderFocus();updateGraph();
</script></body></html>"""


def write_architecture_html(projection: dict[str, Any], output_path: Path) -> None:
    """Write a C4 explorer using the same vis-network renderer as graph.html."""
    payload = {
        "elements": projection.get("elements", []),
        "observed_relations": projection.get("observed_relations", []),
        "observed_code_relations": projection.get("observed_code_relations", []),
        "declared_relations": projection.get("declared_relations", []),
    }
    encoded = json.dumps(payload, ensure_ascii=False).replace("<", "\\u003c")
    html = _TEMPLATE.replace("__STYLES__", _html_styles()).replace("__PAYLOAD__", encoded)
    write_text_atomic(output_path, html)
