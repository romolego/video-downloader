"use strict";
const $ = (s, root=document) => root.querySelector(s);
const escape = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const active = new Set(["queued","extracting","downloading","reconnecting","resuming","finalizing","pausing","stopping"]);
const labels = {queued:"В очереди",extracting:"Проверка источника",downloading:"Скачивание",reconnecting:"Повторное соединение",resuming:"Докачка",finalizing:"Сборка файла",pausing:"Переход на паузу",stopping:"Отмена",paused:"На паузе",error:"Ошибка",interrupted:"Прервано",completed:"Готово",cancelled:"Отменено"};
let library = {projects:[],folders:[],downloads:[],settings:{}}, view = "all", folder = null, project = new URL(location.href).searchParams.get("project");
let selecting = false, selected = new Set(), fingerprint = "", probing = false, metadata = null, metadataUrl = "";
let textResolve, deleteResolve, toastTimer, stopped = false;

async function api(path, method="GET", body) {
  const response = await fetch("/api" + path, {method,headers:{"Content-Type":"application/json","X-Downloader-Client":"local"},body:body === undefined ? undefined : JSON.stringify(body)});
  const result = await response.json();
  if(!response.ok) throw new Error(typeof result.detail === "string" ? result.detail : "Проверьте введённые данные");
  return result;
}
function toast(message) {clearTimeout(toastTimer); $("#toast").textContent=message; $("#toast").hidden=false; toastTimer=setTimeout(()=>$("#toast").hidden=true,6500);}
async function guarded(action) {try {await action();} catch(error) {toast(error.message);}}
function bytes(value) {if(!value) return "—";const units=["Б","КБ","МБ","ГБ","ТБ"];let n=Number(value),i=0;while(n>=1024 && i<4){n/=1024;i++;}return n.toFixed(i>1?1:0)+" "+units[i];}
function duration(seconds) {if(seconds == null)return "";seconds=Math.max(0,Math.round(seconds));const h=Math.floor(seconds/3600), m=Math.floor(seconds/60)%60,s=seconds%60;return (h?h+":"+String(m).padStart(2,"0"):m)+":"+String(s).padStart(2,"0");}
const qualityName = q => q === "audio" ? "Только аудио" : q === "max" ? "Максимальное" : q === "auto" ? "По умолчанию" : q+"p";
function folderOptions(value="", allLabel="Без папки") {return `<option value="">${allLabel}</option>`+library.folders.map(f=>`<option value="${f.id}" ${value===f.id?"selected":""}>${escape(f.name)}</option>`).join("");}
const jobsFor = id => library.downloads.filter(d=>d.project_id===id);
function progress(d) {if(!active.has(d.status) && d.status!=="paused")return "";return `<div class="progress"><div style="width:${Math.max(0,Math.min(100,d.percent||0))}%" ${d.percent==null?"class=indeterminate":""}></div></div>`;}
function badge(status) {return `<span class="badge ${status==="error"?"error":status==="paused"?"paused":""}">${labels[status]||escape(status)}</span>`;}

