const fs = require('node:fs');
const assert = require('node:assert/strict');
const yaml = fs.readFileSync('.github/workflows/project-status.yml', 'utf8').replace(/\r\n/g, '\n');
// Configuration regression: event/review/manual runs for one PR serialize together,
// but distinct PRs cannot replace each other's pending updates.
const groupTemplate = yaml.match(/^  group: (.+)$/m)?.[1];
assert.equal(groupTemplate, 'buddy-project-status-${{ github.event.pull_request.number || inputs.pr_number }}');
assert.match(yaml, /^  cancel-in-progress: false$/m);
function groupFor(payload, inputs = {}) {
  return groupTemplate.replace(/\$\{\{\s*(.*?)\s*\}\}/g, (_, expression) => {
    const values = {
      'github.event.pull_request.number': payload.pull_request?.number,
      'inputs.pr_number': inputs.pr_number,
    };
    for (const key of expression.split('||').map(value => value.trim())) {
      assert.ok(Object.hasOwn(values, key), 'Unexpected concurrency input: ' + key);
      if (values[key]) return String(values[key]);
    }
    throw Error('Missing PR concurrency identity');
  });
}
const eventGroup = number => groupFor({pull_request:{number}});
assert.equal(eventGroup(64), groupFor({}, {pr_number:64}));
assert.equal(eventGroup(64), groupFor({pull_request:{number:64}, review:{state:'APPROVED'}}));
assert.notEqual(eventGroup(64), eventGroup(65));
// Model GitHub's single pending slot per concurrency group.
const running = new Set([eventGroup(64), eventGroup(65), eventGroup(66)]);
const pending = new Map();
const canceled = [];
function enqueue(group, run) {
  if (!running.has(group)) { running.add(group); return; }
  if (pending.has(group)) canceled.push(pending.get(group));
  pending.set(group, run);
}
enqueue(eventGroup(64), 'A2');
enqueue(eventGroup(65), 'B2');
enqueue(eventGroup(66), 'C2');
assert.deepEqual(canceled, []);
assert.deepEqual([...pending.values()], ['A2', 'B2', 'C2']);
enqueue(groupFor({}, {pr_number:65}), 'B3');
assert.deepEqual(canceled, ['B2']);
assert.equal(pending.get(eventGroup(64)), 'A2');
assert.equal(pending.get(eventGroup(66)), 'C2');
console.log('Per-PR concurrency configuration and queue regression checks passed');
const script = yaml.split('          script: |\n')[1].split('\n').map(l => l.slice(12)).join('\n');
const execute = new (Object.getPrototypeOf(async function() {}).constructor)('github', 'context', 'core', script);
const pageInfo = {hasNextPage:false};
async function check({draft=false, state='open', merged=false, events=[], reviews=[], closedIssue=false, archived=false, requests=null, itemPages=null, links=null, body='', title=''}, expected) {
  const mutations = [];
  const pr = {number:55, draft, state, merged, title, body, base:{ref:'main'},user:{login:'author'}};
  const github = {rest:{pulls:{list:'list', get:async()=>({data:pr}), listReviews:'reviews'}},
    paginate:async(endpoint)=>{assert.equal(endpoint,'reviews');return reviews;},
    graphql:async(q,v)=>{
      if(q.startsWith('mutation')) {
        assert.match(q, /updateProjectV2ItemFieldValue/);
        assert.doesNotMatch(q, /updateIssue|addAssignees|removeAssignees|addProjectV2Item|deleteProject|closeIssue|reopenIssue/);
        assert.deepEqual(Object.keys(v).sort(), ['field','item','project','status']);
        assert.equal(v.field, 'f');
        assert.equal(v.item, 'item');
        mutations.push(v.status);return {};
      }
      if(q.includes('fields(first:100)')) {
        assert.match(q, /organization\(login:"Buddy-presenter"\)/);
        assert.doesNotMatch(q, /user\(login:/);
        return {organization:{projectV2:{id:'p',fields:{pageInfo,nodes:[{id:'f',name:'Status',options:[{id:'progress',name:'In Progress'},{id:'waiting',name:'Waiting for review'},{id:'reviewed',name:'Reviewed'},{id:'done',name:'Done'}]}]}}}};
      }
      if(q.includes('items(first:100')) {
        const index = v.cursor === null ? 0 : Number(v.cursor);
        const batches = itemPages ?? [[{id:'item',isArchived:archived,content:{__typename:'Issue',id:'issue',number:46,state:closedIssue?'CLOSED':'OPEN',repository:{nameWithOwner:'Buddy-presenter/Buddy'}},fieldValueByName:{optionId:'backlog'}}]];
        return {node:{items:{pageInfo:{hasNextPage:index + 1 < batches.length,endCursor:String(index + 1)},nodes:batches[index]}}};
      }
      if(q.includes('closingIssuesReferences')) return {repository:{pullRequest:{closingIssuesReferences:{pageInfo,nodes:links ?? [{id:'issue',state:closedIssue?'CLOSED':'OPEN',repository:{nameWithOwner:'Buddy-presenter/Buddy'}}]}}}};
      if(q.includes('issue(number:$number)')) {
        assert.equal(v.number,46);
        return {repository:{issue:{id:'issue',number:46,state:'OPEN',repository:{nameWithOwner:'Buddy-presenter/Buddy'}}}};
      }
      if(q.includes('reviewRequests(first:100')) return {repository:{pullRequest:{reviewRequests:{pageInfo,nodes:requests ?? (events.some(e=>e.__typename==='ReviewRequestedEvent')?[{requestedReviewer:{__typename:'User'}}]:[])}}}};
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
 // Canceling every reviewer must end waiting, including scheduled reconciliation.
 await check({events:[request],requests:[]},'progress');
 await check({events:[request],requests:[{requestedReviewer:{__typename:'User'}}]},'waiting');
 await check({events:[request],requests:[{requestedReviewer:{__typename:'Team'}}]},'waiting');
 await check({events:[request,{...request,createdAt:'2026-10-13T12:00:00Z'}],requests:[{requestedReviewer:{__typename:'User'}}]},'waiting');
 // Prior feedback remains Reviewed if a subsequent request is canceled.
 await check({events:[request,{...request,createdAt:'2026-10-13T12:00:00Z'}],reviews:[review('APPROVED')],requests:[]},'reviewed');
 await check({events:[request],reviews:[review('COMMENTED')],requests:[]},'reviewed');
 await check({events:[request,{...request,createdAt:'2026-10-13T12:00:00Z'}],reviews:[review('APPROVED')],requests:[{requestedReviewer:{__typename:'Team'}}]},'waiting');
 // Unsupported/hidden cards must not prevent later linked issues from updating.
 const issueItem={id:'item',content:{__typename:'Issue',id:'issue',number:46,state:'OPEN',repository:{nameWithOwner:'Buddy-presenter/Buddy'}},fieldValueByName:{optionId:'backlog'}};
 const prItem={id:'pr-card',content:{__typename:'PullRequest'}};
 const draftItem={id:'draft-card',content:{__typename:'DraftIssue'}};
 await check({itemPages:[[prItem,issueItem]]},'progress');
 await check({itemPages:[[draftItem,issueItem]]},'progress');
 await check({itemPages:[[prItem,draftItem],[issueItem]],events:[request]},'waiting');
 await check({itemPages:[[{id:'hidden',content:null},{id:'redacted',content:{}},issueItem]]},'progress');
 await check({itemPages:[[{...issueItem,id:'archived',isArchived:true},{...issueItem,id:'foreign',content:{...issueItem.content,repository:{nameWithOwner:'elsewhere/repo'}}},issueItem]]},'progress');
 // Merely mentioning an issue is not a closing link.
 await check({links:[]},null);
 // Explicit non-closing association updates status without closing the issue.
 await check({links:[],body:'This PR is part of #46'},'progress');
 await check({links:[],body:'This PR is part of #46',events:[request]},'waiting');
 // A bare issue mention is not enough to associate unrelated work.
 await check({links:[],body:'Discusses #46'},null);
 // Do not undo Done even when somebody has manually reopened an issue.
 await check({itemPages:[[{...issueItem,fieldValueByName:{optionId:'done'}}]]},null);
 // Even explicit closing links to an already-closed issue cannot affect it.
 const closedItem={...issueItem,id:'completed-item',content:{...issueItem.content,id:'completed',number:1,state:'CLOSED'},fieldValueByName:{optionId:'done'}};
 await check({links:[{id:'completed',state:'CLOSED',repository:{nameWithOwner:'Buddy-presenter/Buddy'}},{id:'issue',state:'OPEN',repository:{nameWithOwner:'Buddy-presenter/Buddy'}}],itemPages:[[closedItem,issueItem]]},'progress');
 console.log('30 mocked integration scenarios passed; writes restricted to linked open Status');
})().catch(e=>{console.error(e);process.exitCode=1});
