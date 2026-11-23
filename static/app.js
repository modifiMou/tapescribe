const $ = (id) => document.getElementById(id);

const state = {
  files: [],
  selected: [],
  jobs: [],
  providers: [],
  defaultProvider: "",
  network: null,
  currentId: null,
  transcript: null,
  dirty: false,
  shownStatus: "",
  shownLines: -1,
};

// ---- helpers ----

async function api(path, options = {}) {
  if (options.json !== undefined) {
    options.body = JSON.stringify(options.json);
    options.headers = { "Content-Type": "application/json" };
  }
  const response = await fetch(path, options);
  let body = null;
  try { body = await response.json(); } catch (e) { /* no JSON body */ }
  if (!response.ok) {
    let detail = body && body.detail;
    if (Array.isArray(detail)) detail = detail.map((d) => d.msg).join("；");
    throw new Error(detail || `请求失败（${response.status}）`);
  }
  return body;
}

let toastTimer = null;
function toast(message, isError = false) {
  const el = $("toast");
  el.textContent = message;
  el.classList.toggle("error", isError);
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, isError ? 6000 : 2500);
}

const pad = (n) => String(n).padStart(2, "0");
function clock(seconds) {
  const s = Math.max(0, Math.floor(seconds));
  return `${pad(Math.floor(s / 3600))}:${pad(Math.floor((s % 3600) / 60))}:${pad(s % 60)}`;
}
function fileSize(bytes) {
  if (bytes >= 1 << 30) return `${(bytes / (1 << 30)).toFixed(2)} GB`;
  if (bytes >= 1 << 20) return `${(bytes / (1 << 20)).toFixed(1)} MB`;
  return `${Math.max(1, Math.round(bytes / 1024))} KB`;
}
function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

// ---- audio library ----

async function loadFiles() {
  state.files = await api("/api/files");
  const names = new Set(state.files.map((f) => f.name));
  state.selected = state.selected.filter((name) => names.has(name));
  renderFiles();
}

function renderFiles() {
  const list = $("files");
  list.replaceChildren();
  $("filesEmpty").hidden = state.files.length > 0;
  $("pickHint").hidden = state.files.length === 0;
  for (const file of state.files) {
    const index = state.selected.indexOf(file.name);
    const li = el("li", index >= 0 ? "on" : "");
    li.append(el("span", "order", index >= 0 ? String(index + 1) : ""));

    const info = el("div");
    info.style.minWidth = "0";
    info.append(el("div", "fname", file.name));
    const meta = file.valid
      ? `${clock(file.duration)} · ${fileSize(file.size)} · ${file.codec} ${file.sample_rate} Hz`
      : `${fileSize(file.size)} · 无法读取音轨`;
    info.append(el("div", "fmeta", meta));
    info.title = file.name;
    li.append(info);

    const tools = el("div", "frow-tools");
    const remove = el("button", "link danger", "删除");
    remove.addEventListener("click", async (event) => {
      event.stopPropagation();
      if (!confirm(`把 ${file.name} 移到回收站（data/trash）？`)) return;
      try { await api(`/api/files/${encodeURIComponent(file.name)}`, { method: "DELETE" }); }
      catch (error) { toast(error.message, true); }
      loadFiles();
    });
    tools.append(remove);
    li.append(tools);

    li.addEventListener("click", () => {
      if (index >= 0) state.selected.splice(index, 1);
      else state.selected.push(file.name);
      renderFiles();
    });
    list.append(li);
  }
  renderSelection();
}

function renderSelection() {
  const count = state.selected.length;
  const total = state.files
    .filter((f) => state.selected.includes(f.name))
    .reduce((sum, f) => sum + (f.duration || 0), 0);
  $("selNote").textContent = count ? `已选 ${count} 个，共 ${clock(total)}` : "未选择音频";
  $("transcribeBtn").disabled = count < 1;
  $("mergeBtn").disabled = count < 2;
  const deleteBtn = $("deleteSelected");
  deleteBtn.textContent = count ? `删除所选（${count}）` : "删除所选";
  deleteBtn.classList.toggle("armed", count > 0);
}

$("selectAll").addEventListener("click", () => {
  state.selected = state.files.map((f) => f.name);
  renderFiles();
});
$("selectNone").addEventListener("click", () => {
  state.selected = [];
  renderFiles();
});

