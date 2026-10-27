// Execute in cua_repl after binding the local preview as quietTab.
// This uses only documented CUA browser operations and read-only DOM assertions.
export async function verifyQuietPreview(tab) {
  const p=tab.playwright;
  async function focused(){return p.evaluate(()=>Array.from(document.querySelectorAll('button.focused')).map(e=>e.getAttribute('data-focus-key')));}
  function check(value,message){if(!value)throw new Error(message);}
  await p.getByRole('button',{name:'Home',exact:true}).click();await tab.getAXState({emit:false});
  await p.getByRole('button',{name:'Netflix',exact:true}).press('ArrowRight');await tab.getAXState({emit:false});
  const before=await focused();check(before.length===1&&before[0]==='home-9Ur5IzDKqV.TizenYouTube','Right must focus only YouTube');
  await tab.pressKey(null,'Escape');await tab.getAXState({emit:false});
  await p.getByRole('button',{name:'Keep watching',exact:true}).press('ArrowUp');await tab.getAXState({emit:false});
  check(JSON.stringify(await focused())===JSON.stringify(['dialog-cancel']),'Focus escaped dialog');
  await p.getByRole('button',{name:'Keep watching',exact:true}).press('Enter');await tab.getAXState({emit:false});
  check(JSON.stringify(await focused())===JSON.stringify(before),'Home cancel did not restore exactly one prior app');
  await p.getByRole('button',{name:'Settings',exact:true}).click();await tab.getAXState({emit:false});
  await p.getByRole('button',{name:'Close dashboard',exact:true}).click();await tab.getAXState({emit:false});
  await p.getByRole('button',{name:'Keep watching',exact:true}).press('Enter');await tab.getAXState({emit:false});
  check(JSON.stringify(await focused())===JSON.stringify(['setting-close']),'Settings cancel left multiple or incorrect highlights');
  await p.getByRole('button',{name:'Close dashboard',exact:true}).click();await tab.getAXState({emit:false});
  await p.getByRole('button',{name:'Keep watching',exact:true}).click();await tab.getAXState({emit:false});
  check((await focused()).length===1,'Mouse cancel left multiple highlights');
  await tab.pressKey(null,'Escape');await tab.getAXState({emit:false});
  await p.getByRole('button',{name:'Apps',exact:true}).click();await tab.getAXState({emit:false});
  await p.getByRole('button',{name:'Next',exact:true}).click();await tab.getAXState({emit:false});
  const page=await p.evaluate(()=>({cards:document.querySelectorAll('.app-grid .tile').length,page:document.querySelector('.subheading')?.textContent}));
  check(page.cards===10&&page.page.includes('Page 2'),'App pagination failed');check((await focused()).length===1,'Pagination left multiple highlights');
  await p.getByRole('button',{name:'Home',exact:true}).click();await tab.getAXState({emit:false});
  const home=await p.evaluate(()=>({cards:document.querySelectorAll('.app-row .tile').length,broken:[...document.querySelectorAll('.app-row img')].filter(e=>!e.complete||!e.naturalWidth).length}));
  check(home.cards===6&&home.broken===0,'Home must show six loaded brand cards');
  await p.getByRole('button',{name:'Movies',exact:true}).click();await tab.getAXState({emit:false});
  const movies=await p.evaluate(()=>[...document.querySelectorAll('.app-grid .tile')].map(e=>e.getAttribute('aria-label')));
  check(movies.includes('Netflix')&&movies.includes('Apple TV')&&!movies.includes('SecurityCenter'),'Movies must show actual entertainment apps');
  await p.getByRole('button',{name:'Search',exact:true}).click();await tab.getAXState({emit:false});
  await p.getByRole('button',{name:'Clear',exact:true}).click();await tab.getAXState({emit:false});
  const searchBounds=await p.evaluate(()=>{const scale=1920/document.querySelector('.screen').getBoundingClientRect().width;return {next:document.querySelector('.pagination').getBoundingClientRect().bottom*scale,footer:document.querySelector('footer').getBoundingClientRect().top*scale};});
  check(searchBounds.next+12<=searchBounds.footer,'Search pagination overlaps the remote hints');
  await p.getByRole('button',{name:'N',exact:true}).click();await tab.getAXState({emit:false});
  await p.getByRole('button',{name:'E',exact:true}).click();await tab.getAXState({emit:false});
  const search=await p.evaluate(()=>({query:document.querySelector('.search-value')?.textContent,names:[...document.querySelectorAll('.search-results .tile')].map(e=>e.getAttribute('aria-label'))}));
  check(search.query==='NE'&&search.names.includes('Netflix')&&!search.names.includes('Philo'),'Remote search did not filter actual app names');
  await p.getByRole('button',{name:'Delete letter',exact:true}).click();await tab.getAXState({emit:false});
  check(await p.evaluate(()=>document.querySelector('.search-value')?.textContent)==='N','Search delete failed');
  await p.getByRole('button',{name:'Home',exact:true}).click();await tab.getAXState({emit:false});
  return {passed:true,scope:'PC browser only',checks:['arrow navigation','dialog focus boundary','Home cancel focus','Settings cancel focus','mouse cancel single highlight','app pagination','six loaded Home cards','installed movie apps','search stays above remote hints','remote app search','search delete'],homeCancelFocus:before[0]};
}
