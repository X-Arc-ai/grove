/* The fallback table remains readable when no server is available. */
(function(){
  'use strict';
  const allowed=new Set(['Settings present','Not configured','Implementation pending','Deployment not checked']);
  fetch('/v1/privacy',{credentials:'omit',cache:'no-store'}).then(response=>{
    if(!response.ok)throw new Error('unavailable');return response.json();
  }).then(data=>{
    const rows=document.querySelectorAll('.flow-table tbody tr');
    if(data.runtime!==true||!Array.isArray(data.rows)||data.rows.length!==rows.length)return;
    data.rows.forEach((row,index)=>{if(!allowed.has(row.status))return;const cell=rows[index].querySelector('[data-label="Status"]');if(cell){cell.textContent=row.status;cell.dataset.status=row.status.toLowerCase();}});
    document.getElementById('flow-state').textContent='The status column uses this server\'s current configuration. Settings present does not prove a successful connection or completed operation.';
  }).catch(()=>{});
})();