$("deleteSelected").addEventListener("click", async () => {
  const names = [...state.selected];
  if (!names.length) {
    toast("先在上面的音频列表里点选要删除的文件（点条目即选中），再点这里批量删除");
    const list = $("files");
    list.classList.remove("flash");
    void list.offsetWidth;
    list.classList.add("flash");
    setTimeout(() => list.classList.remove("flash"), 1600);
    return;
  }
  const picked = state.files.filter((file) => names.includes(file.name));
  const bytes = picked.reduce((sum, file) => sum + (file.size || 0), 0);
  if (!confirm(`把所选的 ${names.length} 个音频（共 ${fileSize(bytes)}）移到回收站（data/trash）？\n\n需要时可以到 data/trash 里找回。`)) return;
  try {
    const result = await api("/api/files/delete", { method: "POST", json: { names } });
    const parts = [];
    if (result.deleted.length) parts.push(`已删除 ${result.deleted.length} 个`);
    if (result.refused.length) {
      const reasons = [...new Set(result.refused.map((item) => item.reason))].join("、");
      parts.push(`跳过 ${result.refused.length} 个（${reasons}）`);
    }
    if (result.missing.length) parts.push(`${result.missing.length} 个已不在库中`);
    toast(parts.join("，") || "没有可删除的音频", result.deleted.length === 0);
    state.selected = state.selected.filter((name) => !result.deleted.includes(name));
  } catch (error) {
    toast(error.message, true);
  }
  loadFiles();
});

function fileHint(message, isError = false) {
  $("fileHint").textContent = message;
  $("fileHint").classList.toggle("error", isError);
}

$("uploadInput").addEventListener("change", async (event) => {
  const picked = [...event.target.files];
  if (!picked.length) return;
  const data = new FormData();
  picked.forEach((file) => data.append("files", file));
  fileHint(`正在上传 ${picked.length} 个文件…`);
  try {
    const result = await api("/api/files/upload", { method: "POST", body: data });
    fileHint(`已上传 ${result.saved.length} 个文件`);
  } catch (error) {
    fileHint(error.message, true);
  }
  event.target.value = "";
  loadFiles();
});

$("importBtn").addEventListener("click", async () => {
  const path = $("importPath").value.trim();
  if (!path) return;
  fileHint("正在导入…");
  try {
    const result = await api("/api/files/import", { method: "POST", json: { path } });
    fileHint(`已导入 ${result.saved.length} 个文件` + (result.skipped ? `，跳过已存在的 ${result.skipped} 个` : ""));
  } catch (error) {
    fileHint(error.message, true);
  }
  loadFiles();
});

// ---- actions ----

$("mergeBtn").addEventListener("click", async () => {
  const suggested = state.selected[0].replace(/\.[^.]+$/, "") + `_合并${state.selected.length}段`;
  const name = prompt("合并后的文件名（不含扩展名）", suggested);
  if (name === null) return;
  try {
    await api("/api/merge", { method: "POST", json: { files: state.selected, name } });
    toast("已开始合并");
    loadJobs();
  } catch (error) {
    toast(error.message, true);
  }
});

$("transcribeBtn").addEventListener("click", async () => {
  try {
    const created = await api("/api/transcribe", {
      method: "POST",
      json: {
        files: state.selected,
        provider_id: $("provider").value,
        language: $("language").value,
        speakers: Number($("speakers").value),
        context: $("context").value,
      },
    });
    toast(`已加入 ${created.length} 个转写任务`);
    await loadJobs();
    openJob(created[0].id);
  } catch (error) {
    toast(error.message, true);
  }
});

// ---- jobs ----

const STATUS_TEXT = { queued: "排队中", running: "进行中", done: "完成", failed: "失败", canceled: "已取消" };
let knownDone = new Set();

async function loadJobs() {
  const jobs = await api("/api/jobs");
  const mergeFinished = jobs.some(
    (job) => job.type === "merge" && job.status === "done" && !knownDone.has(job.id),
  );
  knownDone = new Set(jobs.filter((job) => job.status === "done").map((job) => job.id));
  state.jobs = jobs;
  renderJobs();
  if (mergeFinished) loadFiles();
  const current = jobs.find((job) => job.id === state.currentId);
  if (current) syncViewer(current);
}

