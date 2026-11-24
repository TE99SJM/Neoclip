const {readFileSync}=require('node:fs');
const vm=require('node:vm');
const assert=require('node:assert/strict');
const html=readFileSync(`${__dirname}/../app/web/index.html`,'utf8');
const code=html.slice(html.indexOf('// FILE_RENAME_START'),html.indexOf('// FILE_RENAME_END'));
function setup({name='New title.mp4', fail=false, lostReply=false, saveFails=false}={}) {
 const calls=[],elements=new Map();
 const ctx={currentSourcePath:'Folder/movie.mp4',activeFile:{path:'Folder/movie.mp4',name:'movie.mp4'},
  currentProxy:{id:'same-id',path:'Folder/movie.mp4'},deletionBusy:false,selectionBusy:false,
  userWantsPlayback:false,playbackEnded:false,currentlyUsingProxy:true,restoringPlayhead:false,
  pendingResumeSeconds:29,proxyWatchToken:1,lastSavedSessionJson:'old',markInSeconds:10,markOutSeconds:40,
  previewRecovery:{pending:()=>false,cancel(){calls.push('cancel')},request(options){calls.push(['recover',options])}},
  video:{error:null,pause(){calls.push('pause')},removeAttribute(){},load(){calls.push('detach')}},
  window:{prompt(){return name}},alert(message){calls.push(['alert',message])},
  document:{getElementById(id){if(!elements.has(id))elements.set(id,{textContent:''});return elements.get(id)}},
  editorSessionPayload(){return {path:ctx.currentSourcePath,current_time:29,mark_in:10,mark_out:40,clip_name:'draft'}},
  async saveEditorSessionNow(){calls.push('save');if(saveFails)throw Error('offline')},
  startHeartbeat(path){calls.push(['heartbeat',path])},stopHeartbeat(){},scheduleEditorSessionSave(){calls.push('schedule')},
  destroyHls(){},cancelPendingSeeks(){},proxyStatusUrl(){return '/status'},
  async loadState(){calls.push('state')},async loadFiles(){calls.push('files')},
  async api(url, options){
   if(url==='/status')return {id:'same-id',path:lostReply?'Folder/New title.mp4':'Folder/movie.mp4'};
   assert.equal(url,'/api/media/rename');assert.equal(ctx.deletionBusy,true);
   assert.equal(ctx.restoringPlayhead,true);assert.equal(ctx.currentlyUsingProxy,false);
   assert.deepEqual(JSON.parse(options.body),{path:'Folder/movie.mp4',name:'New title.mp4'});
   calls.push('rename');if(fail||lostReply)throw Error('Server unavailable');
   return {ok:true,path:'Folder/New title.mp4',warning:''};
  },
 };
 vm.createContext(ctx);vm.runInContext(code,ctx);return {ctx,calls,elements};
}
(async()=>{
 assert.match(html,/id="renameVideoButton" onclick="renameVideo\(\)"/);
 for(const name of [null,'movie.mp4']) {
  const s=setup({name});await s.ctx.renameVideo();assert.equal(s.calls.length,0);
 }
 const saved=setup();await saved.ctx.renameVideo();
 assert.equal(saved.ctx.currentSourcePath,'Folder/New title.mp4');
 assert.equal(saved.ctx.activeFile.name,'New title.mp4');assert.equal(saved.ctx.currentProxy.id,'same-id');
 assert.equal(saved.ctx.markInSeconds,10);assert.equal(saved.ctx.markOutSeconds,40);
 assert.equal(saved.ctx.pendingResumeSeconds,29);assert.equal(saved.ctx.deletionBusy,false);
 const recovery=saved.calls.find(c=>c[0]==='recover')[1];
 assert.equal(recovery.current_time,29);assert.equal(recovery.autoplay,false);
 assert.ok(saved.calls.indexOf('save')<saved.calls.indexOf('detach'));
 assert.ok(saved.calls.indexOf('detach')<saved.calls.indexOf('rename'));
 const failed=setup({fail:true});await failed.ctx.renameVideo();
 assert.equal(failed.ctx.currentSourcePath,'Folder/movie.mp4');assert.equal(failed.ctx.pendingResumeSeconds,29);
 assert.equal(failed.ctx.markInSeconds,10);assert.ok(failed.calls.some(c=>c[0]==='alert'&&/Rename failed/.test(c[1])));
 const lost=setup({lostReply:true});await lost.ctx.renameVideo();
 assert.equal(lost.ctx.currentSourcePath,'Folder/New title.mp4');
 const saveFails=setup({saveFails:true});await saveFails.ctx.renameVideo();
 assert.ok(!saveFails.calls.includes('rename'));assert.ok(!saveFails.calls.includes('detach'));
 const auto=setup();assert.equal(auto.ctx.adoptRenamedVideo('other.mp4','other clipped.mp4'),false);
 assert.equal(auto.ctx.currentSourcePath,'Folder/movie.mp4');
 assert.equal(auto.ctx.adoptRenamedVideo('Folder/movie.mp4','Folder/movie clipped.mp4'),true);
 assert.equal(auto.ctx.markInSeconds,10);assert.equal(auto.ctx.currentProxy.id,'same-id');
 assert.ok(!auto.calls.includes('detach'),'automatic rename keeps the active native player');
 assert.equal(auto.ctx.adoptRenamedVideo('Folder/movie.mp4','Folder/movie clipped.mp4'),false);
 console.log('Rename UI: manual/cancel/failure/lost response, saved edits, cached player and automatic source identity');
})().catch(e=>{console.error(e);process.exitCode=1});
