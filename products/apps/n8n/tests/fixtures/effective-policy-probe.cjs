// Executes the exact pinned CallTool callback/normalizer/native policy; no server
// constructor/listener or backend. Terminal execution only records arguments.
'use strict';
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const assert=require('node:assert/strict');
const root=process.argv[2], input=JSON.parse(fs.readFileSync(0,'utf8'));
const cache=new Map(), noop=()=>{}, calls=[];
const mocks={
  '@modelcontextprotocol/sdk/types.js':Object.fromEntries(['InitializeRequestSchema','ListToolsRequestSchema','CallToolRequestSchema','ListResourcesRequestSchema','ListResourceTemplatesRequestSchema','ReadResourceRequestSchema'].map(x=>[x,x])),
  './ui':{isUIAppsEnabled:()=>false},
  '../utils/request-context':{getRequestScope:()=>undefined},
  '../utils/redaction':{summarizeToolCallArgs:()=>({})},
  '../utils/logger':{logger:{debug:noop,info:noop,warn:noop,error:noop}},
  '../telemetry':{telemetry:{trackToolUsage:noop,trackToolSequence:noop,trackError:noop}},
};
function load(name){
  if(cache.has(name))return cache.get(name).exports;
  const mod={exports:{}};cache.set(name,mod);
  function req(n){
    if(n==='fs'||n==='path')return require(n);
    if(Object.hasOwn(mocks,n))return mocks[n];
    const relative=path.posix.normalize(path.posix.join(path.posix.dirname(name),n))+'.js';
    if(fs.existsSync(path.join(root,relative)))return load(relative);
    return new Proxy({__esModule:true},{get(_o,k){if(k==='__esModule')return true;throw Error('UNEXPECTED_DEPENDENCY '+n+'.'+String(k));}});
  }
  const file=path.join(root,name);
  vm.runInThisContext('(function(exports,require,module){\n'+fs.readFileSync(file,'utf8')+'\n})',{filename:file})(mod.exports,req,mod);
  return mod.exports;
}
(async()=>{
  const docs=load('dist/mcp/tools.js').n8nDocumentationToolsFinal;
  const manager=load('dist/mcp/tools-n8n-manager.js');
  if(input.schemas){
    const schemas=Object.fromEntries([...docs,...manager.n8nManagementTools].map(t=>[t.name,{required:t.inputSchema.required||[],types:Object.fromEntries(Object.entries(t.inputSchema.properties||{}).map(([k,v])=>[k,v.type||null]))}]));
    process.stdout.write(JSON.stringify(schemas));return;
  }
  const C=load('dist/mcp/server.js').N8NDocumentationMCPServer;
  const server=Object.create(C.prototype),handlers=new Map();
  Object.assign(server,{server:{setRequestHandler:(s,f)=>handlers.set(s,f)},additionalToolsByName:new Map(),
    disabledToolsCache:null,disabledToolOperationsCache:null,filteredToolDefinitionsCache:null,
    previousTool:null,requireRequestContext:false,sessionInstanceContext:undefined});
  server.executeTool=async(name,args)=>{calls.push({name,args});return {success:true};};
  server.setupHandlers();
  const response=await handlers.get('CallToolRequestSchema')({params:{name:input.name,arguments:input.arguments}});
  assert(!JSON.stringify(response).includes('UNEXPECTED_DEPENDENCY'));
  process.stdout.write(JSON.stringify({calls,response}));
})().catch(e=>{process.stderr.write(e.stack);process.exitCode=2;});