async function refresh(force=false) {
  if(stopped)return;
  try {
    const next=await api("/library"), signature=JSON.stringify(next);
    library=next;
    document.body.dataset.theme=library.settings.theme;
    if(force || signature!==fingerprint){fingerprint=signature;render();}
    $("#service-status").innerHTML="<i></i> Сервис работает";
  } catch(error){$("#service-status").textContent="Нет соединения"; if(force)toast(error.message);}
}
function visibleProjects() {
  const search=$("#search").value.toLocaleLowerCase(), filter=$("#status-filter").value;
  return library.projects.filter(p=>{
    const jobs=jobsFor(p.id);
    return (!folder || (folder==="none"?!p.folder_id:p.folder_id===folder)) &&
      (!search || (p.name+" "+jobs.map(d=>d.title).join(" ")).toLocaleLowerCase().includes(search)) &&
      (filter==="all" || jobs.some(d=>filter==="active"?active.has(d.status):d.status===filter));
  });
}
function render() {
  const running=library.downloads.filter(d=>active.has(d.status)).length, ready=library.downloads.filter(d=>d.status==="completed").length;
  $("#navigation").innerHTML=[['all','▦ Все проекты',library.projects.length],['queue','↓ Очередь загрузок',running]].map(([id,title,count])=>`<button class="nav-item ${view===id&&!folder&&!project?"active":""}" data-view="${id}"><span>${title}</span><span>${count}</span></button>`).join("");
  $("#folders").innerHTML=library.folders.map(f=>`<div class="folder-row"><button class="nav-item ${folder===f.id?"active":""}" data-folder="${f.id}"><span>▱ ${escape(f.name)}</span><span>${library.projects.filter(p=>p.folder_id===f.id).length}</span></button><button class="icon" data-rename-folder="${f.id}" title="Переименовать папку">✎</button><button class="icon" data-delete-folder="${f.id}" title="Убрать папку, сохранить проекты">×</button></div>`).join("")+`<button class="nav-item ${folder==="none"?"active":""}" data-folder="none"><span>Без папки</span><span>${library.projects.filter(p=>!p.folder_id).length}</span></button>`;
  $("#overview").innerHTML=[['Проектов в библиотеке',library.projects.length],['Загрузок в работе',running],['Готовых файлов',ready]].map(([label,n])=>`<div class="stat"><div class="number">${n}</div><small>${label}</small></div>`).join("");
  const current=library.projects.find(p=>p.id===project);
  if(project&&!current)project=null;
  $("#page-title").textContent=current?.name || (view==="queue"?"Очередь загрузок":folder?library.folders.find(f=>f.id===folder)?.name||"Без папки":"Все проекты");
  $("#page-subtitle").textContent=current?"Все записи проекта — от добавления ссылки до готового файла.":view==="queue"?"Пауза, докачка и автоматическое восстановление соединения.":"Скачивайте записи. Собирайте их в проекты. Используйте где угодно.";
  $("#select-projects").hidden=!!project||view==="queue";
  $("#select-projects").textContent=selecting?"Готово":"Выбрать";
  renderSelection();
  if(current)renderProject(current);else if(view==="queue")renderQueue();else renderProjects();
}
function renderSelection() {
  const bar=$("#selection-bar");bar.hidden=!selecting||!!project||view==="queue";
  if(bar.hidden)return;
  bar.innerHTML=`<strong>Выбрано: ${selected.size}</strong><button data-select-all>Выделить все</button><select id="bulk-folder" aria-label="Папка для выбранных проектов">${folderOptions()}</select><button data-move-selected>Перенести</button><button class="danger" data-delete-selected>Удалить</button>`;
}
function empty(title,text,button=true) {return `<div class="empty"><div class="empty-symbol">↓</div><h2>${title}</h2><p>${text}</p>${button?'<button class="primary" data-add>Скачать первую запись</button>':""}</div>`;}
function renderProjects() {
  const projects=visibleProjects();
  if(!projects.length){$("#content").innerHTML=empty(library.projects.length?"Ничего не найдено":"Начните свою медиатеку",library.projects.length?"Попробуйте изменить поиск или фильтр.":"Вставьте ссылку на видео или аудио. Загрузчик сохранит запись в отдельный проект, а вы сможете использовать её в любой программе.",!library.projects.length);return;}
  const card=p=>{const jobs=jobsFor(p.id), working=jobs.find(d=>active.has(d.status)), error=jobs.find(d=>d.status==="error"), paused=jobs.find(d=>d.status==="paused"),completed=jobs.filter(d=>d.status==="completed");return `<article class="project-card" data-project="${p.id}" tabindex="0" role="button" aria-label="Открыть проект ${escape(p.name)}"><div class="project-top"><span class="project-symbol">▱</span>${selecting?`<input type="checkbox" tabindex="-1" aria-label="Выбрать проект" ${selected.has(p.id)?"checked":""}>`:working?badge(working.status):error?badge("error"):paused?badge("paused"):completed.length?badge("completed"):""}</div><h3>${escape(p.name)}</h3><div class="muted">${jobs.length} записей · ${completed.length} готово</div>${working?progress(working):""}<div class="project-bottom"><span>${escape(library.folders.find(f=>f.id===p.folder_id)?.name||"Без папки")}</span><span>${new Date(p.created_at*1000).toLocaleDateString("ru-RU")}</span></div></article>`;};
  if(library.settings.project_view==="folders"&&!folder){const groups=[...library.folders,{id:null,name:"Без папки"}];$("#content").innerHTML=groups.map(f=>{const ps=projects.filter(p=>p.folder_id===f.id);return ps.length?`<div class="section-title">▱ ${escape(f.name)} <span>· ${ps.length}</span></div><div class="project-grid">${ps.map(card).join("")}</div>`:"";}).join("");} else $("#content").innerHTML=`<div class="project-grid">${projects.map(card).join("")}</div>`;
}
function downloadRow(d,withProject=false) {
  const canPause=active.has(d.status)&&!["pausing","stopping","finalizing"].includes(d.status), canResume=["paused","error","interrupted"].includes(d.status),canCancel=!['completed','cancelled','stopping'].includes(d.status);
  const stats=[d.provider_label,qualityName(d.quality),d.audio_verified?"Звук проверен · "+(d.audio_codec||"").toUpperCase():"",duration(d.duration_sec),withProject?library.projects.find(p=>p.id===d.project_id)?.name:""].filter(Boolean).map(escape).join(" · ");
  return `<article class="download-row"><div class="download-heading"><div><h3>${escape(d.title)}</h3><div class="metadata">${stats}</div></div>${badge(d.status)}</div>${progress(d)}<div class="download-status"><span>${bytes(d.downloaded_bytes)}${d.total_bytes?" / "+bytes(d.total_bytes):""}${d.percent!=null&&active.has(d.status)?" · "+d.percent.toFixed(1)+"%":""}</span><span>${d.speed_bps?bytes(d.speed_bps)+"/с":""}${d.eta_sec?" · Осталось "+duration(d.eta_sec):""}${d.status==="reconnecting"?"Попытка "+d.attempts:""}</span></div>${d.filename&&d.status==="completed"?`<div class="metadata">${escape(d.filename)}</div>`:""}${d.error?`<p class="error-message">${escape(d.error)}</p>`:""}<div class="download-actions">${canPause?`<button data-action="pause" data-id="${d.id}">Ⅱ Пауза</button>`:""}${canResume?`<button data-action="resume" data-id="${d.id}">▶ Продолжить</button>`:""}${canCancel?`<button data-action="cancel" data-id="${d.id}">Отменить загрузку</button>`:""}${d.status==="completed"?`<button data-play="${d.id}">▶ Открыть запись</button><a class="button" href="/api/downloads/${d.id}/file" download>Сохранить копию</a><button data-open-download="${d.id}">↗ Папка файла</button>`:""}${!active.has(d.status)?`<button class="quiet" data-delete-download="${d.id}">Удалить</button>`:""}<a class="source-link" href="${escape(d.url)}" target="_blank" rel="noopener noreferrer">Источник ↗</a></div></article>`;
}
function renderProject(p) {
  $("#content").innerHTML=`<div class="detail-toolbar"><button data-back>← Все проекты</button><button class="primary" data-add>＋ Добавить запись</button><button data-open-project="${p.id}">↗ Папка проекта</button><button data-rename-project="${p.id}">Переименовать</button><select id="project-folder" aria-label="Перенести проект в папку">${folderOptions(p.folder_id)}</select><button class="danger" data-delete-project="${p.id}">Удалить проект</button></div><div class="path">${escape(p.path)}</div>`+(jobsFor(p.id).map(d=>downloadRow(d)).join("")||empty("В проекте пока нет записей","Добавьте ссылку: все следующие записи можно сохранить в этот же проект."));
}
function renderQueue() {
  const filter=$("#status-filter").value, search=$("#search").value.toLocaleLowerCase();
  const jobs=library.downloads.filter(d=>filter==="all"?d.status!=="cancelled":filter==="active"?active.has(d.status):d.status===filter).filter(d=>(d.title+" "+library.projects.find(p=>p.id===d.project_id)?.name).toLocaleLowerCase().includes(search));
  $("#content").innerHTML=jobs.map(d=>downloadRow(d,true)).join("")||empty("Очередь пуста","Добавьте запись по ссылке. Число одновременных загрузок задаётся в настройках.");
}
function openDownload() {
  $("#download-submit").disabled=false;
  const f=$("#download-form");f.reset();metadata=null;metadataUrl="";$("#probe-result").hidden=true;$("#download-error").textContent="";$("#passcode-label").hidden=true;
  f.project_id.innerHTML='<option value="">＋ Новый проект</option>'+library.projects.map(p=>`<option value="${p.id}">${escape(p.name)}</option>`).join("");f.project_id.value=project||"";f.folder_id.innerHTML=folderOptions(folder==="none"?"":folder);f.mode.value=library.settings.download_mode||"video";renderQuality();updateNewProject();$("#download-dialog").showModal();f.url.focus();
}
function updateNewProject() {$("#new-project-fields").hidden=!!$("#download-form").project_id.value;}
function renderQuality() {
  const f=$("#download-form"), old=f.quality.value, audio=f.mode.value==="audio";
  if(audio){f.quality.innerHTML='<option value="audio">Оригинальная аудиодорожка</option>';return;}
  const qs=metadata?.qualities.filter(q=>q.height)||[360,480,720,1080,1440,2160].map(h=>({quality:String(h),label:h+"p"}));
  const noAudio=metadata?.audio_available===false;
  f.quality.innerHTML=`<option value="auto" ${noAudio?"disabled":""}>По умолчанию (${qualityName(library.settings.download_quality)})</option>`+qs.map(q=>`<option value="${escape(q.quality)}" ${q.audio_available===false?"disabled":""}>${escape(q.label)}${q.audio_available===false?" · нет аудио":q.audio_available?" · со звуком":""}${q.filesize_bytes?" · ≈ "+bytes(q.filesize_bytes):""}</option>`).join("")+`<option value="max" ${noAudio?"disabled":""}>Максимальное доступное</option>`;
  if([...f.quality.options].some(o=>o.value===old))f.quality.value=old;
}
async function probe() {
  if(probing)return;const f=$("#download-form");if(!f.url.reportValidity())return;probing=true;$("#probe-button").disabled=true;$("#probe-button").textContent="Проверяем источник…";$("#download-submit").disabled=true;$("#download-error").textContent="";
  const url=f.url.value.trim(),passcode=f.passcode.value;
  try{const result=await api("/probe","POST",{url,passcode:passcode||null});if(f.url.value.trim()!==url || f.passcode.value!==passcode)return;metadata=result;metadataUrl=url;$("#probe-result").innerHTML=`<strong>${escape(metadata.title)}</strong><div class="metadata">${[metadata.provider_label,metadata.uploader,duration(metadata.duration_sec)].filter(Boolean).map(escape).join(" · ")}</div><div class="hint">${metadata.audio_available===false?"Источник не отдал звук. Загрузка видео без аудио запрещена.":metadata.audio_available?"Аудиодорожка доступна. После сборки звук проверяется, для видео используется совместимый AAC.":"Источник не сообщил состав дорожек. Звук будет проверен в готовом файле."} Размеры приблизительные.</div>`;$("#probe-result").hidden=false;if(!f.project_name.value)f.project_name.value=metadata.title;renderQuality();}catch(error){$("#download-error").textContent=error.message;}finally{probing=false;$("#probe-button").disabled=false;$("#probe-button").textContent="Проверить ссылку";$("#download-submit").disabled=metadata?.audio_available===false;}
}
function askText(title,initial="") {
  $("#text-title").textContent=title;$("#text-value").value=initial;$("#text-dialog").showModal();$("#text-value").focus();return new Promise(resolve=>textResolve=resolve);
}
function askDelete(title) {$("#delete-title").textContent=title;$("#delete-dialog").showModal();return new Promise(resolve=>deleteResolve=resolve);}
$("#text-form").addEventListener("submit",event=>{event.preventDefault();const value=$("#text-value").value.trim();if(value){textResolve?.(value);textResolve=null;$("#text-dialog").close();}});
$("#text-dialog").addEventListener("close",()=>{textResolve?.(null);textResolve=null;});
$("#delete-dialog").addEventListener("close",()=>{deleteResolve?.(null);deleteResolve=null;});
$("#delete-keep").onclick=()=>{deleteResolve?.(false);deleteResolve=null;$("#delete-dialog").close();};
$("#delete-files").onclick=()=>{deleteResolve?.(true);deleteResolve=null;$("#delete-dialog").close();};
document.querySelectorAll(".close").forEach(b=>b.onclick=()=>b.closest("dialog").close());
$("#player-dialog").addEventListener("close",()=>{$("#player").pause();$("#player").removeAttribute("src");$("#player").load();});
$("#add-download").onclick=openDownload;
$("#probe-button").onclick=probe;
$("#download-form").project_id.onchange=updateNewProject;
$("#download-form").mode.onchange=renderQuality;
$("#create-project-inline").onclick=()=>{const f=$("#download-form");f.project_id.value="";updateNewProject();f.project_name.focus();};
$("#create-folder-inline").onclick=()=>guarded(async()=>{const name=await askText("Новая папка проектов");if(!name)return;const created=await api("/folders","POST",{name});await refresh(true);$("#download-form").folder_id.innerHTML=folderOptions(created.id);toast("Папка создана и выбрана. Ссылка сохранена.");});
function invalidateProbe() {metadata=null;metadataUrl="";$("#probe-result").hidden=true;$("#download-submit").disabled=probing;const f=$("#download-form");try {$("#passcode-label").hidden=!new URL(f.url.value).hostname.endsWith("zoom.us");}catch{$("#passcode-label").hidden=true;}renderQuality();}
$("#download-form").url.oninput=invalidateProbe;$("#download-form").passcode.oninput=invalidateProbe;
$("#download-form").addEventListener("submit",async event=>{event.preventDefault();const f=event.target,button=$("#download-submit");button.disabled=true;button.textContent="Добавляем запись…";$("#download-error").textContent="";try{const result=await api("/downloads","POST",{url:f.url.value.trim(),passcode:f.passcode.value||null,project_id:f.project_id.value||null,project_name:f.project_name.value.trim()||null,folder_id:f.folder_id.value||null,quality:f.mode.value==="audio"?"audio":f.quality.value==="auto"?library.settings.download_quality:f.quality.value});project=result.project_id;view="all";folder=null;selecting=false;$("#download-dialog").close();await refresh(true);toast("Запись добавлена в очередь");}catch(error){$("#download-error").textContent=error.message;}finally{button.disabled=false;button.textContent="Добавить в очередь";}});
$("#settings-button").onclick=()=>guarded(async()=>{const f=$("#settings-form");for(const [k,v]of Object.entries(library.settings)){if(f.elements[k])f.elements[k].value=v;}if(f.download_quality.value==="")f.download_quality.value="1080";$("#settings-error").textContent="";$("#settings-dialog").showModal();const h=await api("/health");$("#diagnostics").innerHTML=`yt-dlp: ${escape(h.yt_dlp||"не установлен")}<br>JavaScript: ${h.deno?"Deno ✓":h.node?"Node.js ✓":"не найден — YouTube может не работать"}<br>EJS: ${escape(h.ejs||"не найден")}<br>FFmpeg: ${h.ffmpeg&&h.ffprobe?"готов ✓":"не найден"}<br>Данные: ${escape(h.data_dir)}`;});
$("#settings-form").addEventListener("submit",async event=>{event.preventDefault();const b=event.submitter;b.disabled=true;try{await api("/settings","PUT",Object.fromEntries(new FormData(event.target)));$("#settings-dialog").close();await refresh(true);toast("Настройки сохранены");}catch(error){$("#settings-error").textContent=error.message;}finally{b.disabled=false;}});
$("#pick-root").onclick=()=>guarded(async()=>{const r=await api("/pick-folder","POST");if(r.path)$("#settings-form").download_root.value=r.path;});
$("#open-cookies").onclick=()=>guarded(()=>api("/open-folder","POST",{kind:"cookies"}));
$("#open-root").onclick=()=>guarded(()=>api("/open-folder","POST",{kind:"root"}));
$("#new-project").onclick=()=>guarded(async()=>{const name=await askText("Новый проект");if(name){const p=await api("/projects","POST",{name,folder_id:folder&&folder!=="none"?folder:null});project=p.id;view="all";await refresh(true);}});
$("#add-folder").onclick=()=>guarded(async()=>{const name=await askText("Новая папка проектов");if(name){await api("/folders","POST",{name});await refresh(true);}});
$("#select-projects").onclick=()=>{selecting=!selecting;if(!selecting)selected.clear();render();};
$("#search").oninput=render;$("#status-filter").onchange=render;
$("#stop-service").onclick=()=>guarded(async()=>{await api("/shutdown","POST");stopped=true;$("#service-status").textContent="Сервис остановлен";toast("Докачка сохранена. Для продолжения запустите загрузчик снова.");});
document.addEventListener("click",event=>{
  const b=event.target.closest("[data-view],[data-folder],[data-project],[data-back],[data-add],[data-action],[data-play],[data-open-project],[data-open-download],[data-rename-project],[data-delete-project],[data-delete-download],[data-rename-folder],[data-delete-folder],[data-select-all],[data-move-selected],[data-delete-selected]");if(!b)return;
  guarded(async()=>{
    const d=b.dataset;
    if(d.view){view=d.view;folder=null;project=null;selecting=false;render();}
    else if(d.folder){folder=d.folder;view="all";project=null;render();}
    else if(d.project){if(selecting){selected.has(d.project)?selected.delete(d.project):selected.add(d.project);}else project=d.project;render();}
    else if("back"in d){project=null;render();}
    else if("add"in d)openDownload();
    else if(d.action){await api(`/downloads/${d.id}/${d.action}`,"POST");await refresh(true);}
    else if(d.play){const job=library.downloads.find(x=>x.id===d.play);$("#player-title").textContent=job.title;$("#player").src=`/api/downloads/${d.play}/play`;$("#player-dialog").showModal();}
    else if(d.openProject||d.openDownload)await api("/open-folder","POST",{kind:d.openProject?"project":"download",id:d.openProject||d.openDownload});
    else if(d.renameProject){const p=library.projects.find(p=>p.id===d.renameProject);const name=await askText("Переименовать проект",p.name);if(name){await api(`/projects/${p.id}`,"PATCH",{name});await refresh(true);}}
    else if(d.renameFolder){const f=library.folders.find(f=>f.id===d.renameFolder);const name=await askText("Переименовать папку",f.name);if(name){await api(`/folders/${f.id}`,"PATCH",{name});await refresh(true);}}
    else if(d.deleteFolder){await api(`/folders/${d.deleteFolder}`,"DELETE");if(folder===d.deleteFolder)folder=null;await refresh(true);toast("Папка убрана. Проекты сохранены в «Без папки».");}
    else if(d.deleteProject||d.deleteDownload){const files=await askDelete(d.deleteProject?"Удалить проект?":"Удалить запись?");if(files!==null){await api(`/${d.deleteProject?"projects/"+d.deleteProject:"downloads/"+d.deleteDownload}?delete_files=${files}`,"DELETE");await refresh(true);}}
    else if("selectAll"in d){visibleProjects().forEach(p=>selected.add(p.id));render();}
    else if("moveSelected"in d||"deleteSelected"in d){if(!selected.size)return;const move="moveSelected"in d;const target=$("#bulk-folder").value||null;const files=move?false:await askDelete(`Удалить выбранные проекты (${selected.size})?`);if(files===null)return;const errors=[];let done=0;for(const id of [...selected]){try{await api(`/projects/${id}${move?"":"?delete_files="+files}`,move?"PATCH":"DELETE",move?{folder_id:target}:undefined);selected.delete(id);done++;}catch(error){errors.push(error.message);}}await refresh(true);toast(`Обработано проектов: ${done}.`+(errors.length?" Пропущено: "+errors.length+". "+errors[0]:""));}
  });
});
document.addEventListener("change",event=>{if(event.target.id==="project-folder")guarded(async()=>{await api(`/projects/${project}`,"PATCH",{folder_id:event.target.value||null});await refresh(true);});});
document.addEventListener("keydown",event=>{if((event.key==="Enter"||event.key===" ")&&event.target.matches(".project-card")){event.preventDefault();event.target.click();}});
refresh(true);setInterval(()=>refresh(),1500);