function renderJobs() {
  const list = $("jobs");
  list.replaceChildren();
  $("jobsEmpty").hidden = state.jobs.length > 0;
  const retryable = state.jobs.filter(
    (job) => job.type === "transcribe" && job.status === "failed",
  );
  const retryAll = $("retryFailedBtn");
  retryAll.hidden = retryable.length === 0;
  retryAll.textContent = `重试失败任务（${retryable.length}）`;
  for (const job of state.jobs) {
    const li = el("li", job.id === state.currentId ? "current" : "");
    const top = el("div", "job-top");
    top.append(el("span", "tag", job.type === "merge" ? "合并" : "转写"));
    const title = el("span", "job-title", job.title);
    title.title = job.title;
    top.append(title);
    top.append(el("span", `tag ${job.status}`, STATUS_TEXT[job.status] || job.status));
    li.append(top);

    const active = job.status === "queued" || job.status === "running";
    if (job.status === "running") {
      const bar = el("div", "bar");
      const fill = el("i");
      fill.style.width = `${Math.round((job.progress || 0) * 100)}%`;
      bar.append(fill);
      li.append(bar);
    }

    const sub = el("div", "job-sub");
    let note = "";
    if (job.status === "running") note = `${job.stage} · ${Math.round((job.progress || 0) * 100)}%`;
    else if (job.status === "failed") note = job.error;
    else if (job.type === "transcribe") {
      note = job.params.provider_name;
      if (job.status === "done") note += ` · ${clock(job.duration || 0)} · ${job.line_count || 0} 行`;
    } else note = `${job.params.sources.length} 段 → ${job.params.output}`;
    const noteEl = el("span", "grow", note);
    noteEl.title = note;
    sub.append(noteEl);

    const action = el("button", "link danger", active ? "取消" : "删除");
    action.addEventListener("click", async (event) => {
      event.stopPropagation();
      try {
        if (active) await api(`/api/jobs/${job.id}/cancel`, { method: "POST" });
        else {
          if (job.type === "transcribe" && job.status === "done" && !confirm("删除这条转写结果？")) return;
          await api(`/api/jobs/${job.id}`, { method: "DELETE" });
          if (state.currentId === job.id) closeViewer();
        }
      } catch (error) { toast(error.message, true); }
      loadJobs();
    });
    sub.append(action);
    li.append(sub);

    if (job.type === "transcribe") li.addEventListener("click", () => openJob(job.id));
    else li.style.cursor = "default";
    list.append(li);
  }
}

// ---- transcript viewer ----

function closeViewer() {
  state.currentId = null;
  state.transcript = null;
  state.dirty = false;
  $("viewer").hidden = true;
  $("placeholder").hidden = false;
  $("player").pause();
}

async function openJob(id) {
  if (state.dirty && !confirm("当前转写有未保存的修改，确定离开？")) return;
  state.currentId = id;
  state.dirty = false;
  state.shownStatus = "";
  state.shownLines = -1;
  state.transcript = null;
  $("placeholder").hidden = true;
  $("viewer").hidden = false;
  $("lines").replaceChildren();
  $("speakerBar").replaceChildren();
  const player = $("player");
  player.pause();
  player.removeAttribute("src");
  player.hidden = true;
  renderJobs();
  const job = state.jobs.find((item) => item.id === id);
  if (job) await syncViewer(job);
}

async function syncViewer(job) {
  $("vTitle").textContent = job.title;
  const bits = [job.params.provider_name];
  if (job.params.model) bits.push(job.params.model);
  if (job.duration) bits.push(clock(job.duration));
  $("vMeta").textContent = bits.join(" · ");

  const running = job.status === "queued" || job.status === "running";
  $("vProgress").hidden = !running;
  $("vBar").style.width = `${Math.round((job.progress || 0) * 100)}%`;
  $("vStage").textContent = `${job.stage} · ${Math.round((job.progress || 0) * 100)}%`;
  $("vError").hidden = !(job.status === "failed" || job.status === "canceled");
  $("vError").textContent = job.error || "";

  const lineCount = job.line_count || 0;
  const changed = job.status !== state.shownStatus || lineCount !== state.shownLines;
  if (!changed || state.dirty) return;
  state.shownStatus = job.status;
  state.shownLines = lineCount;

  const detail = await api(`/api/jobs/${job.id}`);
  if (state.currentId !== job.id) return;
  state.transcript = detail.transcript || null;
  const editable = job.status === "done";
  renderTranscript(editable);

  const player = $("player");
  if (state.transcript && player.hidden) {
    player.src = `/api/jobs/${job.id}/audio`;
    player.hidden = false;
  }
  document.querySelectorAll(".viewer-tools a").forEach((link) => {
    link.href = `/api/jobs/${job.id}/export?fmt=${link.dataset.fmt}`;
    link.hidden = !editable;
  });
  $("txtBtn").hidden = !state.transcript;
  $("retryBtn").hidden = !(job.status === "failed" || job.status === "canceled");
  $("saveBtn").hidden = !editable;
  $("saveBtn").disabled = true;
}

