const fs = require('node:fs');
const assert = require('node:assert/strict');
const yaml = fs.readFileSync('.github/workflows/project-status.yml', 'utf8').replace(/\r\n/g, '\n');
const script = yaml.split('          script: |\n')[1].split('\n').map(l => l.slice(12)).join('\n');
const execute = new (Object.getPrototypeOf(async function() {}).constructor)('github', 'context', 'core', script);
const pageInfo = {hasNextPage:false};
async function check({draft=false, state='open', merged=false, events=[], reviews=[], closedIssue=false, archived=false, requestedReviewers=[{login:'h'}], requestedTeams=[]}, expected) {
  const mutations = [];
  const pr = {number:55, draft, state, merged, base:{ref:'main'},user:{login:'author'},requested_reviewers:requestedReviewers,requested_teams:requestedTeams};
  const github = {rest:{pulls:{list:'list', get:async()=>({data:pr}), listReviews:'reviews'}},
    paginate:async(endpoint)=>endpoint==='reviews'?reviews:state==='open'?[pr]:[],
    graphql:async(q,v)=>{
      if(q.startsWith('mutation')) {mutations.push(v.status);return {};}
      if(q.includes('fields(first:100)')) return {user:{projectV2:{id:'p',fields:{pageInfo,nodes:[{id:'f',name:'Status',options:[{id:'progress',name:'In Progress'},{id:'waiting',name:'Waiting for review'},{id:'reviewed',name:'Reviewed'}]}]}}}};
      if(q.includes('items(first:100')) return {node:{items:{pageInfo,nodes:[{id:'item',isArchived:archived,content:{id:'issue',number:46,state:closedIssue?'CLOSED':'OPEN',repository:{nameWithOwner:'HusseinMoukalled/Buddy'}},fieldValueByName:{optionId:'backlog'}}]}}};
      if(q.includes('closingIssuesReferences')) return {repository:{pullRequest:{closingIssuesReferences:{pageInfo,nodes:[{id:'issue',state:closedIssue?'CLOSED':'OPEN',repository:{nameWithOwner:'HusseinMoukalled/Buddy'}}]}}}};
      if(q.includes('timelineItems')) return {repository:{pullRequest:{timelineItems:{pageInfo,nodes:events}}}};
      throw Error(q);
    }};
  await execute(github,{payload:{pull_request:{number:55},repository:{default_branch:'main'}}},{info:()=>{}});
  assert.deepEqual(mutations,expected?[expected]:[]);
}
(async()=>{
 const request={__typename:'ReviewRequestedEvent',createdAt:'2026-10-13T10:00:00Z'};
 await check({},'progress');
 await check({events:[request]},'waiting');
 await check({draft:true,events:[request]},'progress');
 const review = state => ({user:{login:'h'},state,submitted_at:'2026-10-13T11:00:00Z'});
 await check({events:[request],reviews:[review('APPROVED')]},'reviewed');
 await check({events:[request],reviews:[review('CHANGES_REQUESTED')]},'reviewed');
 await check({events:[request],reviews:[review('COMMENTED')]},'reviewed');
 await check({events:[request],reviews:[review('DISMISSED')]},'waiting');
 await check({events:[request],reviews:[{...review('COMMENTED'),user:{login:'author'}}]},'waiting');
 await check({events:[request,{...request,createdAt:'2026-10-13T12:00:00Z'}],reviews:[review('APPROVED')]},'waiting');
 await check({events:[request,{__typename:'ConvertToDraftEvent',createdAt:'2026-10-13T12:00:00Z'}],reviews:[review('APPROVED')]},'progress');
 await check({reviews:[review('COMMENTED')]},'reviewed');
 await check({state:'closed'},'progress');
 await check({state:'closed',merged:true},null);
 await check({closedIssue:true},null);
 await check({archived:true},null);
 // Historical requests must not keep a canceled review cycle waiting.
 const removed={__typename:'ReviewRequestRemovedEvent',createdAt:'2026-10-13T10:30:00Z'};
 const rerequest={...request,createdAt:'2026-10-13T12:00:00Z'};
 await check({events:[request,removed],requestedReviewers:[]},'progress');
 await check({events:[request,removed],requestedReviewers:[{login:'other'}]},'waiting');
 await check({events:[request,removed],requestedReviewers:[],requestedTeams:[{slug:'team'}]},'waiting');
 await check({events:[request,removed,rerequest]},'waiting');
 await check({events:[request,removed],reviews:[review('COMMENTED')],requestedReviewers:[]},'reviewed');
 await check({events:[request,rerequest,{...removed,createdAt:'2026-10-13T13:00:00Z'}],reviews:[review('APPROVED')],requestedReviewers:[]},'reviewed');
 await check({events:[request,removed,rerequest],reviews:[review('APPROVED')]},'waiting');
 console.log('22 mocked integration scenarios passed');
})().catch(e=>{console.error(e);process.exitCode=1});
