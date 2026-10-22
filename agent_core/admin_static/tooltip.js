"use strict";
(() => {
  // A body-level fixed layer escapes the workspace's scroll/overflow clips.
  const tip=document.createElement('div');
  tip.id='core-help-tooltip';tip.className='help-tooltip';tip.role='tooltip';tip.hidden=true;
  document.body.append(tip);
  let anchor=null,closeTimer=null,previousDescription=null;
  function hide(){
    clearTimeout(closeTimer);
    if(anchor){
      if(previousDescription===null)anchor.removeAttribute('aria-describedby');
      else anchor.setAttribute('aria-describedby',previousDescription);
    }
    anchor=null;tip.hidden=true;tip.textContent='';
  }
  function show(target){
    clearTimeout(closeTimer);
    if(anchor!==target){
      hide();anchor=target;previousDescription=target.getAttribute('aria-describedby');
      target.setAttribute('aria-describedby',[previousDescription,tip.id].filter(Boolean).join(' '));
    }
    tip.textContent=target.dataset.help;tip.hidden=false;
    const rect=target.getBoundingClientRect(),box=tip.getBoundingClientRect();
    const width=document.documentElement.clientWidth,height=document.documentElement.clientHeight,gap=8,pad=12;
    const left=Math.max(pad,Math.min(rect.left+(rect.width-box.width)/2,width-box.width-pad));
    const preferred=rect.bottom+gap+box.height<=height-pad?rect.bottom+gap:rect.top-box.height-gap;
    tip.style.left=left+'px';tip.style.top=Math.max(pad,Math.min(preferred,height-box.height-pad))+'px';
  }
  function delayedHide(){
    clearTimeout(closeTimer);
    closeTimer=setTimeout(()=>{if(anchor&&document.activeElement!==anchor)hide();},100);
  }
  document.addEventListener('pointerover',event=>{
    const target=event.target.closest?.('.help[data-help]');
    if(target)show(target);
    else if(tip.contains(event.target))clearTimeout(closeTimer);
  });
  document.addEventListener('pointerout',event=>{
    if(anchor&&(anchor.contains(event.target)||tip.contains(event.target))&&
       !anchor.contains(event.relatedTarget)&&!tip.contains(event.relatedTarget))delayedHide();
  });
  document.addEventListener('focusin',event=>{
    const target=event.target.closest?.('.help[data-help]');
    if(target)show(target);else hide();
  });
  document.addEventListener('focusout',event=>{if(event.target===anchor)hide();});
  document.addEventListener('keydown',event=>{
    if(event.key==='Escape'&&anchor){hide();event.stopPropagation();}
  },true);
  document.addEventListener('pointerdown',event=>{
    if(anchor&&!anchor.contains(event.target)&&!tip.contains(event.target))hide();
  },true);
  document.addEventListener('scroll',event=>{if(!tip.contains(event.target))hide();},true);
  window.addEventListener('resize',hide);
  new MutationObserver(()=>{if(anchor&&!anchor.isConnected)hide();}).observe(document.body,{childList:true,subtree:true});
})();