function speakerName(id) {
  return (state.transcript.speakers[String(id)] || "?");
}

function markDirty() {
  state.dirty = true;
  $("saveBtn").disabled = false;
}

function renderTranscript(editable) {
  const bar = $("speakerBar");
  const list = $("lines");
  bar.replaceChildren();
  list.replaceChildren();
  if (!state.transcript) return;

  const ids = Object.keys(state.transcript.speakers).map(Number).sort((a, b) => a - b);
  if (editable) {
    for (const id of ids) {
      const label = el("label");
      label.append(el("span", `who s${id % 4}`, "人物"));
      const input = el("input", "input");
      input.value = speakerName(id);
      input.maxLength = 20;
      input.addEventListener("input", () => {
        state.transcript.speakers[String(id)] = input.value || "?";
        markDirty();
        list.querySelectorAll(`.who[data-speaker="${id}"]`).forEach((node) => {
          node.textContent = input.value || "?";
        });
      });
      label.append(input);
      bar.append(label);
    }
    if (ids.length > 1) bar.append(el("span", "hint", "点击每行前的人物标记可切换该行的说话人；点击时间可从该处播放。"));
  }

  let previous = null;
  state.transcript.lines.forEach((line, index) => {
    const li = el("li", line.speaker !== previous ? "turn" : "");
    previous = line.speaker;
    li.dataset.index = index;

    const who = el("button", `who s${line.speaker % 4}`, speakerName(line.speaker));
    who.dataset.speaker = line.speaker;
    who.type = "button";
    who.disabled = !editable || ids.length < 2;
    who.title = "切换说话人";
    who.addEventListener("click", () => {
      const next = ids[(ids.indexOf(line.speaker) + 1) % ids.length];
      line.speaker = next;
      markDirty();
      const top = window.scrollY;
      renderTranscript(true);
      window.scrollTo(0, top);
    });
    li.append(who);

    const ts = el("button", "ts", clock(line.start));
    ts.type = "button";
    ts.title = `${clock(line.start)} - ${clock(line.end)}，点击播放`;
    ts.addEventListener("click", () => {
      const player = $("player");
      player.currentTime = line.start;
      player.play();
    });
    li.append(ts);

    const text = el("span", "tx", line.text);
    if (editable) {
      text.contentEditable = "plaintext-only";
      text.spellcheck = false;
      text.addEventListener("input", () => {
        line.text = text.textContent.replace(/\n/g, " ");
        markDirty();
      });
      text.addEventListener("keydown", (event) => {
        if (event.key === "Enter") event.preventDefault();
      });
    }
    li.append(text);
    list.append(li);
  });
}

$("player").addEventListener("timeupdate", () => {
  if (!state.transcript) return;
  const now = $("player").currentTime;
  const lines = state.transcript.lines;
  let active = -1;
  for (let i = 0; i < lines.length; i++) {
    if (lines[i].start <= now) active = i; else break;
  }
  const current = document.querySelector(".lines li.playing");
  if (current && Number(current.dataset.index) === active) return;
  if (current) current.classList.remove("playing");
  const next = document.querySelector(`.lines li[data-index="${active}"]`);
  if (next) next.classList.add("playing");
});

$("saveBtn").addEventListener("click", async () => {
  try {
    await api(`/api/jobs/${state.currentId}/transcript`, {
      method: "PUT",
      json: { speakers: state.transcript.speakers, lines: state.transcript.lines },
    });
    state.dirty = false;
    $("saveBtn").disabled = true;
    toast("已保存");
  } catch (error) {
    toast(error.message, true);
  }
});

$("retryBtn").addEventListener("click", async () => {
  try {
    await api(`/api/jobs/${state.currentId}/retry`, { method: "POST" });
    toast("已重新排队");
  } catch (error) {
    toast(error.message, true);
  }
  loadJobs();
});

