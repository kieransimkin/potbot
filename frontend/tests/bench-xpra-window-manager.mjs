/** CPU-only bounded Xpra window-list benchmark; not browser compositor FPS. */
import { performance } from 'node:perf_hooks';
import { projectXpraWindows } from '../src/lib/xpra-window-manager.ts';

const windows=Array.from({length:500},(_,i)=>({id:`xwin-${(i+1).toString(16)}`,
  title:i%3?`Terminal ${i}`:`Legacy Game ${i}`,width:640,height:480,x:0,y:0,generation:1}));
const hidden=windows.filter((_,i)=>i%3===0).map(x=>x.id);
const samples=[];
for(let trial=0;trial<500;trial++){
  const start=performance.now();
  projectXpraWindows(windows,hidden,trial%3===0?'legacy':trial%3===1?'terminal':'','all');
  samples.push(performance.now()-start);
}
samples.sort((a,b)=>a-b);
console.log(JSON.stringify({label:'Synthetic 500-window inventory, CPU only',
  samples:samples.length,
  medianMs:Number(samples[Math.floor(samples.length/2)].toFixed(3)),
  p95Ms:Number(samples[Math.floor(samples.length*.95)].toFixed(3)),
  gpuFpsMeasured:false, xpraNetworkMeasured:false},null,2));
