const {readFileSync}=require('node:fs');
const vm=require('node:vm');
const assert=require('node:assert/strict');
const html=readFileSync(`${__dirname}/../app/web/index.html`,'utf8');
const code=html.slice(html.indexOf('    async function selectVideo'),html.indexOf('    async function watchProxyAndSwitch'));
const requests=[];
const ctx={deletionBusy:false,selectionBusy:true,pendingSelection:null,
    api:async(url,options)=>{requests.push({url,body:JSON.parse(options.body)});return {}},
    document:{getElementById(){return {textContent:''}}}};
vm.createContext(ctx);vm.runInContext(code,ctx);
(async()=>{
    await ctx.selectVideo({path:'first.mp4'});
    await ctx.selectVideo({path:'second.mp4'});
    assert.deepEqual(requests.map(r=>r.body.path),['first.mp4','second.mp4']);
    assert.ok(requests.every(r=>r.url==='/api/proxy/queue'));
    assert.equal(ctx.pendingSelection.file.path,'second.mp4');
    ctx.deletionBusy=true;
    await ctx.selectVideo({path:'deleting.mp4'});
    assert.equal(requests.length,2);
    assert.match(html,/<video[^>]+id="video"[^>]+controls/);
    assert.match(html,/NeoClip 240/);
    console.log('Every selection queues a durable 240p build while another preview opens');
})().catch(error=>{console.error(error);process.exitCode=1});
