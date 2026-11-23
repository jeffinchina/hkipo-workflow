import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
const [directory,modulePath]=process.argv.slice(2);
const {chromium}=await import(pathToFileURL(modulePath).href);
const profile=fs.mkdtempSync(path.join(os.tmpdir(),'hkipo-layout-'));
let browser;
try {
  browser=await chromium.launchPersistentContext(profile,{
    ...(process.env.HKIPO_CHROME_PATH ? {executablePath:process.env.HKIPO_CHROME_PATH} : {}),headless:true,
    viewport:{width:390,height:844},deviceScaleFactor:1,isMobile:true,hasTouch:true,
    args:['--no-first-run','--disable-background-networking']});
  const page=await browser.newPage();
  const url=pathToFileURL(path.join(directory,'preview.html')).href;
  await page.route('**/*',route=>{
    const u=route.request().url();
    return (u===url||u.startsWith('data:')||u.startsWith('blob:'))?route.continue():route.abort();
  });
  await page.goto(url,{timeout:20000});
  await page.evaluate(()=>document.fonts.ready);
  await page.locator('img').evaluateAll(imgs=>Promise.all(imgs.map(i=>i.decode().catch(()=>{}))));
  const geometry=await page.evaluate(()=>({
    viewport_width:innerWidth,scroll_width:document.documentElement.scrollWidth,
    height:document.documentElement.scrollHeight,image_count:document.images.length,
    broken_images:[...document.images].filter(i=>!i.complete||!i.naturalWidth).length,
    qr_width:document.querySelector('[data-branding="personal-wechat-qr"] img')?.getBoundingClientRect().width,
    overflow_count:[...document.querySelectorAll('body *')].filter(e=>{
      const r=e.getBoundingClientRect();return r.right>390.5||r.left<-.5;
    }).length}));
  if(geometry.height<=40000)await page.screenshot({path:path.join(directory,'mobile-preview.png'),fullPage:true});
  fs.writeFileSync(path.join(directory,'layout-geometry.json'),JSON.stringify(geometry,null,2));
  process.stdout.write(JSON.stringify(geometry));
} finally {
  if(browser)await browser.close();
  fs.rmSync(profile,{recursive:true,force:true});
}
