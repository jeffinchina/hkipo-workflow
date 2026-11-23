/** Read-only anonymous public broker source. No application/service integration. */
import fs from 'node:fs/promises';
import {chromium} from 'playwright';
const [output,maxArg='3']=process.argv.slice(2);const max=Number(maxArg);
if(!output||!Number.isInteger(max)||max<1||max>20)throw Error('usage: futu_discovery.mjs OUTPUT.json [1..20]');
const entry='https://q.futunn.com/profile/12147652';
const allowed=u=>u===entry||/^https:\/\/q\.futunn\.com\/feed\/\d+$/.test(u);
const browser=await chromium.launch({...(process.env.FUTU_CHROME_PATH ? {executablePath:process.env.FUTU_CHROME_PATH} : {}),headless:true});
const snapshot={schema_version:1,source:'futu_public_12147652',entry,started_at:new Date().toISOString(),status:'failed',posts:[]};
try{
 const context=await browser.newContext();const page=await context.newPage();
 const nav=async(p,url)=>{if(!allowed(url))throw Error('URL outside source allowlist');const r=await p.goto(url,{waitUntil:'domcontentloaded',timeout:45000});if(!r||r.status()!==200||!allowed(p.url()))throw Error('public page unavailable');};
 await nav(page,entry);await page.waitForTimeout(1500);
 const candidates=await page.locator('a[href]').evaluateAll(aa=>[...new Set(aa.filter(a=>/中籤公佈|中签公布/.test(a.innerText)).map(a=>a.href).filter(u=>/^https:\/\/q\.futunn\.com\/feed\/\d+$/.test(u)))]);
 if(!candidates.length)throw Error('empty public discovery list');
 for(const url of candidates.slice(0,max)){
  const p=await context.newPage();await nav(p,url);
  for(let i=0;i<3;i++){await p.evaluate(()=>window.scrollTo(0,document.body.scrollHeight));await p.waitForTimeout(200);}
  const detail=await p.evaluate(()=>{
   const script=[...document.scripts].map(s=>s.textContent).find(t=>/^\s*window\.__INITIAL_STATE__\s*=/.test(t));
   const match=script?.match(/^\s*window\.__INITIAL_STATE__\s*=\s*(\{[\s\S]*\});\s*$/);
   if(!match)return null;const d=JSON.parse(match[1]).detail;
   return d?{feedCommon:{feedId:d.feedCommon?.feedId,timestamp:d.feedCommon?.timestamp},authorInfo:{userId:d.authorInfo?.userId},title:d.feedTitle,body:d.feedFlattenText,moduleData:d.moduleData}:null;
  });
  const id=url.split('/').pop();if(!detail||String(detail.feedCommon?.feedId)!==id||String(detail.authorInfo?.userId)!=='12147652'||!/^\d+$/.test(String(detail.feedCommon?.timestamp)))throw Error('unverified current feed identity');
  const images=[];const visit=v=>{if(!v||typeof v!=='object')return;if(v.orgPic?.url&&v.bigPic?.url&&!v.imageLink){for(const variant of ['orgPic','bigPic']){const u=v[variant].url;if(/^https:\/\/nnqimage\.futunn\.com\//.test(u))images.push(u);}}for(const x of Object.values(v))visit(x);};visit(detail.moduleData);
  if(!detail.title||typeof detail.body!=='string'||!detail.body||!images.length)throw Error('empty broker content or original image evidence');
  snapshot.posts.push({feed_id:id,url,author_id:'12147652',published_timestamp:Number(detail.feedCommon.timestamp),title:detail.title,body:detail.body,images:[...new Set(images)],data_scope:'broker_customer_allotment'});await p.close();
 }
 snapshot.status='success';await context.close();
}catch(e){snapshot.error=String(e.message||e);}finally{await browser.close();snapshot.finished_at=new Date().toISOString();await fs.writeFile(output+'.tmp',JSON.stringify(snapshot,null,2));await fs.rename(output+'.tmp',output);}
if(snapshot.status!=='success')process.exitCode=1;
