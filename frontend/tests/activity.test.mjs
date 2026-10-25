import { test } from 'node:test';
import assert from 'node:assert/strict';
import { projectJobs, projectJobEvents, projectModels, activityView, filterJobs } from '../src/lib/activity.ts';
const id='123e4567-e89b-12d3-a456-426614174000';
const taskId='123e4567-e89b-12d3-a456-426614174001';
const wrap=data=>({schema_version:1,request_id:id,data,error:null});

test('project actual job receipts and strip raw data, results and credentials',()=>{
  const jobs=projectJobs(wrap({items:[{id,episode_id:taskId,job_type:'model.generate',state:'running',
    progress:{stage:'calling',completed:1,total:3,private:'secret'},created_at:'2026-10-25T10:00:00Z',
    cancellation_requested:false,input:{password:'not exposed'},result:{token:'hidden'}}]}));
  assert.equal(jobs.length,1);
  assert.equal(jobs[0].completed,1);
  assert.equal(jobs[0].stage,'calling');
  assert.doesNotMatch(JSON.stringify(jobs),/secret|password|token/);
  assert.equal(projectJobs(wrap({items:[{id,episode_id:taskId,job_type:'model.generate',state:'arbitrary'}]})),null);
  assert.equal(projectJobs(wrap({items:[{id:'bad',episode_id:taskId,job_type:'a',state:'running'}]})),null);
});

test('project ordered events and detect gaps without exposing payloads',()=>{
  const events=projectJobEvents(wrap({items:[{job_id:id,sequence:12,event_type:'job.progress',recorded_at:'2026-10-25T12:00:00Z',data:{secret:'hidden'}},
    {job_id:id,sequence:14,event_type:'job.done',recorded_at:'2026-10-25T12:00:30Z'}],next_cursor:14,has_more:false}),11);
  assert.deepEqual(events.events.map(x=>x.sequence),[12,14]);
  assert.equal(events.gap,true);
  assert.ok(!JSON.stringify(events).includes('hidden'));
  assert.equal(projectJobEvents(wrap({items:[{job_id:id,sequence:11,event_type:'job.done'}],next_cursor:11,has_more:false}),11),null);
  assert.equal(projectJobEvents(wrap({items:[],next_cursor:10,has_more:false}),11),null);
  assert.equal(activityView([],events).transferTelemetry,false);
  assert.equal(activityView([],events).installTelemetry,false);
});

test('model listing represents catalogue not installed or verified model files',()=>{
  const models=projectModels(wrap({local:[{canonical_model:'Qwen/Qwen3-0.6B',role:'reasoning',
    qualified:false,deployment_configured:false,tokenizer_credentials:'SECRET'},
    {canonical_model:'Test/model',role:'coding',qualified:true,deployment_configured:false}]}));
  assert.equal(models.length,2);
  assert.equal(models[0].qualified,false);
  assert.equal(models[1].qualified,true);
  assert.equal(models[1].configured,false);
  assert.ok(!JSON.stringify(models).includes('SECRET'));
  assert.equal(projectModels(wrap({local:[{canonical_model:'../escaped'}]})),null);
});


test('active and completed job filters return only authoritative states', () => {
  const jobs=[{id:'a',type:'model.generate',stage:'think',state:'running'},
              {id:'b',type:'model.generate',stage:'completed',state:'succeeded'},
              {id:'c',type:'model.generate',stage:null,state:'indeterminate'}];
  assert.deepEqual(filterJobs(jobs,'','active').map(x=>x.id),['a']);
  assert.deepEqual(filterJobs(jobs,'','finished').map(x=>x.id),['b','c']);
  assert.deepEqual(filterJobs(jobs,'THINK','all').map(x=>x.id),['a']);
});
