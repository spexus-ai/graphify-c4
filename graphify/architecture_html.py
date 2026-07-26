"""Self-contained interactive browser view for a Graphify C4 projection."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from graphify.paths import write_text_atomic


_TEMPLATE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>C4 Architecture</title>
<style>
:root { color-scheme: light dark; font: 15px system-ui, sans-serif; }
body { margin: 0; background: #f6f8fb; color: #18212f; }
header { padding: 20px 28px; background: #172033; color: #fff; }
header h1 { margin: 0 0 4px; font-size: 22px; } header p { margin: 0; opacity: .8; }
main { padding: 22px 28px; max-width: 1500px; margin: auto; }
.controls { display: flex; flex-wrap: wrap; gap: 12px; align-items: end; margin-bottom: 20px; }
label { display: grid; gap: 5px; font-weight: 600; } select, button { font: inherit; padding: 7px 10px; border-radius: 6px; border: 1px solid #b8c2d3; background: #fff; color: #18212f; }
button { cursor: pointer; } .summary { color: #526174; margin: 0 0 14px; }
.cards { display: grid; grid-template-columns: repeat(auto-fill, minmax(230px, 1fr)); gap: 12px; }
.card { border: 1px solid #cbd5e1; border-left: 5px solid #3867d6; border-radius: 8px; background: #fff; padding: 12px; cursor: pointer; text-align: left; min-height: 88px; }
.card:hover { box-shadow: 0 2px 10px #17203324; } .type { color: #526174; font-size: 12px; text-transform: uppercase; font-weight: 700; } .name { margin: 6px 0 3px; font-weight: 700; } .id { color: #66758a; font: 12px ui-monospace, SFMono-Regular, monospace; overflow-wrap: anywhere; }
section { margin-top: 30px; } h2 { font-size: 18px; } table { width: 100%; border-collapse: collapse; background: #fff; border: 1px solid #cbd5e1; } th, td { padding: 9px 10px; border-bottom: 1px solid #e2e8f0; text-align: left; vertical-align: top; } th { background: #edf2f7; } .observed { color: #9c3b00; } .declared { color: #166534; } .empty { padding: 18px; background: #fff; border: 1px dashed #94a3b8; border-radius: 8px; }
@media (prefers-color-scheme: dark) { body { background: #0d1524; color: #e5e7eb; } header { background: #111827; } select, button, .card, table, .empty { background: #162033; color: #e5e7eb; border-color: #334155; } th { background: #1d2a40; } th, td { border-color: #334155; } .summary,.type,.id { color: #a8b6c8; } }
</style></head><body>
<header><h1>C4 Architecture</h1><p>Declared contract and observed Graphify code evidence</p></header>
<main><div class="controls"><label>Level <select id="level"><option value="context">Context</option><option value="container" selected>Container</option><option value="component">Component</option><option value="code">Code</option></select></label><label>Focus <select id="focus"></select></label><button id="reset">Reset focus</button></div><p class="summary" id="summary"></p><div class="cards" id="cards"></div><section><h2>Relationships</h2><div id="relationships"></div></section></main>
<script>const payload = __PAYLOAD__;
const levelTypes = {context:new Set(['person','software_system','external_system']),container:new Set(['container','datastore']),component:new Set(['component']),code:new Set(['code'])};
const byId = new Map(payload.elements.map(x => [x.id,x]));
const level = document.querySelector('#level'), focus = document.querySelector('#focus'), cards = document.querySelector('#cards'), summary = document.querySelector('#summary'), relationshipBox = document.querySelector('#relationships');
function desc(id, ancestor) { let current=id, seen=new Set; while(current && !seen.has(current)) { if(current===ancestor) return true; seen.add(current); current=(byId.get(current)||{}).parent; } return false; }
function ancestor(id, types) { let current=id, seen=new Set; while(current && !seen.has(current)) { seen.add(current); const item=byId.get(current); if(!item) return null; if(types.has(item.c4_type)) return current; current=item.parent; } return null; }
function nextLevel(type) { return ({software_system:'container',container:'component',component:'code',datastore:'component'})[type] || 'component'; }
function visible() { const wanted=levelTypes[level.value], selected=focus.value; return payload.elements.filter(x => wanted.has(x.c4_type) && (!selected || desc(x.id,selected))); }
function rolledRelations(selected) { const ids=new Set(selected.map(x=>x.id)), types=levelTypes[level.value], all=[]; for(const r of payload.observed_relations) all.push({...r, origin:'observed', evidence_count:r.evidence.length}); for(const r of payload.declared_relations) all.push({...r, origin:'declared', evidence_count:0}); const merged=new Map; for(const r of all) { const s=ancestor(r.source,types), t=ancestor(r.target,types); if(!s||!t||s===t||!ids.has(s)||!ids.has(t)) continue; const key=[s,t,r.kind,r.origin].join('|'); const prior=merged.get(key)||{source:s,target:t,kind:r.kind,origin:r.origin,evidence_count:0}; prior.evidence_count+=r.evidence_count; merged.set(key,prior); } return [...merged.values()].sort((a,b)=>a.source.localeCompare(b.source)||a.target.localeCompare(b.target)||a.kind.localeCompare(b.kind)); }
function renderFocus() { const before=focus.value; focus.innerHTML='<option value="">Entire system</option>'+payload.elements.map(x=>`<option value="${x.id}">${x.name} — ${x.id}</option>`).join(''); focus.value=byId.has(before)?before:''; }
function render() { const selected=visible(), rels=rolledRelations(selected); summary.textContent=`${selected.length} ${level.value} element(s), ${rels.length} visible relationship(s). Click a card to drill down.`; cards.innerHTML=selected.length?selected.map(x=>`<button class="card" data-id="${x.id}"><div class="type">${x.c4_type}</div><div class="name">${x.name}</div><div class="id">${x.id}</div></button>`).join(''):'<div class="empty">No elements at this level and focus.</div>'; for(const card of cards.querySelectorAll('[data-id]')) card.onclick=()=>{ const e=byId.get(card.dataset.id); focus.value=e.id; level.value=nextLevel(e.c4_type); render(); }; relationshipBox.innerHTML=rels.length?`<table><thead><tr><th>Source</th><th>Relationship</th><th>Target</th><th>Evidence</th></tr></thead><tbody>${rels.map(r=>`<tr><td>${r.source}</td><td class="${r.origin}">${r.kind} (${r.origin})</td><td>${r.target}</td><td>${r.evidence_count||'—'}</td></tr>`).join('')}</tbody></table>`:'<div class="empty">No relationships at this zoom level.</div>'; }
renderFocus(); level.onchange=render; focus.onchange=render; document.querySelector('#reset').onclick=()=>{focus.value='';render()}; render();
</script></body></html>"""


def write_architecture_html(projection: dict[str, Any], output_path: Path) -> None:
    """Write an offline, browser-ready C4 explorer without external assets."""
    payload = {
        "elements": projection.get("elements", []),
        "observed_relations": projection.get("observed_relations", []),
        "declared_relations": projection.get("declared_relations", []),
    }
    encoded = json.dumps(payload, ensure_ascii=False).replace("<", "\\u003c")
    write_text_atomic(output_path, _TEMPLATE.replace("__PAYLOAD__", encoded))