$("retryFailedBtn").addEventListener("click", async () => {
  try {
    const result = await api("/api/jobs/retry-failed", { method: "POST" });
    const skipped = result.skipped ? `，${result.skipped} 个跳过（源音频已删）` : "";
    toast(`已重新排队 ${result.retried} 个任务${skipped}`);
  } catch (error) {
    toast(error.message, true);
  }
  loadJobs();
});

$("txtBtn").addEventListener("click", () => {
  if (!state.transcript) return;
  const job = state.jobs.find((item) => item.id === state.currentId);
  const name = `${(job ? job.title.replace(/\.[^.]+$/, "") : "转写结果")}.txt`;
  const text = state.transcript.lines
    .map((line) => `[${clock(line.start)} - ${clock(line.end)}] ${speakerName(line.speaker)}：${line.text}`)
    .join("\n");
  const url = URL.createObjectURL(new Blob([`${text}\n`], { type: "text/plain;charset=utf-8" }));
  const link = el("a");
  link.href = url;
  link.download = name;
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
  toast("已导出 TXT");
});

window.addEventListener("beforeunload", (event) => {
  if (state.dirty) event.preventDefault();
});

// ---- providers ----

const TYPE_TEXT = {
  mlx: "本地 mlx-qwen3-asr",
  openai: "OpenAI 兼容",
  dashscope_filetrans: "百炼录音文件识别",
};
const TYPE_DEFAULT_URL = {
  mlx: "http://127.0.0.1:8765",
  openai: "https://dashscope.aliyuncs.com/compatible-mode/v1",
  dashscope_filetrans: "https://dashscope.aliyuncs.com/api/v1",
};

// ---- access scope ----

const NETWORK_TEXT = {
  localhost: "仅本机",
  lan: "局域网",
};

function renderNetwork() {
  const net = state.network;
  const hint = $("networkHint");
  if (!net) { hint.textContent = ""; return; }
  $("networkMode").value = net.mode;
  const addr = net.mode === "lan"
    ? (net.lan_url ? `${net.lan_url}（本机 http://127.0.0.1:${net.port}）` : `监听 0.0.0.0:${net.port}，未检测到网卡地址`)
    : `http://127.0.0.1:${net.port}`;
  hint.textContent = `当前：${NETWORK_TEXT[net.mode] || net.label}，监听 ${net.host}:${net.port}，入口 ${addr}`;
}

$("saveNetwork").addEventListener("click", async () => {
  const mode = $("networkMode").value;
  const hint = $("networkHint");
  hint.classList.remove("error");
  try {
    const info = await api("/api/network", { method: "POST", json: { mode, restart: true } });
    if (!info.restarting) {
      toast("访问范围未变化");
      await loadState();
      return;
    }
    const local = ["127.0.0.1", "localhost", "[::1]"].includes(location.hostname);
    if (mode === "localhost" && !local) {
      hint.textContent = `已切换为仅本机，请用 http://127.0.0.1:${info.port} 打开`;
      toast("已切换为仅本机");
      return;
    }
    hint.textContent = "已切换，服务重启中…";
    toast("访问范围已切换，服务重启中");
    setTimeout(() => location.reload(), 3000);
  } catch (error) {
    hint.classList.add("error");
    hint.textContent = error.message;
  }
});

async function loadState() {
  const data = await api("/api/state");
  state.providers = data.providers;
  state.defaultProvider = data.default_provider;
  state.network = data.network;
  renderNetwork();
  $("envNote").textContent = data.diarization_ready
    ? `音频目录 ${data.audio_dir}`
    : "说话人分离模型缺失：只能按 1 人转写";
  const select = $("provider");
  const keep = select.value;
  select.replaceChildren();
  for (const provider of state.providers) {
    const option = el("option", "", `${provider.name}${provider.model ? "（" + provider.model + "）" : ""}`);
    option.value = provider.id;
    select.append(option);
  }
  select.value = state.providers.some((p) => p.id === keep) ? keep : state.defaultProvider;
  renderProviders();
}

