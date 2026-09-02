// Run only against assistant_ui_server.py: it has an isolated in-memory database.
const assert = require('node:assert/strict');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
(async()=>{
  const visible = process.env.ASSISTANT_VISIBLE === '1';
  const browser = await chromium.launch({headless:!visible, channel:'chrome'});
  const page = await browser.newPage(); const errors=[];
  page.on('pageerror',e=>errors.push(e.message));
  await page.goto('http://127.0.0.1:18098/static/assistant.html');
  await page.getByText('Harness 尚未配置，可先用显式路径生成计划。配置模型后才能使用自然语言规划。').waitFor();
  await page.locator('#pipeline').selectOption({label:'自然资源DWD'});
  await page.locator('summary').click();
  await page.locator('#filename').fill('test.dmp');
  await page.locator('#prepare').click();
  await page.getByText('已生成待确认计划，尚未执行。',{exact:true}).waitFor();
  await page.locator('#review').click();
  await page.locator('#reviewDialog[open]').waitFor();
  assert.equal(await page.locator('#confirm').isDisabled(),true);
  assert.match(await page.locator('#detail').innerText(),/ALL_DB/);
  assert.match(await page.locator('#detail').innerText(),/ID_CARD/);
  assert.match(await page.locator('#detail').innerText(),/truncate_insert/);
  await page.locator('#detail summary').click();
  assert.match(await page.locator('#detail pre').innerText(),/insert into DWD.A select \* from ODS.A/);
  for(const [width,height] of [[1440,900],[960,900],[720,900],[390,844]]){
    if(visible){
      const session=await page.context().newCDPSession(page);
      const {windowId}=await session.send('Browser.getWindowForTarget');
      await session.send('Browser.setWindowBounds',{windowId,bounds:{windowState:'normal'}});
      await session.send('Browser.setWindowBounds',{windowId,bounds:width===1440?{windowState:'maximized'}:{left:width===720?960:0,top:0,width,height}});
      await session.detach();
    }
    await page.setViewportSize({width,height});
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth),true);
    assert.equal(await page.locator('#confirm').isVisible(),true);
  }
  await page.setViewportSize({width:1440,height:900});
  await page.screenshot({path:'tmp/assistant-ui-review.png',fullPage:true});
  await page.locator('#ack').check();await page.locator('#confirm').click();
  await page.getByText('已确认；后台将自动推进四个阶段。',{exact:true}).waitFor();
  await page.reload();
  await page.getByText('等待文件稳定',{exact:true}).waitFor();
  assert.match(await page.locator('#summary').innerText(),/2 张表/);
  assert.match(await page.locator('#history').innerText(),/等待文件稳定/);
  await page.locator('#review').click();assert.equal(await page.locator('#confirm').isVisible(),false);
  await page.locator('#closeReview').click();
  await page.locator('#message').fill('处理test.dmp');await page.locator('#chat').click();
  await page.locator('#reply.error').waitFor();
  assert.match(await page.locator('#reply').innerText(),/尚未配置/);
  assert.deepEqual(errors,[]);
  await browser.close();console.log('UI PASS: prepare, full scope, explicit confirmation, reload, missing model, 4 viewports; no page errors');
})().catch(e=>{console.error(e);process.exit(1)});
