import { test } from 'node:test';
import assert from 'node:assert/strict';
import { projectXpraWindows } from '../src/lib/xpra-window-manager.ts';

const win=(id,title,generation=1)=>({id,title,generation,width:640,height:480,x:0,y:0});

test('workspace window switcher searches titles and correctly separates minimized panes',()=>{
 const windows=[win('xwin-a','XBill'),win('xwin-b','xterm'),win('xwin-c','XCalc')];
 const all=projectXpraWindows(windows,['xwin-b'],'');
 assert.equal(all.total,3);assert.equal(all.minimized,1);
 assert.deepEqual(all.items.map(x=>x.title),['XBill','XCalc','xterm']);
 assert.deepEqual(projectXpraWindows(windows,['xwin-b'],'','minimized').items.map(x=>x.id),['xwin-b']);
 assert.deepEqual(projectXpraWindows(windows,['xwin-b'],'','visible').items.map(x=>x.id),['xwin-a','xwin-c']);
 assert.deepEqual(projectXpraWindows(windows,[],'calc').items.map(x=>x.title),['XCalc']);
 assert.equal(projectXpraWindows(windows,[],'bogus').items.length,0);
});

test('newer generation wins and a large inventory is safely capped',()=>{
 const previous=win('xwin-1','Old window',1), latest=win('xwin-1','New window',2);
 const list=projectXpraWindows([latest,previous],[],'');
 assert.equal(list.total,1);
 assert.equal(list.items[0].title,'New window');
 const many=Array.from({length:300},(_,i)=>win(`xwin-${i.toString(16)}`,`Application ${i}`));
 const projection=projectXpraWindows(many,[],'');
 assert.equal(projection.total,300);
 assert.equal(projection.items.length,256);
 assert.equal(projection.limited,true);
});