function renderProviders() {
  const list = $("providerList");
  list.replaceChildren();
  for (const provider of state.providers) {
    const li = el("li");
    const info = el("div", "pinfo");
    info.append(el("div", "", provider.name + (provider.id === state.defaultProvider ? "（默认）" : "")));
    const keyNote = provider.has_api_key ? "已填 Key" : (provider.api_key_file ? "使用密钥文件" : "未配置 Key");
    info.append(el("div", "pmodel", `${TYPE_TEXT[provider.type]} · ${provider.model || "未填模型名"} · ${keyNote}`));
    li.append(info);

    const result = el("div", "ptest");
    const test = el("button", "btn small", "测试连接");
    test.addEventListener("click", async () => {
      result.className = "ptest";
      result.textContent = "测试中…";
      try {
        const out = await api(`/api/providers/${provider.id}/test`, { method: "POST" });
        result.classList.add(out.ok ? "ok" : "bad");
        result.textContent = out.message;
      } catch (error) {
        result.classList.add("bad");
        result.textContent = error.message;
      }
    });
    const edit = el("button", "btn small", "编辑");
    edit.addEventListener("click", () => showProviderForm(provider));
    const remove = el("button", "btn small danger", "删除");
    remove.addEventListener("click", async () => {
      if (!confirm(`删除模型“${provider.name}”？`)) return;
      try { await api(`/api/providers/${provider.id}`, { method: "DELETE" }); }
      catch (error) { toast(error.message, true); }
      loadState();
    });
    li.append(test, edit, remove, result);
    list.append(li);
  }
}

function showProviderForm(provider) {
  const form = $("providerForm");
  form.hidden = false;
  $("providerHint").textContent = "";
  form.elements.id.value = provider ? provider.id : "";
  form.elements.name.value = provider ? provider.name : "";
  form.elements.type.value = provider ? provider.type : "dashscope_filetrans";
  form.elements.base_url.value = provider ? provider.base_url : TYPE_DEFAULT_URL.dashscope_filetrans;
  form.elements.model.value = provider ? provider.model : "";
  form.elements.api_key.value = "";
  form.elements.api_key.placeholder = provider && provider.has_api_key ? "已保存，留空则不修改" : "";
  form.elements.api_key_file.value = provider ? provider.api_key_file : "";
  form.elements.rate_per_min.value = provider ? provider.rate_per_min : 0;
  const extra = provider ? provider.extra : {};
  form.elements.extra.value = Object.keys(extra).length ? JSON.stringify(extra, null, 2) : "";
  form.elements.make_default.checked = provider ? provider.id === state.defaultProvider : false;
  form.elements.name.focus();
}

$("providerForm").elements.type.addEventListener("change", (event) => {
  const form = $("providerForm");
  const known = Object.values(TYPE_DEFAULT_URL);
  if (!form.elements.base_url.value || known.includes(form.elements.base_url.value)) {
    form.elements.base_url.value = TYPE_DEFAULT_URL[event.target.value];
  }
});

$("providerForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.target;
  const hint = $("providerHint");
  hint.classList.remove("error");
  let extra = {};
  const rawExtra = form.elements.extra.value.trim();
  if (rawExtra) {
    try { extra = JSON.parse(rawExtra); }
    catch (error) {
      hint.classList.add("error");
      hint.textContent = "附加请求参数不是合法的 JSON";
      return;
    }
  }
  try {
    await api("/api/providers", {
      method: "POST",
      json: {
        id: form.elements.id.value,
        name: form.elements.name.value,
        type: form.elements.type.value,
        base_url: form.elements.base_url.value,
        model: form.elements.model.value,
        api_key: form.elements.api_key.value,
        api_key_file: form.elements.api_key_file.value,
        rate_per_min: Number(form.elements.rate_per_min.value || 0),
        extra,
        make_default: form.elements.make_default.checked,
      },
    });
    form.hidden = true;
    toast("模型已保存");
    loadState();
  } catch (error) {
    hint.classList.add("error");
    hint.textContent = error.message;
  }
});

$("addProvider").addEventListener("click", () => showProviderForm(null));
$("cancelProvider").addEventListener("click", () => { $("providerForm").hidden = true; });
$("openSettings").addEventListener("click", () => {
  $("providerForm").hidden = true;
  $("settings").showModal();
});

// ---- start ----

async function refresh() {
  try { await loadJobs(); } catch (error) { /* server restarting; retry on next tick */ }
}

(async () => {
  try {
    await Promise.all([loadState(), loadFiles(), loadJobs()]);
  } catch (error) {
    toast(error.message, true);
  }
  setInterval(refresh, 1500);
})();
