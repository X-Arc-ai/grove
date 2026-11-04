/* D14: two deliberate steps. No automatic retry and no browser storage of the target. */
(function(){
  'use strict';
  const el=id=>document.getElementById(id);
  const form=el('forget-form'),status=el('status'),confirmation=el('confirmation');
  let selected=null,busy=false;
  function message(text){status.textContent=text;}
  function errorMessage(code){
    return ({unauthenticated:'Sign in again before you continue.',approval_required:'This approval is no longer valid. Review the removal again.',unavailable:'Forget is not available on this server. No new request will be sent.',not_found:'The selected content is no longer available.',invalid_arguments:'Check your selection and review it again.'})[code]||'The request was refused. Check your Grove before you submit another request.';
  }
  async function request(url,body){
    let response;
    try{response=await fetch(url,{method:body?'POST':'GET',credentials:'same-origin',cache:'no-store',headers:body?{'Content-Type':'application/json'}:{},body:body?JSON.stringify(body):undefined});}
    catch(_){throw new Error('The request result is unknown. Check your Grove before another request.');}
    let value;try{value=await response.json();}catch(_){throw new Error('The server returned no readable result. Check your Grove before another request.');}
    if(!response.ok){const error=new Error(errorMessage(value.error&&value.error.code));error.code=value.error&&value.error.code;throw error;}
    return value;
  }
  function fields(){
    const type=el('target-type').value;
    el('phrase-fields').hidden=type!=='named_value';el('item-fields').hidden=type==='named_value';el('field-fields').hidden=type!=='field_value';
    el('named-value').required=type==='named_value';el('item-id').required=type!=='named_value';
  }
  el('target-type').addEventListener('change',fields);fields();
  form.addEventListener('submit',event=>{
    event.preventDefault();if(busy)return;
    const type=el('target-type').value;
    if(type==='named_value'){
      const value=el('named-value').value;if(!value.trim()){message('Enter a word or phrase.');return;}
      selected={type,value};el('choice').textContent='Word or phrase: '+value;
    }else{
      const picker=el('item-id');if(!picker.value){message('Choose an item.');return;}
      selected={type,item_id:picker.value};el('choice').textContent='Item: '+picker.selectedOptions[0].textContent;
      if(type==='field_value'){selected.field=el('field-name').value;el('choice').textContent+=' · Field: '+el('field-name').selectedOptions[0].textContent;}
    }
    form.hidden=true;confirmation.hidden=false;message('Review the removal. Nothing is removed yet.');el('confirm-heading').focus();
  });
  el('cancel-button').addEventListener('click',()=>{if(busy)return;selected=null;el('choice').textContent='';confirmation.hidden=true;form.hidden=false;message('Removal cancelled.');el('review-button').focus();});
  el('confirm-button').addEventListener('click',async()=>{
    if(busy||!selected)return;busy=true;el('confirm-button').disabled=true;el('cancel-button').disabled=true;message('Submitting your confirmed removal.');
    try{
      const approved=await request('/v1/forget/approve',{target:selected,confirm:true});
      const receipt=await request('/v1/forget',{target:selected,approval_token:approved.approval_token});
      const total=Object.values(receipt.counts||{}).filter(n=>Number.isSafeInteger(n)&&n>=0).reduce((a,b)=>a+b,0);
      selected=null;el('choice').textContent='';el('named-value').value='';el('item-id').replaceChildren();confirmation.hidden=true;
      el('receipt-count').textContent=total+' stored entries changed.';
      el('receipt-backup').textContent=/^\d{4}-\d{2}-\d{2}$/.test(receipt.backups_clear_after||'')?'Older backups expire by '+receipt.backups_clear_after+'.':'Check the retention date with the operator.';
      el('receipt').hidden=false;message('The removal completed.');el('receipt-heading').focus();
    }catch(error){message(error.message);selected=null;el('choice').textContent='';confirmation.hidden=true;form.hidden=false;el('review-button').focus();}
    finally{busy=false;el('confirm-button').disabled=false;el('cancel-button').disabled=false;}
  });
  (async()=>{
    try{
      await request('/v1/views/session');
      const graph=await request('/v1/views/graph');
      for(const node of ((graph.elements||{}).nodes||[])){
        const data=node.data||{};if(typeof data.id!=='string'||typeof data.label!=='string')continue;
        const option=document.createElement('option');option.value=data.id;option.textContent=data.label;el('item-id').append(option);
      }
      form.hidden=false;message('Choose the content to remove.');
    }catch(error){message(error.message);if(error.code==='unauthenticated')el('signin').hidden=false;}
  })();
})();
